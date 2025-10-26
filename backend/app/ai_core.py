# backend/app/ai_core.py
"""
High-level AI core that:
- calls services.openai_service.generate_ai_json (sync) via asyncio.to_thread
- extracts JSON blob if necessary
- validates expected keys, normalizes types and returns mapping of strings
- if parsing fails or values missing, returns safe fallback texts
"""

import json
import logging
from typing import Dict, Any
import asyncio

from backend.app.services.openai_service import generate_ai_json

logger = logging.getLogger(__name__)

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
    """Extract first top-level JSON object from possibly noisy text."""
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
            if stack:
                stack.pop()
            if not stack:
                return text[start:i+1]
    last = text.rfind("}")
    if last != -1 and last > start:
        return text[start:last+1]
    return ""


def _safe_stringify(value: Any) -> str:
    """Convert lists/dicts to readable string if returned; ensure no None."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


async def generate_ai_sections(proposal: Dict[str, Any], tone: str = "Formal") -> Dict[str, str]:
    """
    Ask the OpenAI service to generate JSON and return normalized dict of strings.
    Uses asyncio.to_thread to call the synchronous service without blocking event loop.
    """
    try:
        raw = await asyncio.to_thread(generate_ai_json, proposal, tone)
    except Exception as e:
        logger.exception("OpenAI service failed to run: %s", e)
        # return safe defaults
        return await generate_ai_sections_safe(proposal)

    if not raw:
        logger.warning("OpenAI returned empty response; using safe fallback")
        return await generate_ai_sections_safe(proposal)

    # Extract JSON object if model returned commentary + JSON
    json_blob = _extract_json_blob(raw)
    parsed = None
    if json_blob:
        try:
            parsed = json.loads(json_blob)
        except Exception as e:
            logger.exception("Failed to parse extracted JSON blob: %s; raw: %s", e, json_blob)
            parsed = None
    else:
        # maybe raw is pure JSON
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None

    if not isinstance(parsed, dict):
        logger.warning("Could not parse AI output as JSON dict; using fallback executive summary and safe defaults.")
        # return raw in executive_summary_text so user has something to review
        executive = raw.strip()[:4000]
        return {
            **{k: "" for k in EXPECTED_KEYS},
            "executive_summary_text": executive
        }

    # Normalize result: ensure all EXPECTED_KEYS present and converted to strings
    result: Dict[str, str] = {}
    for k in EXPECTED_KEYS:
        v = parsed.get(k, "")
        result[k] = _safe_stringify(v)

    return result


async def generate_ai_sections_safe(proposal: Dict[str, Any]) -> Dict[str, str]:
    """Return developer-friendly safe texts (used as fallback when OpenAI fails)."""
    client = str(proposal.get("client_name", "") or "Client")
    # keep these concise — they will be inserted into docx template
    safe = {
        "executive_summary_text": f"This proposal for {client} outlines a phased plan to achieve the client's objectives. (Auto-generated fallback.)",
        "project_mission_text": "Deliver a reliable, maintainable solution that provides measurable business value.",
        "solution_concept_text": "We propose a pragmatic architecture using modular services and reliable third-party platforms.",
        "project_methodology_text": "Agile with two-week sprints, continuous integration, automated testing, and regular demos.",
        "financial_justification_text": "Expected benefits and efficiency gains justify the investment; detailed ROI analysis available on request.",
        "payment_terms_text": "50% upfront, 50% upon final delivery. This proposal is valid for 30 days.",
        "development_note": "Estimate includes development, QA, and DevOps support.",
        "licenses_note": "Includes typical SaaS and hosting licenses required for the solution.",
        "support_note": "Includes post-launch support and critical fixes for a limited period."
    }
    return safe
