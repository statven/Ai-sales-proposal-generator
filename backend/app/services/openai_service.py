# backend/app/services/openai_service.py
"""
Minimal, safe migration to use openai.OpenAI() client when available.

Behavior:
- Try to use new `openai.OpenAI()` client only.
- Do NOT attempt legacy calls that trigger APIRemovedInV1 (Completion.create / ChatCompletion.create).
- If OpenAI client is missing/unusable, skip OpenAI and try Hugging Face fallback.
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
from typing import Dict, Any, Tuple
from functools import lru_cache, wraps

import requests

# try import openai
try:
    import openai
except Exception:
    openai = None

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

# Hugging Face fallback
HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
HUGGINGFACE_INFERENCE_URL = os.getenv("HUGGINGFACE_INFERENCE_URL")
HUGGINGFACE_MODEL = os.getenv("HUGGINGFACE_MODEL", "")

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

# ------------- Hugging Face fallback -------------
def _call_huggingface(prompt_str: str) -> Tuple[str, str]:
    if not HUGGINGFACE_INFERENCE_URL:
        return "", "HuggingFace not configured"
    headers = {}
    if HUGGINGFACE_API_KEY:
        headers["Authorization"] = f"Bearer {HUGGINGFACE_API_KEY}"
    payload = {"inputs": prompt_str, "options": {"wait_for_model": True}}
    try:
        r = requests.post(HUGGINGFACE_INFERENCE_URL, headers=headers, json=payload, timeout=30)
    except Exception as e:
        return "", f"HF request error: {e}"
    if r.status_code != 200:
        return "", f"hf-status-{r.status_code}:{r.text}"
    try:
        data = r.json()
        if isinstance(data, dict) and "generated_text" in data:
            return data["generated_text"], "huggingface_success"
        if isinstance(data, list):
            first = data[0]
            if isinstance(first, dict) and "generated_text" in first:
                return first["generated_text"], "huggingface_success"
            if isinstance(first, str):
                return first, "huggingface_success"
        if isinstance(data, str):
            return data, "huggingface_success"
        return r.text, "huggingface_success"
    except Exception as e:
        return "", f"hf-parse-error:{e}"

# ------------- public entrypoint -------------
def generate_ai_json(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Returns model text or JSON string. Tries:
      1) cached new OpenAI client
      2) live OpenAI new client (retries)
      3) Hugging Face fallback
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
                try:
                    _invoke_openai_cached.cache_clear()
                    _invoke_openai_cached(prompt_str, OPENAI_MODEL)
                except Exception:
                    pass
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

    # OpenAI new-client failed -> try HuggingFace fallback
    logger.info("OpenAI new-client attempts exhausted; trying Hugging Face fallback. reason=%s", last_reason)
    hf_text, hf_reason = _call_huggingface(prompt_str)
    if hf_text:
        logger.info("Hugging Face fallback succeeded: %s", hf_reason)
        return hf_text

    logger.warning("Hugging Face fallback failed reason=%s; returning deterministic stub.", hf_reason)
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
