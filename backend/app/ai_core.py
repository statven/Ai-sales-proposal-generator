# backend/app/ai_core.py
import json
import logging
from typing import Dict, Any, List, Tuple
import asyncio
import os
from datetime import date

# Prefer importing the sync helper from services; tests usually patch it.
try:
    from backend.app.services.openai_service import generate_ai_json
except Exception:
    # When services not available in test env, leave generate_ai_json undefined;
    # tests typically monkeypatch _call_model_async or backend.app.services.openai_service.generate_ai_json.
    generate_ai_json = None

logger = logging.getLogger("uvicorn.error")

EXPECTED_KEYS: List[str] = [
    "executive_summary_text",
    "project_mission_text",
    "solution_concept_text",
    "project_methodology_text",
    "financial_justification_text",
    "payment_terms_text",
    "development_note",
    "licenses_note",
    "support_note",
]


def _extract_json_blob(text: str) -> str:
    """
    Extract the first balanced JSON object substring from `text`.
    Skip template markers like '{{' to avoid grabbing docx template placeholders.
    Returns '' if no balanced JSON object is found.
    """
    if not text or not isinstance(text, str):
        return ""
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "{":
            # skip '{{' templating marker
            if i + 1 < n and text[i + 1] == "{":
                i += 2
                continue
            depth = 0
            start = i
            j = i
            while j < n:
                c = text[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start : j + 1]
                j += 1
            # unmatched open brace — advance one char and continue searching
            i = start + 1
        else:
            i += 1
    return ""


def _safe_stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


async def generate_ai_sections_safe(proposal: Dict[str, Any]) -> Dict[str, str]:
    """Return a set of safe fallback texts (non-blocking)."""
    client = str(proposal.get("client_name") or proposal.get("client_company_name") or "Client")
    safe = {
        "executive_summary_text": f"This proposal for {client} outlines a phased plan to meet the goals specified.",
        "project_mission_text": "Deliver a reliable, maintainable solution that provides measurable business value.",
        "solution_concept_text": "A modular services architecture with reliable third-party integrations.",
        "project_methodology_text": "Agile with two-week sprints, CI/CD, testing and regular demos.",
        "financial_justification_text": "Expected efficiency gains and revenue uplift justify the investment.",
        "payment_terms_text": "50% upfront, 50% on delivery. Valid for 30 days.",
        "development_note": "Estimate includes development, QA, and DevOps.",
        "licenses_note": "Includes required SaaS licenses and hosting.",
        "support_note": "Includes 3 months of post-launch support.",
    }
    return safe


async def _call_model_async(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Call the synchronous `generate_ai_json` in a threadpool to avoid blocking.
    Tests commonly monkeypatch backend.app.ai_core._call_model_async or backend.app.services.openai_service.generate_ai_json.
    """
    if generate_ai_json is None:
        # No service available; return empty string so caller falls back to safe texts.
        logger.debug("_call_model_async: generate_ai_json not available; returning empty string.")
        return ""
    try:
        # run sync function in threadpool
        res = await asyncio.to_thread(generate_ai_json, proposal, tone)
        # Ensure we return a string
        if isinstance(res, bytes):
            try:
                return res.decode("utf-8", errors="ignore")
            except Exception:
                return str(res)
        return str(res)
    except Exception as e:
        logger.exception("generate_ai_json raised exception: %s", e)
        # propagate to caller as empty string (caller handles fallback)
        return ""


async def generate_ai_sections(proposal: dict, tone: str = "Formal") -> dict:
    """
    Robust wrapper to get structured AI sections from an LLM.

    Strategy:
      1) Call model once (raw1). Try to extract JSON object from it (via _extract_json_blob).
      2) If parse succeeds -> return parsed dict.
      3) If parse fails -> put raw1 into executive_summary_text as fallback and call model again (regen).
      4) Try parse second response (raw2). If parse succeeds -> merge/return parsed dict,
         preserving raw1 executive summary if parsed2 lacks it.
      5) If both fail -> return dict with executive_summary_text set to raw1 + raw2 combined (or safe fallback).
    """
    def try_parse_string_to_dict(s: str) -> Dict[str, Any]:
        if not s or not isinstance(s, str):
            return {}
        # try to extract a JSON blob (first balanced {...})
        blob = _extract_json_blob(s)
        if blob:
            try:
                return json.loads(blob)
            except Exception:
                # fallback to trying raw s
                pass
        s_stripped = s.strip()
        if s_stripped.startswith("{") and s_stripped.endswith("}"):
            try:
                return json.loads(s_stripped)
            except Exception:
                return {}
        return {}

    def normalize_values(d: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in (d or {}).items():
            if v is None:
                out[k] = ""
            elif isinstance(v, (str, int, float, bool)):
                out[k] = v
            else:
                # keep nested structures as-is or stringify if necessary
                try:
                    out[k] = v
                except Exception:
                    out[k] = str(v)
        return out

    # 1) First call
    raw1 = ""
    try:
        raw1 = await _call_model_async(proposal, tone=tone)
        if raw1 is None:
            raw1 = ""
    except Exception as e:
        logger.exception("First AI call exception: %s", e)
        raw1 = ""

    # 2) Try parse raw1
    parsed1 = {}
    try:
        parsed1 = try_parse_string_to_dict(raw1)
    except Exception:
        parsed1 = {}

    if parsed1:
        return normalize_values(parsed1)

    # 3) First response unparsable: use as fallback executive_summary_text
    fallback_summary = raw1 or ""

    # 4) Second call (regeneration)
    raw2 = ""
    try:
        raw2 = await _call_model_async(proposal, tone=tone)
        if raw2 is None:
            raw2 = ""
    except Exception as e:
        logger.exception("Second AI call exception: %s", e)
        raw2 = ""

    parsed2 = {}
    try:
        parsed2 = try_parse_string_to_dict(raw2)
    except Exception:
        parsed2 = {}

    if parsed2:
        # ensure executive_summary_text present: prefer parsed2 value, otherwise use fallback_summary
        parsed2_norm = normalize_values(parsed2)
        if "executive_summary_text" not in parsed2_norm or not parsed2_norm.get("executive_summary_text"):
            parsed2_norm["executive_summary_text"] = fallback_summary
        return parsed2_norm

    # 5) Both failed -> combine texts into executive_summary_text (or use safe fallback)
    combined = fallback_summary
    if raw2:
        if combined:
            combined = (combined + "\n\n" + raw2).strip()
        else:
            combined = raw2.strip()
    if not combined:
        # no useful text from model -> return safe defaults
        safe = await generate_ai_sections_safe(proposal)
        return safe
    return {"executive_summary_text": combined}


async def process_ai_content(proposal: Dict[str, Any], tone: str = "Formal") -> Tuple[Dict[str, str], str]:
    """
    Thin orchestration wrapper expected by main/tests:
      - Calls generate_ai_sections
      - Returns (sections_dict, used_model_string)
    `used_model_string` is best-effort: if OPENAI_MODEL env var set use it, else 'openai' or 'fallback_safe'
    """
    used_model = os.getenv("OPENAI_MODEL") or "openai"
    try:
        sections = await generate_ai_sections(proposal, tone)
        # simple heuristic: if the executive_summary_text looks like our fallback wording, mark fallback
        exec_text = (sections.get("executive_summary_text") or "").lower() if isinstance(sections, dict) else ""
        if exec_text and ("this proposal for" in exec_text or "phased plan" in exec_text or "fallback" in exec_text):
            used_model = "fallback_safe"
        return sections, used_model
    except Exception as e:
        logger.exception("process_ai_content: AI generation failed: %s", e)
        safe = await generate_ai_sections_safe(proposal)
        return safe, "fallback_safe"
