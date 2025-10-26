# backend/app/ai_core.py
import asyncio
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# Async stub — in production replace with actual OpenAI calls (async)
async def generate_ai_sections_safe(proposal: Dict[str, Any], timeout: float = 15.0) -> Dict[str, str]:
    """
    Robust wrapper that attempts to generate AI sections. This is a stub for development.
    It returns sane fallback texts on any error or timeout.
    Replace with a real OpenAI implementation when ready.
    """
    async def _stub():
        # Create plausible text pieces based on incoming proposal
        client = proposal.get("client_name", "Client")
        goal = proposal.get("project_goal", "the project")
        return {
            "executive_summary_text": f"This proposal for {client} outlines a plan to {goal}.",
            "project_mission_text": "Project mission: deliver measurable value and reliable software.",
            "solution_concept_text": "Proposed solution: modular services and integration layers tailored to the client's environment.",
            "project_methodology_text": "We will follow an Agile approach with two-week sprints and continuous demos.",
            "financial_justification_text": "Investment is justified by projected revenue uplift and operational savings.",
            "payment_terms_text": "Standard payment: 50% upfront, 50% upon final delivery.",
            "development_note": "Development estimate includes senior and mid-level engineering resources.",
            "licenses_note": "Licenses include required 3rd-party SaaS and hosting costs.",
            "support_note": "Includes 3 months of post-launch support."
        }

    try:
        result = await asyncio.wait_for(_stub(), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        logger.exception("AI core timeout — returning fallback texts")
        return {
            "executive_summary_text": "Executive summary temporarily unavailable.",
            "project_mission_text": "",
            "solution_concept_text": "",
            "project_methodology_text": "",
            "financial_justification_text": "",
            "payment_terms_text": "",
            "development_note": "",
            "licenses_note": "",
            "support_note": ""
        }
    except Exception:
        logger.exception("AI core failed — returning fallback texts")
        return {
            "executive_summary_text": "Executive summary temporarily unavailable.",
            "project_mission_text": "",
            "solution_concept_text": "",
            "project_methodology_text": "",
            "financial_justification_text": "",
            "payment_terms_text": "",
            "development_note": "",
            "licenses_note": "",
            "support_note": ""
        }
