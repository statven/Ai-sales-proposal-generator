# backend/app/ai_core.py
import json
import logging
from typing import Dict, Any, List
import asyncio
import copy
from datetime import datetime

from backend.app.services.openai_service import generate_ai_json

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
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


async def generate_ai_sections_safe(proposal: Dict[str, Any]) -> Dict[str, str]:
    """Return developer-friendly safe texts (used as fallback)."""
    client = str(proposal.get("client_name", "") or "Client")
    safe = {
        "executive_summary_text": f"This proposal for {client} outlines a phased plan to meet the goals specified.",
        "project_mission_text": "Deliver a reliable, maintainable solution that provides measurable business value.",
        "solution_concept_text": "A modular services architecture with reliable third-party integrations.",
        "project_methodology_text": "Agile with two-week sprints, CI/CD, testing and regular demos.",
        "financial_justification_text": "Expected efficiency gains and revenue uplift justify the investment.",
        "payment_terms_text": "50% upfront, 50% on delivery. Valid for 30 days.",
        "development_note": "Estimate includes development, QA, and DevOps.",
        "licenses_note": "Includes required SaaS licenses and hosting.",
        "support_note": "Includes 3 months of post-launch support."
    }
    return safe


async def _call_model_async(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Call the synchronous generate_ai_json in a threadpool.
    """
    try:
        return await asyncio.to_thread(generate_ai_json, proposal, tone)
    except Exception as e:
        logger.exception("generate_ai_json raised exception: %s", e)
        raise


async def generate_ai_sections(proposal: Dict[str, Any], tone: str = "Formal") -> Dict[str, str]:
    """
    Primary entrypoint:
    - call model once,
    - parse JSON,
    - if some EXPECTED_KEYS are missing/empty, perform a targeted regenerate for missing keys,
      then merge results.
    - If anything fails, return safe defaults.
    """
    # first attempt
    try:
        raw = await _call_model_async(proposal, tone)
    except Exception as e:
        logger.exception("OpenAI service failed to run: %s", e)
        return await generate_ai_sections_safe(proposal)

    if not raw:
        logger.warning("OpenAI returned empty response; using safe fallback")
        return await generate_ai_sections_safe(proposal)

    # parse JSON blob from model output
    parsed = None
    json_blob = _extract_json_blob(raw)
    if json_blob:
        try:
            parsed = json.loads(json_blob)
        except Exception as e:
            logger.exception("Failed to parse extracted JSON blob: %s; raw blob: %s", e, json_blob)
            parsed = None
    else:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None

    # If not dict, return raw in executive_summary_text so user can inspect
    if not isinstance(parsed, dict):
        logger.warning("Could not parse AI output as JSON dict; returning raw in executive_summary_text.")
        exec_text = (raw.strip() or "")[:4000]
        out = {k: "" for k in EXPECTED_KEYS}
        out["executive_summary_text"] = exec_text
        return out

    # Normalize initial results
    result: Dict[str, str] = {}
    for k in EXPECTED_KEYS:
        result[k] = _safe_stringify(parsed.get(k, ""))

    # Find missing / empty keys
    missing_keys = [k for k, v in result.items() if not v or str(v).strip() == ""]
    if not missing_keys:
        # All good
        return result

    # Prepare a targeted regenerate: instruct model to return ONLY missing keys as JSON
    try:
        logger.info("Regenerating missing keys from LLM: %s", ", ".join(missing_keys))
        # Make a shallow copy of proposal and append instruction to scope so openai_service._build_prompt sees it
        repair_proposal = copy.deepcopy(proposal)
        repair_instruction = (
            "\n\n-- REGENERATION REQUEST --\n"
            "The previous response left some fields empty. Please RETURN ONLY A SINGLE VALID JSON OBJECT "
            "that contains exactly the following keys and their string values: "
            f"{', '.join(missing_keys)}. Do NOT include any other keys. "
            "Each value should be 1-4 concise sentences. If you cannot produce a value, set it to an empty string \"\".\n"
        )
        # Append to scope (safe place) — openai_service uses scope in prompt
        old_scope = repair_proposal.get("scope", "") or ""
        repair_proposal["scope"] = f"{old_scope}\n{repair_instruction}"

        # call model again (same tone)
        raw2 = await _call_model_async(repair_proposal, tone)
        if not raw2:
            logger.warning("Regeneration call returned empty; skipping merge.")
            return result

        # parse second JSON
        blob2 = _extract_json_blob(raw2)
        parsed2 = None
        if blob2:
            try:
                parsed2 = json.loads(blob2)
            except Exception as e:
                logger.exception("Failed to parse fallback JSON blob: %s; raw: %s", e, blob2)
                parsed2 = None
        else:
            try:
                parsed2 = json.loads(raw2)
            except Exception:
                parsed2 = None

        if isinstance(parsed2, dict):
            # Merge: keep existing non-empty values, fill missing from parsed2
            for k in missing_keys:
                v2 = parsed2.get(k, "")
                if v2 and str(v2).strip():
                    result[k] = _safe_stringify(v2)
            # recompute remaining missing
            remaining = [k for k, v in result.items() if not v or str(v).strip() == ""]
            if remaining:
                logger.warning("After regeneration, still missing keys: %s", remaining)
        else:
            logger.warning("Regeneration call did not return JSON dict; raw2 kept as fallback in executive_summary")
            # incorporate raw2 into executive_summary_text if exec was empty
            if not result.get("executive_summary_text"):
                result["executive_summary_text"] = (raw2.strip() or "")[:4000]
    except Exception as e:
        logger.exception("Regeneration attempt failed: %s", e)

    return result
