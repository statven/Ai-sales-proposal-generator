"""
High-level AI core: calls OpenAI service, parses JSON, validates keys,
returns dict of strings for template. Fallbacks to safe defaults if errors.
"""

import json
import logging
from typing import Dict, Any
import asyncio

from backend.app.services.openai_service import generate_ai_json  # синхронный метод

logger = logging.getLogger(__name__)

# Expected keys from AI JSON
EXPECTED_KEYS = [
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
    """Попытка извлечь JSON-объект из текста, если модель добавила комментарий"""
    if not text:
        return ""
    start = text.find("{")
    if start == -1:
        return ""
    stack = []
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            stack.append(i)
        elif ch == "}":
            stack.pop()
            if not stack:
                return text[start:i+1]
    last = text.rfind("}")
    if last != -1 and last > start:
        return text[start:last+1]
    return ""

async def generate_ai_sections(proposal: Dict[str, Any], tone: str = "Formal") -> Dict[str, str]:
    """
    High-level wrapper:
    - call OpenAI
    - parse JSON
    - ensure all expected keys exist
    """
    try:
        # Асинхронно вызываем синхронный generate_ai_json через поток
        raw = await asyncio.to_thread(generate_ai_json, proposal, tone)
    except Exception as e:
        logger.exception("OpenAI call failed: %s", str(e))
        return {k: "" for k in EXPECTED_KEYS}

    json_blob = _extract_json_blob(raw)
    parsed = {}
    if not json_blob:
        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning("Cannot parse AI output, fallback to raw executive_summary_text")
            return {**{k: "" for k in EXPECTED_KEYS}, "executive_summary_text": raw.strip()[:4000]}
    else:
        try:
            parsed = json.loads(json_blob)
        except Exception as e:
            logger.exception("JSON parse error: %s; raw blob: %s", str(e), json_blob)
            return {**{k: "" for k in EXPECTED_KEYS}, "executive_summary_text": raw.strip()[:4000]}

    # Normalize results
    result: Dict[str, str] = {}
    for k in EXPECTED_KEYS:
        v = parsed.get(k, "")
        if isinstance(v, (list, dict)):
            try:
                v = json.dumps(v, ensure_ascii=False)
            except Exception:
                v = str(v)
        if v is None:
            v = ""
        result[k] = str(v).strip()

    return result

async def generate_ai_sections_safe(proposal: Dict[str, Any]) -> Dict[str, str]:
    """Fallback safe AI generator: returns empty strings"""
    return {k: "" for k in EXPECTED_KEYS}
