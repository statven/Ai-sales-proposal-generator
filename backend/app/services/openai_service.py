"""
Low-level OpenAI service (synchronous, compatible с последней версией openai>=1.0.0)
"""

import os
import logging
from typing import Dict, Any
import openai

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY not set — OpenAI calls будут неуспешными")
openai.api_key = OPENAI_API_KEY

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1048"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.3"))

def _build_prompt(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    client = proposal.get("client_name", "")
    provider = proposal.get("provider_name", "")
    project_goal = proposal.get("project_goal", "")
    scope = proposal.get("scope", "")
    technologies = proposal.get("technologies") or []
    techs = ", ".join(technologies) if isinstance(technologies, list) else str(technologies)
    deadline = proposal.get("deadline", "")
    tone_instruction = "Use a formal, professional tone."

    prompt = f"""
You are a professional proposal writer. Given structured input, produce a JSON object only,
with exactly these keys:
- executive_summary_text
- project_mission_text
- solution_concept_text
- project_methodology_text
- financial_justification_text
- payment_terms_text
- development_note
- licenses_note
- support_note

Input:
- client_name: "{client}"
- provider_name: "{provider}"
- project_goal: "{project_goal}"
- scope: "{scope}"
- technologies: "{techs}"
- deadline: "{deadline}"
- tone: "{tone}"

Instruction:
{tone_instruction}
"""
    return prompt.strip()

def generate_ai_json(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Синхронный вызов OpenAI ChatCompletion
    """
    prompt = _build_prompt(proposal, tone)
    try:
        resp = openai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=OPENAI_MAX_TOKENS,
            temperature=OPENAI_TEMPERATURE
        )
        return resp.choices[0].message.content
    except Exception as e:
        logger.exception("OpenAI call failed")
        return ""
