# backend/app/services/openai_service.py
"""
Robust OpenAI service wrapper (synchronous).
- Reads config from environment.
- Supports new OpenAI client or older openai.* interfaces.
- Stub mode for local development/testing.
- Exponential backoff + jitter retries.
- Fallback to cheaper model on rate-limit.
- Simple process-local LRU caching for identical prompts.
- Returns a string (raw model text). Never raises OpenAI errors to callers.
"""

# backend/app/services/openai_service.py (top portion)
import os
import time
import random
import logging
import hashlib
from typing import Dict, Any, Optional
from functools import lru_cache, wraps

logger = logging.getLogger(__name__)

# Try to import openai and its exception classes in a robust way.
# If openai is not installed, we provide graceful fallbacks so the module
# can still be imported (useful for running in stub mode or tests).
try:
    import openai
    # Try to import exception classes used by the SDK
    try:
        from openai.error import OpenAIError, RateLimitError, PermissionError as OpenAIPermissionError
    except Exception:
        # older/newer SDKs might expose these differently; try to map common names
        try:
            OpenAIError = getattr(openai, "OpenAIError")
        except Exception:
            class OpenAIError(Exception): pass
        try:
            RateLimitError = getattr(openai, "RateLimitError")
        except Exception:
            class RateLimitError(OpenAIError): pass
        try:
            OpenAIPermissionError = getattr(openai, "PermissionError")
        except Exception:
            class OpenAIPermissionError(OpenAIError): pass
    # try to import new-style client
    try:
        from openai import OpenAI as OpenAIClientClass  # type: ignore
    except Exception:
        OpenAIClientClass = None
except ModuleNotFoundError:
    openai = None  # type: ignore
    OpenAIClientClass = None
    class OpenAIError(Exception): pass
    class RateLimitError(OpenAIError): pass
    class OpenAIPermissionError(OpenAIError): pass
    logger.warning("openai package not installed in this environment — OpenAI features disabled. Install with 'pip install openai' to enable.")


# --- Configuration ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_TYPE = os.getenv("OPENAI_API_TYPE", "").lower()  # "" or "azure"
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE")
OPENAI_API_VERSION = os.getenv("OPENAI_API_VERSION")
OPENAI_DEPLOYMENT = os.getenv("OPENAI_DEPLOYMENT")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
OPENAI_FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-3.5-turbo")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1024"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))
OPENAI_REQUEST_TIMEOUT = int(os.getenv("OPENAI_REQUEST_TIMEOUT", "30"))

OPENAI_RETRY_ATTEMPTS = int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3"))
OPENAI_RETRY_BACKOFF_BASE = float(os.getenv("OPENAI_RETRY_BACKOFF_BASE", "1.0"))
OPENAI_USE_STUB = os.getenv("OPENAI_USE_STUB", "0").lower() in ("1", "true", "yes")

# initialize client where possible (work with both old/new libs)
_client = None
_USE_NEW_CLIENT = False

if OPENAI_API_KEY:
    try:
        # set top-level openai.api_key for older clients
        openai.api_key = OPENAI_API_KEY
    except Exception:
        logger.debug("Could not set openai.api_key (older/new client mismatch).")

# try to create new client instance if available
if OpenAIClientClass is not None:
    try:
        if OPENAI_API_KEY:
            _client = OpenAIClientClass(api_key=OPENAI_API_KEY)
        else:
            _client = OpenAIClientClass()
        _USE_NEW_CLIENT = True
        # configure azure-specific properties on global openai module if needed
        if OPENAI_API_TYPE == "azure":
            openai.api_type = "azure"
            if OPENAI_API_BASE:
                openai.api_base = OPENAI_API_BASE
            if OPENAI_API_VERSION:
                openai.api_version = OPENAI_API_VERSION
    except Exception as e:
        logger.warning("Could not initialize new OpenAI client: %s. Falling back to module-level API.", e)
        _client = None
        _USE_NEW_CLIENT = False

# --- caching helper (process-local) ---
def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def lru_cache_func(maxsize: int = 256):
    def deco(fn):
        cached = lru_cache(maxsize=maxsize)(fn)
        @wraps(fn)
        def wrapper(*args, **kwargs):
            return cached(*args, **kwargs)
        wrapper.cache_clear = cached.cache_clear
        return wrapper
    return deco

# low-level call wrapper — try multiple invocation styles to maximize compatibility
def _invoke_chat_completion(prompt: str, model: str) -> str:
    """
    Attempt to call OpenAI chat completion using whichever API surface is available.
    May raise OpenAIError exceptions upstream; caller will handle retries.
    Returns the raw string content (or empty string).
    """
    messages = [{"role": "user", "content": prompt}]

    # 1) Try new client (OpenAI())
    if _USE_NEW_CLIENT and _client is not None:
        try:
            # new client uses client.chat.completions.create
            resp = _client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=OPENAI_MAX_TOKENS,
                temperature=OPENAI_TEMPERATURE,
                request_timeout=OPENAI_REQUEST_TIMEOUT
            )
            # try common ways to access content
            choice = resp.choices[0]
            # choice.message.content or choice["message"]["content"]
            content = getattr(choice, "message", None)
            if content:
                # some client returns object with .get or .content
                if isinstance(content, dict):
                    return content.get("content", "") or ""
                return getattr(content, "content", "") or ""
            # fallback dictionary-style
            try:
                return resp.choices[0]["message"]["content"]
            except Exception:
                return ""
        except Exception:
            raise

    # 2) Try module-level chat.completions.create (older)
    try:
        # some versions use openai.ChatCompletion.create, some openai.chat.completions.create
        if hasattr(openai, "chat") and hasattr(openai.chat, "completions") and hasattr(openai.chat.completions, "create"):
            resp = openai.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=OPENAI_MAX_TOKENS,
                temperature=OPENAI_TEMPERATURE,
                request_timeout=OPENAI_REQUEST_TIMEOUT
            )
        elif hasattr(openai, "ChatCompletion") and hasattr(openai.ChatCompletion, "create"):
            resp = openai.ChatCompletion.create(
                model=model,
                messages=messages,
                max_tokens=OPENAI_MAX_TOKENS,
                temperature=OPENAI_TEMPERATURE,
                request_timeout=OPENAI_REQUEST_TIMEOUT
            )
        else:
            # try the generic create on openai (last resort)
            resp = openai.Completion.create(
                engine=model,
                prompt=prompt,
                max_tokens=OPENAI_MAX_TOKENS,
                temperature=OPENAI_TEMPERATURE
            )
            # Completion returns text rather than chat schema
            return getattr(resp, "choices", [])[0].text if getattr(resp, "choices", None) else ""
        # Now extract text in a few ways
        try:
            # common shape: resp.choices[0].message.content
            choice = resp.choices[0]
            if hasattr(choice, "message"):
                message = choice.message
                if isinstance(message, dict):
                    return message.get("content", "") or ""
                return getattr(message, "content", "") or ""
            # fallback dict-style
            try:
                return resp.choices[0]["message"]["content"]
            except Exception:
                # Last fallback: if model returned text directly
                return getattr(choice, "text", "") or ""
        except Exception:
            return ""
    except Exception:
        # bubble up
        raise

# cached low-level call to avoid repeating identical prompts in same process
@lru_cache_func(maxsize=512)
def _call_model_cached(prompt: str, model: str) -> str:
    return _invoke_chat_completion(prompt, model)

def _build_prompt(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    # Minimal sanitization of provided inputs (we assume ai_core or caller sanitized them earlier)
    client = str(proposal.get("client_name", "") or "")
    provider = str(proposal.get("provider_name", "") or "")
    goal = str(proposal.get("project_goal", "") or "")
    scope = str(proposal.get("scope", "") or "")
    technologies = proposal.get("technologies") or []
    techs = ", ".join(technologies) if isinstance(technologies, list) else str(technologies)
    deadline = str(proposal.get("deadline", "") or "")
    tone_instruction = {
        "Formal": "Use a formal, professional tone focused on clarity and precision.",
        "Marketing": "Write in a persuasive, benefit-focused marketing tone (highlight outcomes).",
        "Technical": "Write in a detailed technical tone focusing on architecture and acceptance criteria.",
        "Friendly": "Write in a friendly, conversational tone."
    }.get(tone, "Use a neutral and professional tone.")

    template = f"""
You are a professional proposal writer. Output ONLY valid JSON (UTF-8) with EXACTLY these keys:
executive_summary_text, project_mission_text, solution_concept_text, project_methodology_text,
financial_justification_text, payment_terms_text, development_note, licenses_note, support_note.

Input:
client_name: "{client}"
provider_name: "{provider}"
project_goal: "{goal}"
scope: "{scope}"
technologies: "{techs}"
deadline: "{deadline}"
tone: "{tone}"

Instruction:
{tone_instruction}

Return a single JSON object and nothing else. If you cannot produce text for a field, return an empty string for that field.
"""
    return template.strip()

def _fallback_stub(proposal: Dict[str, Any]) -> str:
    """Return guaranteed-valid JSON (string) as fallback."""
    import json
    client = proposal.get("client_name") or "Client"
    stub = {
        "executive_summary_text": f"This is a fallback executive summary for {client}.",
        "project_mission_text": "Project mission: deliver measurable value and reliable software.",
        "solution_concept_text": "Proposed solution: pragmatic modular services and integrations.",
        "project_methodology_text": "Agile approach with two-week sprints, CI/CD and demos.",
        "financial_justification_text": "Investment is justified by expected revenue uplift and efficiency gains.",
        "payment_terms_text": "50% upfront, 50% on delivery. Proposal valid for 30 days.",
        "development_note": "Includes development, QA and DevOps efforts.",
        "licenses_note": "Includes required third-party SaaS licenses and hosting.",
        "support_note": "Includes 3 months of post-launch support."
    }
    return json.dumps(stub, ensure_ascii=False)

def generate_ai_json(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Synchronous entrypoint.
    Returns: raw model text (expected JSON string). On all failures returns either stub JSON (if configured) or empty string.
    """
    # stub mode for offline/dev
    if OPENAI_USE_STUB:
        logger.info("OPENAI_USE_STUB enabled — returning fallback stub JSON.")
        return _fallback_stub(proposal)

    prompt = _build_prompt(proposal, tone)
    prompt_hash = _hash_text(prompt + (tone or ""))

    # 1) try cache (fast)
    try:
        cached = _call_model_cached(prompt, OPENAI_MODEL)
        if cached:
            logger.debug("OpenAI: cache hit for prompt %s", prompt_hash)
            return cached
    except Exception:
        logger.debug("Cache check failed or miss; proceeding to live request")

    last_exc: Optional[Exception] = None

    for attempt in range(1, OPENAI_RETRY_ATTEMPTS + 1):
        try:
            logger.debug("OpenAI: calling model=%s attempt %d/%d (hash %s)",
                         OPENAI_MODEL, attempt, OPENAI_RETRY_ATTEMPTS, prompt_hash)
            result = _invoke_chat_completion(prompt, OPENAI_MODEL)
            # store to cache via cached wrapper (best-effort)
            try:
                _call_model_cached(prompt, OPENAI_MODEL)
            except Exception:
                pass
            if result:
                return result
            logger.warning("OpenAI returned empty result (attempt %d).", attempt)
        except RateLimitError as e:
            logger.warning("OpenAI RateLimitError (attempt %d): %s", attempt, str(e))
            last_exc = e
            # Try fallback model once
            if OPENAI_FALLBACK_MODEL and OPENAI_FALLBACK_MODEL != OPENAI_MODEL:
                try:
                    logger.info("Switching to fallback model %s due to rate limit.", OPENAI_FALLBACK_MODEL)
                    fallback_res = _invoke_chat_completion(prompt, OPENAI_FALLBACK_MODEL)
                    if fallback_res:
                        return fallback_res
                except Exception as fe:
                    logger.warning("Fallback model failed: %s", fe)
            # break to avoid wasting quota
            break
        except OpenAIPermissionError as e:
            logger.error("OpenAI permission/region error: %s", e)
            last_exc = e
            break
        except OpenAIError as e:
            logger.warning("OpenAIError on attempt %d: %s", attempt, e)
            last_exc = e
        except Exception as e:
            logger.exception("Unexpected error when calling OpenAI (attempt %d): %s", attempt, e)
            last_exc = e

        # backoff
        if attempt < OPENAI_RETRY_ATTEMPTS:
            backoff = OPENAI_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            jitter = random.random() * (backoff * 0.5)
            sleep_for = backoff + jitter
            logger.debug("Sleeping %.2fs before retry...", sleep_for)
            time.sleep(sleep_for)

    # if we reach here — everything failed or unrecoverable
    if last_exc:
        logger.error("OpenAI calls failed: %s", last_exc)
    else:
        logger.error("OpenAI returned no content and no exception; returning fallback.")

    # final fallback: return fallback stub (safer than empty string)
    return _fallback_stub(proposal)
