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
# FIX 1: Используем JSON-совместимую модель по умолчанию
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.3-turbo-0125") 
OPENAI_FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", OPENAI_MODEL)
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "2048"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))
OPENAI_REQUEST_TIMEOUT = int(os.getenv("OPENAI_REQUEST_TIMEOUT", "30"))
OPENAI_RETRY_ATTEMPTS = int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3"))
OPENAI_RETRY_BACKOFF_BASE = float(os.getenv("OPENAI_RETRY_BACKOFF_BASE", "1.0"))
OPENAI_USE_STUB = os.getenv("OPENAI_USE_STUB", "0").lower() in ("1", "true", "yes")

# Gemini (Google AI) fallback
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash") # Используем быструю модель

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
    """Prompt asking for full JSON: textual sections, suggestions, visualization structure."""
    client = proposal.get("client_company_name") or proposal.get("client_name") or ""
    provider = proposal.get("provider_company_name") or proposal.get("provider_name") or ""
    project_goal = proposal.get("project_goal", "")
    scope = proposal.get("scope", "")
    technologies = proposal.get("technologies") or []
    techs = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)
    deadline = proposal.get("deadline", "")

    prompt = f"""
You are an expert commercial proposal writer AND a systems architect / delivery manager.
Return EXACTLY ONE VALID JSON OBJECT and NOTHING ELSE. The JSON must have the following keys:

- executive_summary_text (string)
- project_mission_text (string)
- solution_concept_text (string)
- project_methodology_text (string)
- financial_justification_text (string)
- payment_terms_text (string)
- development_note (string)
- licenses_note (string)
- support_note (string)

- suggested_deliverables: array of objects {{ "title","description","acceptance" }}
- suggested_phases: array of objects {{ "phase_name","duration_weeks","tasks" }}

- visualization: object containing:
    components: [{{id, title, description, type (service|db|ui|other), depends_on: [ids]}}]
    infrastructure: [{{node, label}}]
    data_flows: [{{from, to, label}}]    # logical flows (DFD style)
    connections: [{{from, to, label}}]   # infra-level network links
    milestones: [{{name, start (YYYY-MM-DD|null), end (YYYY-MM-DD|null), duration_days (int|null)}}]

Input brief:
client_name: "{client}"
provider_name: "{provider}"
project_goal: "{project_goal}"
scope: "{scope}"
technologies: "{techs}"
deadline: "{deadline}"
tone: "{tone}"

Guidelines:
- For the textual fields: write concise, professional sentences (2-6 sentences each).
- For suggested_deliverables and suggested_phases: be concrete and numbered (realistic durations).
- For visualization: produce a concrete list of components, infra nodes, data flows and milestones so diagrams can be built automatically.
- Use ISO dates YYYY-MM-DD for start/end where possible; if unknown, set start or end to null.
- Do NOT include any keys besides the ones listed above.
- Return only valid JSON. If unsure, include empty arrays or nulls rather than free text.

Produce JSON only.
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

# Note: _extract_json_blob is not strictly needed for JSON Mode, but it was 
# present in the original code snippet (though not fully included here). 
# We rely on JSON Mode for clean output.

# ------------- OpenAI: NEW client only -------------
def _call_openai_new_client(prompt_str: str, model_name: str) -> str:
    """
    Use only new openai.OpenAI() client. If not available or fails, raise exception.
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
        # FIX 2: Добавляем response_format для активации JSON Mode
        json_format = {"type": "json_object"} 
        
        try:
            resp = create_fn(
                model=model_name, 
                messages=messages, 
                max_tokens=OPENAI_MAX_TOKENS, 
                temperature=OPENAI_TEMPERATURE, 
                request_timeout=OPENAI_REQUEST_TIMEOUT,
                response_format=json_format # КЛЮЧЕВОЕ ДОБАВЛЕНИЕ
            )
        except TypeError:
            # Fallback (если request_timeout не поддерживается, 
            # что маловероятно для новых клиентов)
            resp = create_fn(
                model=model_name, 
                messages=messages, 
                max_tokens=OPENAI_MAX_TOKENS, 
                temperature=OPENAI_TEMPERATURE,
                response_format=json_format # КЛЮЧЕВОЕ ДОБАВЛЕНИЕ
            )

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
    if OPENAI_USE_STUB:
        # create a deterministic stub compatible with schema (minimal)
        client = proposal.get("client_company_name", "Client")
        stub = {
            "executive_summary_text": f"Fallback executive summary for {client}.",
            "project_mission_text": "Deliver a reliable solution.",
            "solution_concept_text": "Modular microservices architecture.",
            "project_methodology_text": "Agile with 2-week sprints.",
            "financial_justification_text": "ROI and efficiency gained.",
            "payment_terms_text": "50% upfront, 50% on delivery.",
            "development_note": "Covers development and QA.",
            "licenses_note": "Typical SaaS licenses.",
            "support_note": "3 months of post-launch support.",
            "suggested_deliverables": [],
            "suggested_phases": [],
            "visualization": {
                "components": [],
                "infrastructure": [],
                "data_flows": [],
                "connections": [],
                "milestones": []
            }
        }
        return json.dumps(stub, ensure_ascii=False)

    prompt = _build_prompt(proposal, tone)
    # try cached fast path
    try:
        cached = _invoke_openai_cached(prompt, OPENAI_MODEL)
        if cached:
            # try parse to be safe
            try:
                json.loads(cached)
                return cached
            except Exception:
                # not strict JSON, still use it as text
                return cached
    except Exception:
        pass

    last_exc = None
    for attempt in range(1, OPENAI_RETRY_ATTEMPTS + 1):
        try:
            res = _call_openai_new_client(prompt, OPENAI_MODEL)
            if res:
                return res
            last_exc = RuntimeError("empty response")
        except Exception as e:
            last_exc = e
            # если ошибка связана с отсутствием модели — сразу fallback
            if "model_not_found" in str(e).lower() or "does not exist" in str(e).lower():
                logger.warning("OpenAI model not found (%s), switching to Gemini fallback", OPENAI_MODEL)
                break
            if attempt < OPENAI_RETRY_ATTEMPTS:
                backoff = OPENAI_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                time.sleep(backoff + random.random() * 0.5)
            continue


    # Gemini fallback
    gemini_text, gemini_reason = _call_gemini(prompt)
    if gemini_text:
        return gemini_text

    # final stub
    client = proposal.get("client_company_name", "Client")
    stub = {
        "executive_summary_text": f"Fallback executive summary for {client}.",
        "project_mission_text": "Deliver a reliable solution.",
        "solution_concept_text": "Modular microservices architecture.",
        "project_methodology_text": "Agile with 2-week sprints.",
        "financial_justification_text": "ROI and efficiency gained.",
        "payment_terms_text": "50% upfront, 50% on delivery.",
        "development_note": "Covers development and QA.",
        "licenses_note": "Typical SaaS licenses.",
        "support_note": "3 months of post-launch support.",
        "suggested_deliverables": [],
        "suggested_phases": [],
        "visualization": {"components": [], "infrastructure": [], "data_flows": [], "connections": [], "milestones": []}
    }
    return json.dumps(stub, ensure_ascii=False)


# ----------------------------------------------
# Suggestion generation: targeted prompts for deliverables/phases (returns dict)
# ----------------------------------------------
def _build_suggestion_prompt(proposal: Dict[str, Any], tone: str = "Formal", max_deliverables: int = 8, max_phases: int = 8) -> str:
    """
    Build a focused prompt asking the LLM to propose deliverables and phases.
    Returns bilingual prompt (EN short instructions) to improve locality.
    The model MUST return exactly ONE JSON object and NOTHING ELSE.
    """
    client = proposal.get("client_name", "")
    project_goal = proposal.get("project_goal", "")
    scope = proposal.get("scope", "")
    technologies = proposal.get("technologies") or []
    techs = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)

    prompt = f"""
You are an experienced delivery/project manager and proposal writer.


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
                blob = cached if isinstance(cached, str) else str(cached)
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    return {
                        "suggested_deliverables": parsed.get("suggested_deliverables", []),
                        "suggested_phases": parsed.get("suggested_phases", [])
                    }
            except Exception:
                pass
    except Exception:
        pass

    # Live attempts (retries) using new-client call function if available
    last_exc = None
    for attempt in range(1, OPENAI_RETRY_ATTEMPTS + 1):
        try:
            txt = _call_openai_new_client(prompt, OPENAI_MODEL)
            
            if not txt:
                last_exc = RuntimeError("empty response")
                continue
                
            # try parse clean JSON
            json_blob = txt if isinstance(txt, str) else str(txt)
            parsed = None
            try:
                parsed = json.loads(json_blob)
            except Exception:
                parsed = None

            if isinstance(parsed, dict):
                return {
                    "suggested_deliverables": parsed.get("suggested_deliverables", []),
                    "suggested_phases": parsed.get("suggested_phases", [])
                }
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
                parsed = None
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