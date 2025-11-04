"""
Minimal, safe migration to use openai.OpenAI() client when available.
Behavior:
- Try to use new `openai.OpenAI()` client only.
- Do NOT attempt legacy calls that trigger APIRemovedInV1 (Completion.create / ChatCompletion.create).
- If OpenAI client is missing/unusable, skip OpenAI and try Gemini (Google AI) fallback.
- If both fail, return deterministic stub JSON.
- Minimal changes to keep compatibility with ai_core/main (generate_ai_json returns str).
"""
from __future__ import annotations
import os
import time
import random
import json
import logging
import hashlib
import re
from typing import Dict, Any, Tuple, Optional
from functools import lru_cache, wraps
# Добавляем импорты для конкретных исключений
import requests # Для сетевых ошибок в requests (хотя здесь используется client, все равно полезно)

# try import openai
try:
    import openai
    # Импортируем специфические ошибки OpenAI
    from openai import APIError as OpenAIAPIError, AuthenticationError as OpenAIAuthError, RateLimitError as OpenAIRateLimitError
except Exception:
    openai = None
    OpenAIAPIError = OpenAIRateLimitError = OpenAIAuthError = Exception # fallback

# try import gemini
try:
    import google.generativeai as genai
    # Импортируем специфические ошибки Gemini
    from google.api_core.exceptions import GoogleAPIError as GeminiAPIError, ResourceExhausted as GeminiRateLimitError
except Exception:
    genai = None
    GeminiAPIError = GeminiRateLimitError = Exception # fallback

logger = logging.getLogger("uvicorn.error")

# --- ENV / configuration ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
OPENAI_FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", OPENAI_MODEL)
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1024"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))
OPENAI_REQUEST_TIMEOUT = int(os.getenv("OPENAI_REQUEST_TIMEOUT", "30"))
OPENAI_RETRY_ATTEMPTS = int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3"))
OPENAI_RETRY_BACKOFF_BASE = float(os.getenv("OPENAI_RETRY_BACKOFF_BASE", "1.0"))
OPENAI_USE_STUB = os.getenv("OPENAI_USE_STUB", "0").lower() in ("1", "true", "yes")

# Gemini (Google AI) fallback
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash") # Используем быструю модель

# If module-level api_key attribute exists, set it for best-effort compatibility
if openai is not None and OPENAI_API_KEY:
    try:
        if hasattr(openai, "api_key"):
            openai.api_key = OPENAI_API_KEY
    except Exception:
        # ignore if cannot set
        pass

# --- utilities ---
def _prompt_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _build_prompt(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    client = proposal.get("client_name", "")
    provider = proposal.get("provider_name", "")
    project_goal = proposal.get("project_goal", "")
    scope = proposal.get("scope", "")
    technologies = proposal.get("technologies") or []
    techs = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)
    deadline = proposal.get("deadline", "")

    tone_instruction = {
        "Formal": "Use a formal, professional tone focused on clarity and precision.",
        "Marketing": "Write in a persuasive, benefit-focused marketing tone (highlight outcomes).",
        "Technical": "Write in a detailed technical tone focusing on architecture and acceptance criteria.",
        "Friendly": "Write in a friendly, conversational tone.",
    }.get(tone, "Use a neutral and professional tone.")

    prompt = f"""
You are an expert commercial proposal writer. RETURN EXACTLY ONE VALID JSON OBJECT AND NOTHING ELSE — no commentary, no markdown.
The JSON must contain exactly these keys (strings):
  executive_summary_text, project_mission_text, solution_concept_text,
  project_methodology_text, financial_justification_text, payment_terms_text,
  development_note, licenses_note, support_note

Input:
client_name: "{client}"
provider_name: "{provider}"
project_goal: "{project_goal}"
scope: "{scope}"
technologies: "{techs}"
deadline: "{deadline}"
tone: "{tone}"

Instruction:
{tone_instruction}
For each field: write 1-4 concise sentences. If you cannot determine a field, set it to an empty string "".
Do NOT include extra keys.

EXACT JSON EXAMPLE:
{{
  "executive_summary_text": "Short summary...",
  "project_mission_text": "Mission ...",
  "solution_concept_text": "Solution ...",
  "project_methodology_text": "Methodology ...",
  "financial_justification_text": "Why investment ...",
  "payment_terms_text": "Payment schedule ...",
  "development_note": "What development covers ...",
  "licenses_note": "Licenses included ...",
  "support_note": "Support and SLA ..."
}}
"""
    return prompt.strip()

def _extract_text_from_openai_response(resp: Any) -> str:
    """
    Robust extraction for likely response shapes from new OpenAI client.
    """
    try:
        if hasattr(resp, "choices"):
            choices = resp.choices
            if choices and len(choices) > 0:
                first = choices[0]
                # try message.content
                msg = getattr(first, "message", None)
                if msg is not None:
                    content = getattr(msg, "content", None)
                    if content is None and isinstance(msg, dict):
                        content = msg.get("content") or msg.get("text")
                    if content:
                        return content
                # try .text
                if hasattr(first, "text") and first.text:
                    return first.text
                # dict-like fallback
                if isinstance(first, dict):
                    m = first.get("message")
                    if isinstance(m, dict):
                        return m.get("content") or m.get("text") or ""
                    return first.get("text") or ""
        # dict-like response
        if isinstance(resp, dict):
            choices = resp.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    m = first.get("message")
                    if isinstance(m, dict):
                        return m.get("content") or m.get("text") or ""
                    return first.get("text") or ""
    except Exception:
        logger.debug("Failed to parse OpenAI response shape", exc_info=True)
    
    try:
        return str(resp) or ""
    except Exception:
        return ""

def _extract_json_blob(text: str) -> Optional[str]:
    """
    Find the first JSON object or array blob in a string.
    """
    if not text:
        return None
    
    # Ищем первый { ... }
    match_obj = re.search(r"\{.*\}", text, re.DOTALL)
    if match_obj:
        return match_obj.group(0)
    
    # Если не нашли, ищем [ ... ]
    match_arr = re.search(r"\[.*\]", text, re.DOTALL)
    if match_arr:
        return match_arr.group(0)
        
    return None

# ------------- OpenAI: NEW client only -------------
def _call_openai_new_client(prompt_str: str, model_name: str) -> str:
    """
    Use only new openai.OpenAI() client. If not available or fails, raise exception.
    This intentionally avoids attempting legacy API paths that will raise APIRemovedInV1.
    """
    if openai is None:
        raise RuntimeError("openai package not installed")

    OpenAIClass = getattr(openai, "OpenAI", None)
    if OpenAIClass is None:
        # no new client available in this runtime: treat as not supported here
        raise RuntimeError("openai.OpenAI client class not available in this installation")

    # construct client (best-effort: accept api_key in constructor or default)
    try:
        try:
            client = OpenAIClass(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else OpenAIClass()
        except TypeError:
            client = OpenAIClass()
    except Exception as e:
        raise RuntimeError(f"Failed to instantiate openai.OpenAI client: {e}")

    # prepare messages
    messages = [{"role": "user", "content": prompt_str}]

    # prefer client.chat.completions.create (new client shape)
    create_fn = None
    try:
        create_fn = getattr(getattr(client, "chat", None), "completions", None)
        create_fn = getattr(create_fn, "create", None) if create_fn else None
    except Exception:
        create_fn = None

    if not create_fn:
        raise RuntimeError("openai.OpenAI client found but chat.completions.create() not available on it")

    # call (try request_timeout first, fall back if TypeError)
    try:
        try:
            resp = create_fn(model=model_name, messages=messages, max_tokens=OPENAI_MAX_TOKENS, temperature=OPENAI_TEMPERATURE, request_timeout=OPENAI_REQUEST_TIMEOUT)
        except TypeError:
            resp = create_fn(model=model_name, messages=messages, max_tokens=OPENAI_MAX_TOKENS, temperature=OPENAI_TEMPERATURE)
        
        text = _extract_text_from_openai_response(resp)
        logger.info("OpenAI new client returned result for model=%s", model_name)
        return text or ""
    except Exception as e:
        logger.exception("OpenAI new client invocation failed: %s", e)
        raise

# ------------- caching wrapper -------------
def _cached_call(maxsize: int = 256):
    def deco(fn):
        cached = lru_cache(maxsize=maxsize)(fn)

        @wraps(fn)
        def wrapper(prompt_str: str, model_name: str):
            return cached(prompt_str, model_name)
        
        wrapper.cache_clear = cached.cache_clear
        return wrapper
    return deco

@_cached_call(maxsize=512)
def _invoke_openai_cached(prompt_str: str, model_name: str) -> str:
    # cached wrapper around new-client call
    return _call_openai_new_client(prompt_str, model_name)

# ------------- Gemini (Google AI) fallback -------------
def _call_gemini(prompt_str: str) -> Tuple[str, str]:
    """
    Calls Google Gemini API as a fallback.
    Returns (generated_text, reason)
    """
    if genai is None:
        return "", "google-generativeai package not installed"
    if not GOOGLE_API_KEY:
        return "", "GOOGLE_API_KEY not set"

    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        
        # Настройки безопасности (минимальные, чтобы разрешить JSON)
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        # Важно: Gemini может отказаться генерировать JSON, если промпт содержит
        # "instruction" на возврат *только* JSON.
        # Промпты _build_prompt и _build_suggestion_prompt
        # достаточно строгие (RETURN EXACTLY ONE JSON OBJECT)
        
        response = model.generate_content(
            prompt_str,
            safety_settings=safety_settings
        )

        if response.text:
            return response.text, "gemini_success"
        else:
            # Обработка случая, если ответ пустой или заблокирован
            feedback = response.prompt_feedback if hasattr(response, 'prompt_feedback') else 'unknown_reason'
            logger.warning("Gemini returned empty or blocked response. Feedback: %s", feedback)
            return "", f"gemini_empty_or_blocked: {feedback}"
            
    except Exception as e:
        logger.exception("Gemini invocation failed: %s", e)
        return "", f"gemini_error: {e}"

# ------------- public entrypoint (AI sections) -------------
def generate_ai_json(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Returns model text or JSON string. Tries:
      1) cached new OpenAI client
      2) live OpenAI new client (retries)
      3) Gemini (Google AI) fallback
      4) deterministic stub
    Always returns a str (never None).
    """
    if OPENAI_USE_STUB:
        client_name = proposal.get("client_name", "Client")
        stub = {
            "executive_summary_text": f"This is a fallback executive summary for {client_name}.",
            "project_mission_text": "Deliver a reliable, maintainable solution that provides measurable business value.",
            "solution_concept_text": "We propose a pragmatic architecture using modular services and reliable third-party platforms.",
            "project_methodology_text": "Agile with two-week sprints, CI/CD, testing and demos.",
            "financial_justification_text": "Expected benefits and efficiency gains justify the investment.",
            "payment_terms_text": "50% upfront, 50% on delivery. Proposal valid for 30 days.",
            "development_note": "Includes development, QA, and DevOps efforts.",
            "licenses_note": "Includes typical SaaS licenses and hosting.",
            "support_note": "Includes 3 months of post-launch support."
        }
        return json.dumps(stub, ensure_ascii=False)

    prompt_str = _build_prompt(proposal, tone)
    prompt_key = _prompt_hash(prompt_str + (tone or ""))

    # try cached fast path (only uses new OpenAI client)
    try:
        cached = _invoke_openai_cached(prompt_str, OPENAI_MODEL)
        if cached:
            logger.info("Cache hit for prompt %s model=%s", prompt_key, OPENAI_MODEL)
            return cached
    except Exception:
        logger.debug("Cache check failed/miss; proceeding to live call", exc_info=True)

    last_reason = ""
    last_exc = None

    for attempt in range(1, OPENAI_RETRY_ATTEMPTS + 1):
        try:
            logger.info("Attempting OpenAI new client call model=%s attempt=%d/%d", OPENAI_MODEL, attempt, OPENAI_RETRY_ATTEMPTS)
            res = _call_openai_new_client(prompt_str, OPENAI_MODEL)
            if res:
                # try to update cache
                #try:
                   # _invoke_openai_cached.cache_clear() # crude invalidation
                  #  _invoke_openai_cached(prompt_str, OPENAI_MODEL) # re-populate
                #except Exception:
                   # pass
                return res
            last_reason = "openai_empty"
            logger.warning("OpenAI returned empty on attempt %d", attempt)
        except Exception as e:
            last_exc = e
            last_reason = f"{type(e).__name__}:{e}"
            logger.exception("OpenAI new client error on attempt %d: %s", attempt, last_reason)
            # If this is clearly a "no new client available" or instantiation problem, don't retry
            if "openai.OpenAI client class not available" in str(e) or "Failed to instantiate openai.OpenAI client" in str(e):
                break
        
        if attempt < OPENAI_RETRY_ATTEMPTS:
            backoff = OPENAI_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
            jitter = random.random() * (backoff * 0.5)
            sleep_time = backoff + jitter
            logger.debug("Sleeping %.2fs before retry...", sleep_time)
            time.sleep(sleep_time)

    # OpenAI new-client failed -> try Gemini fallback
    logger.info("OpenAI new-client attempts exhausted; trying Gemini fallback. reason=%s", last_reason)
    gemini_text, gemini_reason = _call_gemini(prompt_str)
    if gemini_text:
        logger.info("Gemini fallback succeeded: %s", gemini_reason)
        return gemini_text

    logger.warning("Gemini fallback failed reason=%s; returning deterministic stub.", gemini_reason)
    client_name = proposal.get("client_name", "Client")
    stub = {
        "executive_summary_text": f"This is a fallback executive summary for {client_name}.",
        "project_mission_text": "Deliver a reliable, maintainable solution that provides measurable business value.",
        "solution_concept_text": "We propose a pragmatic architecture using modular services and reliable third-party platforms.",
        "project_methodology_text": "Agile with two-week sprints, CI/CD, testing and demos.",
        "financial_justification_text": "Expected benefits and efficiency gains justify the investment.",
        "payment_terms_text": "50% upfront, 50% on delivery. Proposal valid for 30 days.",
        "development_note": "Includes development, QA, and DevOps efforts.",
        "licenses_note": "Includes typical SaaS licenses and hosting.",
        "support_note": "Includes 3 months of post-launch support."
    }
    return json.dumps(stub, ensure_ascii=False)


# ----------------------------------------------
# Suggestion generation: targeted prompts for deliverables/phases (returns dict)
# ----------------------------------------------
def _build_suggestion_prompt(proposal: Dict[str, Any], tone: str = "Formal", max_deliverables: int = 5, max_phases: int = 5) -> str:
    """
    Build a focused prompt asking the LLM to propose deliverables and phases.
    Returns bilingual prompt (RU/EN short instructions) to improve locality.
    The model MUST return exactly ONE JSON object and NOTHING ELSE.
    """
    client = proposal.get("client_name", "")
    project_goal = proposal.get("project_goal", "")
    scope = proposal.get("scope", "")
    technologies = proposal.get("technologies") or []
    techs = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)

    prompt = f"""
You are an experienced delivery/project manager and proposal writer.

Task (RU):
На основе краткого брифа предложи список ключевых Deliverables (рекомендуется до {max_deliverables}) и Phases (рекомендуется до {max_phases}) для коммерческого предложения.
Возвращай РОВНО ОДИН JSON-ОБЪЕКТ и НИЧЕГО КРОМЕ ЕГО — без markdown, без пояснений.

Task (EN):
Based on the brief below, propose up to {max_deliverables} deliverables and up to {max_phases} phases (timeline steps).
Return EXACTLY ONE JSON OBJECT and NOTHING ELSE.

Input:
client_name: "{client}"
project_goal: "{project_goal}"
scope: "{scope}"
technologies: "{techs}"
tone: "{tone}"

JSON schema to return (exactly this shape):
{{
  "suggested_deliverables": [
    {{
      "title": "<short title, max 8-10 words>",
      "description": "<1-2 sentences describing the deliverable>",
      "acceptance": "<acceptance criteria (1 sentence)>"
    }}
    // repeat up to {max_deliverables}
  ],
  "suggested_phases": [
    {{
      "phase_name": "<short name>",
      "duration_weeks": <integer weeks>,
      "tasks": "<short list or sentence of key tasks>"
    }}
    // repeat up to {max_phases}
  ]
}}

Important:
- Use realistic durations (integer weeks).
- Keep titles concise.
- Do not include any additional keys.
- If uncertain, you may return empty arrays.
"""
    return prompt.strip()


def generate_suggestions(proposal: Dict[str, Any], tone: str = "Formal", max_deliverables: int = 5, max_phases: int = 5) -> Dict[str, Any]:
    """
    Return a dict with keys 'suggested_deliverables' and 'suggested_phases'.
    Tries OpenAI new client, then Gemini, then deterministic stub.
    Always returns a dict (never raises for expected failure cases).
    """
    prompt = _build_suggestion_prompt(proposal, tone, max_deliverables=max_deliverables, max_phases=max_phases)
    
    # Try cached new-client path first
    try:
        # try to use cached invocation of new-client if available
        cached = None
        try:
            # if caching wrapper exists for new client (name may differ), use a direct call to underlying function
            cached = _invoke_openai_cached(prompt, OPENAI_MODEL)
        except Exception:
            cached = None
        
        if cached:
            # cached is raw text; try parse JSON
            try:
                blob = _extract_text_from_openai_response(cached) if isinstance(cached, (dict, object)) else cached
                blob = blob if isinstance(blob, str) else str(blob)
                js = json.loads(_extract_json_blob(blob) or blob)
                if isinstance(js, dict):
                    return {
                        "suggested_deliverables": js.get("suggested_deliverables", []),
                        "suggested_phases": js.get("suggested_phases", [])
                    }
            except Exception:
                pass
    except Exception:
        pass

    # Live attempts (retries) using new-client call function if available
    last_exc = None
    for attempt in range(1, OPENAI_RETRY_ATTEMPTS + 1):
        try:
            # prefer new client call if available
             #try:
                #txt = _call_openai_new_client(prompt, OPENAI_MODEL)
             #except Exception:
                # If new client not available, fall back to _invoke_openai_cached (which may call new client)
            # prefer new client call if available
            txt = _call_openai_new_client(prompt, OPENAI_MODEL)
            
            if not txt:
                last_exc = RuntimeError("empty response")
                continue
                
            # try extract JSON blob
            blob = txt if isinstance(txt, str) else str(txt)
            json_blob = _extract_json_blob(blob)
            parsed = None
            if json_blob:
                parsed = json.loads(json_blob)
            else:
                try:
                    parsed = json.loads(blob)
                except Exception:
                    parsed = None

            if isinstance(parsed, dict):
                return {
                    "suggested_deliverables": parsed.get("suggested_deliverables", []),
                    "suggested_phases": parsed.get("suggested_phases", [])
                }
            # else: try to parse text response heuristically (not ideal)
            last_exc = RuntimeError("Parsed non-dict")
        except Exception as e:
            last_exc = e
            # backoff
            if attempt < OPENAI_RETRY_ATTEMPTS:
                backoff = OPENAI_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                time.sleep(backoff + random.random() * 0.5)
            continue

    # OpenAI failed -> try Gemini
    try:
        gemini_text, gemini_reason = _call_gemini(prompt)
        if gemini_text:
            try:
                blob = gemini_text if isinstance(gemini_text, str) else str(gemini_text)
                json_blob = _extract_json_blob(blob)
                parsed = None
                if json_blob:
                    parsed = json.loads(json_blob)
                else:
                    try:
                        parsed = json.loads(blob)
                    except Exception:
                        parsed = None
                if isinstance(parsed, dict):
                    return {
                        "suggested_deliverables": parsed.get("suggested_deliverables", []),
                        "suggested_phases": parsed.get("suggested_phases", [])
                    }
            except Exception:
                pass
    except Exception:
        pass

    # deterministic stub suggestions
    client = proposal.get("client_name", "Client")
    stub = {
        "suggested_deliverables": [
            {
                "title": "Requirements & Analysis",
                "description": f"Detailed requirements gathering and analysis for {client}.",
                "acceptance": "Approved requirements document signed by client."
            },
            {
                "title": "CRM Integration",
                "description": "Design and implement CRM synchronization and admin panel.",
                "acceptance": "Data sync tested and UAT accepted."
            }
        ],
        "suggested_phases": [
            {"phase_name": "Planning", "duration_weeks": 2, "tasks": "Requirements, scope, prototypes"},
            {"phase_name": "Implementation", "duration_weeks": 8, "tasks": "Development, integration, tests"}
        ]
    }
    return stub