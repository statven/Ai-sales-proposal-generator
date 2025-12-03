# backend/app/services/openai_service.py
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
from typing import Dict, Any, Tuple, Optional, List
from datetime import date, datetime, timedelta
import math
from datetime import timedelta as _td

from functools import lru_cache, wraps
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo-0125") 
OPENAI_FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", OPENAI_MODEL)
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1000"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))
OPENAI_REQUEST_TIMEOUT = int(os.getenv("OPENAI_REQUEST_TIMEOUT", "30"))
OPENAI_RETRY_ATTEMPTS = int(os.getenv("OPENAI_RETRY_ATTEMPTS", "1"))
OPENAI_RETRY_BACKOFF_BASE = float(os.getenv("OPENAI_RETRY_BACKOFF_BASE", "1.0"))
OPENAI_USE_STUB = os.getenv("OPENAI_USE_STUB", "0").lower() in ("1", "true", "yes")

# Gemini (Google AI) fallback
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash") 

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
import json
import math
import logging
from datetime import date, datetime
from string import Template
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _build_prompt(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Rewritten build_prompt: strong, defensive, structured, and agent-friendly.
    - Requires the model to return a single JSON object (strict schema) only.
    - Forces structured output: team_structure (roles+skills), phases (preferred_role, required_skills, effort_hours), visualization, metadata.
    - Contains strict adaptation algorithm, risk rules, API rate-limit verification steps, and audience-aware presentation rules.
    - Uses Template.safe_substitute for safe insertion of computed values.
    - NEVER raises: always returns a non-empty prompt string (fallback minimal prompt on failure).
    """
    import json
    import math
    from datetime import date, datetime
    from string import Template
    from textwrap import dedent
    import logging
    try:
        logger = logging.getLogger(__name__)
    except Exception:
        class _FakeLogger:
            def exception(self, *a, **k): pass
            def warning(self, *a, **k): pass
            def info(self, *a, **k): pass
        logger = _FakeLogger()
    # --- helpers ---
    def safe_get(d, *keys, default=None):
        try:
            for k in keys:
                if isinstance(d, dict) and k in d:
                    v = d[k]
                    if v is not None:
                        return v
        except Exception:
            pass
        return default
    def as_safe_str(v, default=""):
        try:
            if v is None:
                return default
            if isinstance(v, (date, datetime)):
                return v.strftime("%Y-%m-%d")
            if isinstance(v, (list, dict, tuple)):
                return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            s = str(v)
            s = s.replace("\r", " ").replace("\n", " ").strip()
            return " ".join(s.split())
        except Exception:
            return default
    def as_safe_json_str(v, default="[]"):
        try:
            if v is None:
                return default
            if isinstance(v, str) and not v.strip():
                return default
            j = json.dumps(v, indent=2, ensure_ascii=False)
            return j.replace("\n", "\\n")
        except Exception:
            try:
                return json.dumps(default)
            except Exception:
                return default
    def safe_int(v, default=None):
        try:
            if v is None:
                return default
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, (int,)):
                return int(v)
            if isinstance(v, float):
                return int(math.ceil(v))
            s = str(v).strip()
            if s == "":
                return default
            if "." in s:
                return int(math.ceil(float(s)))
            return int(s)
        except Exception:
            return default
    # --- extract core fields with fallbacks ---
    client = as_safe_str(safe_get(proposal, "client_company_name", "client_name"), "the Client")
    provider = as_safe_str(safe_get(proposal, "provider_company_name", "provider_name"), "the Provider")
    project_goal = as_safe_str(safe_get(proposal, "project_goal", "goal", default=""))
    scope = as_safe_str(safe_get(proposal, "scope", "description", default=""))
    technologies_field = safe_get(proposal, "technologies", "tech", default=[])
    if isinstance(technologies_field, (list, tuple)):
        techs = ", ".join(as_safe_str(x) for x in technologies_field)
    else:
        techs = as_safe_str(technologies_field, "")
    # audience: executive|technical_lead|mixed
    audience = as_safe_str(safe_get(proposal, "audience"), "mixed")
    if audience not in ("executive", "technical_lead", "mixed"):
        audience = "mixed"
    # deliverables & phases provided
    user_deliverables = safe_get(proposal, "deliverables", "suggested_deliverables", default=[])
    user_phases = safe_get(proposal, "phases", "suggested_phases", default=[])
    deliverables_input_str = as_safe_json_str(user_deliverables, default="[]")
    phases_input_str = as_safe_json_str(user_phases, default="[]")
    team_size = safe_int(proposal.get("team_size"), default=1)
    
    if team_size >= 4:
        team_composition_hint = f"REQUIRED: The team size is {team_size}. You MUST define exactly {team_size} distinct roles (e.g., Project Manager, Solution Architect, Senior Backend Dev, Frontend Dev, QA Engineer, DevOps). Do NOT suggest a single-person team."
    elif team_size > 1:
        team_composition_hint = f"REQUIRED: The team size is {team_size}. Define distinct roles for these {team_size} people."
    else:
        team_composition_hint = "The team size is 1 (Single Project Lead mode)."
    # reality check extraction (if helper exists use it)
    rc = {}
    try:
        extract_fn = globals().get("_extract_reality_check", None)
        if callable(extract_fn):
            rc = extract_fn(proposal) or {}
    except Exception:
        rc = {}
    if not rc:
        raw_rc = safe_get(proposal, "reality_check", default={})
        if isinstance(raw_rc, dict):
            rc = raw_rc
    rc_planned = safe_int(rc.get("planned_effort_hours") or rc.get("planned_hours"), default=None)
    rc_capacity = safe_int(rc.get("team_capacity_hours") or rc.get("capacity_hours"), default=None)
    rc_allow_overflow = bool(rc.get("allow_overflow")) if rc.get("allow_overflow") is not None else False
    rc_requested_extension = safe_int(rc.get("requested_deadline_extension_days"), default=None)
    # team size: MUST use explicit if present; else fallback to 1
    team_size = safe_int(safe_get(proposal, "team_size"), default=1)
    if team_size is None or team_size <= 0:
        team_size = 1
    # compute capacity fallback from deadline if rc_capacity not provided
    computed_capacity = None
    try:
        if rc_capacity is not None:
            computed_capacity = int(rc_capacity)
        else:
            compute_fn = globals().get("_compute_capacity_from_deadline", None)
            if callable(compute_fn):
                try:
                    res = compute_fn(proposal)
                    if isinstance(res, (int, float)):
                        computed_capacity = int(res)
                except Exception:
                    computed_capacity = None
            else:
                deadline_raw = safe_get(proposal, "deadline", "deadline_iso", "deadline_date", default=None)
                if deadline_raw:
                    try:
                        if isinstance(deadline_raw, (date, datetime)):
                            dd = deadline_raw if isinstance(deadline_raw, date) else deadline_raw.date()
                        else:
                            dd = datetime.strptime(str(deadline_raw), "%Y-%m-%d").date()
                        today = date.today()
                        if dd > today:
                            days = (dd - today).days
                            work_days = max(0, math.floor(days * (5.0/7.0)))
                            computed_capacity = int(max(8, work_days * 8 * max(1, team_size))) if work_days > 0 else 0
                        else:
                            computed_capacity = 0
                    except Exception:
                        computed_capacity = None
    except Exception:
        computed_capacity = None
    capacity_hours_available = computed_capacity if isinstance(computed_capacity, int) else None
    # work_days used for add_fte formula
    work_days = None
    try:
        deadline_raw = safe_get(proposal, "deadline", "deadline_iso", "deadline_date", default=None)
        if deadline_raw:
            if isinstance(deadline_raw, (date, datetime)):
                dd = deadline_raw if isinstance(deadline_raw, date) else deadline_raw.date()
            else:
                dd = datetime.strptime(str(deadline_raw), "%Y-%m-%d").date()
            today = date.today()
            if dd > today:
                days = (dd - today).days
                work_days = max(0, math.floor(days * (5.0/7.0)))
            else:
                work_days = 0
    except Exception:
        work_days = None
    # thresholds
    too_short_threshold_h = 8
    preferred_must_phase_h = 40
    aggressive_compression_limit_pct = 30
    min_phase_hours = 4
    # mapping for Template
    mapping = {
        "client": client,
        "provider": provider,
        "project_goal": project_goal,
        "scope": scope,
        "techs": techs,
        "team_size": str(team_size),
        "team_composition_hint": team_composition_hint,
        "audience": audience,
        "deliverables_input_str": deliverables_input_str,
        "phases_input_str": phases_input_str,
        "rc_provided": "yes" if rc else "no",
        "rc_planned": str(rc_planned) if rc_planned is not None else "null",
        "rc_capacity": str(rc_capacity) if rc_capacity is not None else "null",
        "rc_allow_overflow": "true" if rc_allow_overflow else "false",
        "rc_requested_extension": str(rc_requested_extension) if rc_requested_extension is not None else "null",
        "capacity_hours_available": str(capacity_hours_available) if capacity_hours_available is not None else "null",
        "work_days": str(work_days) if work_days is not None else "null",
        "too_short_threshold_h": str(too_short_threshold_h),
        "preferred_must_phase_h": str(preferred_must_phase_h),
        "aggressive_compression_limit_pct": str(aggressive_compression_limit_pct),
        "min_phase_hours": str(min_phase_hours),
        "tone": as_safe_str(tone or "Formal")
    }
    # --- The new, strict prompt template ---
    prompt_template = dedent(r"""
YOU MUST RETURN ONLY VALID JSON. NO MARKDOWN. NO TEXT OUTSIDE JSON.
You are the Expert Committee for contractor "$provider", producing a Commercial Proposal for "$client".
Follow the rules and schema below exactly. Use conservative professional judgment. Do NOT ask clarifying questions — make reasonable assumptions and record them in assumptions_text.
Use a $tone tone in all narrative texts.
INPUT (use verbatim when present):
- project_goal: "$project_goal"
- scope: "$scope"
- tech_stack: "$techs"
- declared_team_size: $team_size
- audience: "$audience"
- user_provided_deliverables: $deliverables_input_str
- user_provided_phases: $phases_input_str
- reality_check_provided: $rc_provided
- reality_check.planned_effort_hours: $rc_planned
- reality_check.team_capacity_hours: $rc_capacity
- reality_check.allow_overflow: $rc_allow_overflow
- computed_capacity_hours: $capacity_hours_available
- computed_work_days: $work_days
- aggressive_compression_limit_pct: $aggressive_compression_limit_pct
IMPORTANT: ANSWER ONLY WITH A SINGLE VALID JSON OBJECT AND NOTHING ELSE. NO MARKDOWN, NO EXPLANATION, NO TEXT OUTSIDE THE JSON.
MUST-HAVE OUTPUT FORMAT (STRICT JSON):
Return EXACTLY one JSON object with these top-level keys in this order:
1) team_structure
2) suggested_phases
3) suggested_deliverables
4) visualization
5) risks_text
6) executive_summary_text (write 1-4 paragraphs)
7) project_mission_text (write 1-4 paragraphs)
8) all other narrative fields (technical_backend_text, technical_deployment_text, engagement_model_text, delivery_approach_text, team_structure_text, status_reporting_text, phases_summary_text, qa_strategy_text, qa_testing_types_text, financial_justification_text, payment_terms_text, development_note, licenses_note, support_note, assumptions_text)
9) metadata
*** GLOBAL CONSTRAINTS & RULES (ABSOLUTE) ***
                             # В prompt_parts, перед "ADDITIONAL GUIDANCE:", добавить:
"IMPORTANT: Do not include the client or provider company name (or any signatures, footers, or repeated mentions) at the end of any text field. The template handles company information separately. Keep all text fields clean and focused on content only."
- YOU MUST RETURN VALID JSON ONLY. No Markdown, no commentary.
- team_structure MUST exactly describe real team members and roles and MUST match declared_team_size:
   - If team_size < logical roles, fill extra slots with explicit placeholders: { "name": "TBD", "role":"TBD", "skills":[], "seniority":"junior" }.
   - Do NOT invent extra named roles beyond team_size.
- All arrays required by schema MUST be JSON arrays; do not output text bullets or markdown.
- All role names referenced in suggested_phases.preferred_role MUST be exact matches to team_structure.role values.
- milestones and phases_list (if both used) MUST be identical arrays. If trimmed, add trimmed names to metadata.dropped_phases.
- If any numeric input is missing, set corresponding numeric outputs to null and list missing items in assumptions_text.
- Ensure all sections are consistent with team_structure, suggested_phases, and metadata. No contradictions or hallucinations. Phase counts must match across phases_summary_text and suggested_phases; consolidate to <=8 phases if necessary, documenting in assumptions_text.
- Incorporate best practices from professional IT proposals, such as client-centric language, value propositions, and clear call to action in executive_summary_text. Avoid repetitive insertions like client name at section ends.
- Ensure financial sections are consistent and realistic; do not invent costs—use "estimated based on assumptions" if needed.
-
=== REQUIRED: team_structure ===
Return an array `team_structure` of length == declared_team_size ($team_size).
Each element MUST be an object with exactly these keys:
{
  "member_id": str,           # unique id (e.g., "pm-1" or "u123") OR null if unknown
  "role": str,                # canonical role name (e.g., "Project Manager")
  "skills": [str,...],        # non-empty array; tokens (e.g., "api","shopify","ci/cd")
  "seniority": "junior|mid|senior|lead",
  "capacity_hours_per_week": int|null   # optional but preferred
}
- Roles must be concise names (Project Manager, Solution Architect, Backend Engineer, QA Engineer, DevOps, Frontend Engineer).
- Skills must be normalized tokens (lowercase, no spaces preferred; use hyphen if needed).
- If a field is unknown, use null; however role must be provided.
=== REQUIRED: suggested_phases ===
- Return `suggested_phases` as an ARRAY (len <= 8). Each element MUST exactly contain:
{
  "phase_name": str,
  "description": str,
  "tasks": str,
  "effort_hours": int,            # canonical man-hours for the phase (integer)
  "duration_weeks": int|null,     # duration consistent with effort_hours (see CONSISTENCY RULES)
  "start": str|null,              # ISO date (YYYY-MM-DD) or null
  "end": str|null,                # ISO date or null
  "original_hours": int|null,     # if adjusted; else null
  "preferred_role": str,          # MUST match one of team_structure.role exactly
  "required_skills": [str,...],   # non-empty array used for role mapping
  "depends_on": [str,...],        # names of other phases (phase_name strings)
  "priority": "must|should|optional",
  "owner_member_id": str|null     # assigned owner member_id (if assigned by LLM); else null
}
- effort_hours must be integer >= 8 unless justified in assumptions_text.
- duration_weeks, if present, MUST be consistent with effort_hours (duration_weeks * team_size * 40 should approximate effort_hours — see CONSISTENCY RULES).
- preferred_role MUST be one of roles from team_structure.
- required_skills MUST be specific skill tokens (e.g., ["api","openapi","data-mapping"]).
=== PHASES DUPLICATION RULE ===
- Also include `phases_list` field as an EXACT COPY of suggested_phases (for backward compatibility): phases_list == suggested_phases.
=== REQUIRED: suggested_deliverables ===
- Array length <= 12. Each deliverable:
{ "title": str, "description": str, "acceptance_criteria": str }
=== REQUIRED: visualization ===
- Object with keys:
{
  "components": [...],
  "milestones": [ { "name":str, "start":str|null, "end":str|null, "owner_role":str, "owner_member_id": str|null } , ... ],
  "infrastructure": [...],
  "data_flows": [...],
  "connections": [...],
  "gantt_team_members": [ { "member_id":str, "name":str, "role":str } , ... ]   # MUST contain exact list of team_structure members
  "uml_structure": {  # REQUIRED: Generate UML for technical architecture
    "components": [  # List of dicts for system components
      { "id": str, "name": str, "stereotype": str, "responsibilities": [str,...], "attributes": [str,...], "notes": str }
    ],
    "relations": [  # List of dicts for connections
      { "from": str, "to": str, "type": str, "label": str }
    ]
  }
}
- Generate uml_structure based on tech_stack, scope, and suggested_phases. Use 8-12 components (e.g., API, Database, Workers). Relations as dependencies (type: "dependency"). Stereotypes: service, database, worker, etc.
- If start/end unknown, set them to null. Use effort_hours as canonical sizing.
- `gantt_team_members` MUST include every member from `team_structure` in the same order; do not omit.
- For diagrams (e.g., lifecycle, UML), describe them in relevant narrative texts if not generating visuals.
"- For UML, generate 'uml_structure' with 'components' (list of dicts: id, name, stereotype, responsibilities, attributes, notes) and 'relations' (list of dicts: from, to, type, label)."
=== RISKS ===
- Provide risks_text grouped exactly as:
  "High Risks:\n- <bullet>\n\nMedium Risks:\n- <bullet>\n\nLow Risks:\n- <bullet>\n"
- Phase-level risks MUST appear under appropriate group first.
- Additionally produce metadata.phase_risks (only if any phases flagged) as array of objects:
  { "phase_name","duration_hours","risk_level","reason","likelihood","impact","mitigations","additional_hours_needed","recommended_action","affected_downstream_phases" }
=== CONSISTENCY & VALIDATION RULES (ENFORCED) ===
- effort_hours MUST be numeric integer; duration_weeks if present MUST satisfy:
    |effort_hours - (duration_weeks * declared_team_size * 40)| <= 0.25 * effort_hours
  Otherwise: set duration_weeks to null and document adjustment in assumptions_text.
- start/end date inference must respect dependencies: if A depends_on B then A.start >= B.end (if known). If conflict, set dates to null and document in metadata.conflicts.
- owner_member_id MUST correspond to a member in team_structure (member_id). If assignment impossible, owner_member_id must be null and mark recommendation in metadata.owner_unassigned_phases.
- preferred_role MUST be exactly one of the roles in team_structure.
- All strings must escape newlines as '\\n'.
=== ADAPTATION ALGORITHM (ENFORCED, run if baseline > capacity) ===
1) Baseline = SUM(suggested_phases[*].effort_hours) -> metadata.total_hours_realistic (integer).
2) Compare Baseline to capacity (prefer reality_check.team_capacity_hours if provided; else computed_capacity_hours).
3) If Baseline <= capacity -> metadata.deadline_feasible = true.
4) If Baseline > capacity:
   A) Attempt logical parallelization/re-sequencing (document calendar impact).
   B) Compress 'should' or 'optional' phases up to $aggressive_compression_limit_pct% each (document compression_pct per phase).
   C) Drop optional phases and add names to metadata.dropped_phases.
   D) If still > capacity:
      - If reality_check.allow_overflow is true: metadata.allow_overflow_used=true and propose overflow_plan (numeric hours & mitigations).
      - Else: choose ONE primary_recommendation from {extend_deadline, add_fte, compress_scope}. Provide numeric rationale.
   E) When recommending extend_deadline, compute:
      suggested_deadline_extension_days = ceil(overflow_hours / (team_size * 8))
   F) When recommending add_fte:
      additional_FTEs_required = ceil(overflow_hours / (work_days * 8)) if work_days known else ceil(overflow_hours / (20*8))
- All formulas and numeric results MUST appear in metadata (both formula string and numeric result).
=== AGENT-LOGIC & OWNER ASSIGNMENT RULES (STRICT) ===
- The agent must assign owner_member_id deterministically using:
   score = w_skill * skill_match_score + w_seniority * seniority_score + w_load * availability_score + w_affinity * role_affinity_score
- skill_match_score = normalized count of required_skills intersect member.skills (weighted by proficiency if present).
- seniority_score: map junior=0.5, mid=1.0, senior=1.2, lead=1.4.
- availability_score: uses capacity_hours_per_week and current load; if unknown assume neutral (1.0) but document in assumptions_text.
- role_affinity_score = 1.0 if member.role == preferred_role else 0.7 if related, else 0.5.
- Tie-breaker MUST be deterministic: use hash(phase_name + member_id) modulo to pick highest hashed id among equals (no randomness).
- Do NOT assign owners to roles not present in team_structure.
- If no member reaches a minimum score threshold (configurable default 0.5), set owner_member_id=null and mark in metadata.owner_unassigned_phases.
=== API RATE-LIMIT & VERIFICATION REQUIREMENTS ===
- Identify external APIs from tech_stack/scope. For each vendor provide:
  { "vendor": str, "value": int|"unknown", "unit": str, "confidence": "low|medium|high", "source_url": str|"unknown", "verification_steps": str, "operational_recommendation": str, "tariff_info": str|"unknown" }
- If unknown, include concrete curl examples and header names to check.
- Include tariff considerations (e.g., cost per API call, storage fees) in tariff_info and reference in financial_justification_text where applicable.
TEAM-SIZE RULES (CRITICAL & MANDATORY)
    * **CURRENT INPUT TEAM SIZE: $team_size FTEs.**
    * $team_composition_hint
    * If `team_size` > 1, section `team_structure_text` MUST list exactly $team_size distinct roles with specific responsibilities.
    * Do NOT output "Given the team_size of 1" if the input is $team_size.
    * Adjust the `delivery_approach_text` to reflect parallel work streams possible with $team_size people.
    * ALWAYS include a `what_if_6FTE` block in metadata (even if current size is 6, just reaffirm feasibility).
=== TECHNICAL SECTIONS & APPENDIX RULES ===
- Provide `technical_backend_text` (high-level) aligned to phases and team.
- If you include detailed numeric tuning (>3 numeric tuning parameters), move them into `appendix_technical_spec` and set metadata.appendix_present = true.
- `appendix_technical_spec` must be labeled "Technical Appendix — for engineering teams only".
- Be concrete and decisive in recommendations, do not use phrases like "for example" or "such as" for core components; choose one primary option and justify.
- Specify all necessary tools, setups, and configurations to ensure the solution works as expected for the client, evaluating options where relevant and selecting the optimal one with justification.
- Provide a concrete `technical_backend_text` focused on the project, including architecture descriptions, data flows, storage strategy, worker design, security, observability, deployment artifacts, CI/CD pipeline, and operational runbook, without code examples.
=== RISK & ASSUMPTIONS RULES ===
- assumptions_text must be client-facing, one per line.
- internal notes must go to metadata.internal_notes (if any).
- If any required numeric input is missing set numeric outputs to null and list missing items in assumptions_text.
=== FINAL VALIDATION (must be satisfied by your answer) ===
- metadata.total_hours_realistic == SUM(suggested_phases[*].effort_hours) (integers). If coercion applied, document in metadata.risk_message and assumptions_text.
- len(suggested_phases) <= 8; len(suggested_deliverables) <= 12. If trimmed, add dropped names to metadata.dropped_phases.
- All owners in phases MUST be member_ids from team_structure. If not possible, set owner_member_id to null and explain in assumptions_text.
- metadata must include all required fields listed below.
=== AUDIENCE RULE ===
- If audience == "executive": keep technical_backend_text high-level and put operational tuning to appendix_technical_spec.
- If audience == "technical_lead" or "mixed": include high-level technical with appendix for tuning.
- Tailor the entire proposal to the audience, incorporating best practices from similar professional documents for structure, clarity, and persuasion.
=== HALLUCINATION GUARD ===
- Do NOT invent capacities, vendor limits, tariffs, cost figures, or financial numbers. If unknown, set "unknown" and include exact verification steps + responsible role.
=== OUTPUT SCHEMA (metadata fields required) ===
metadata must include at least:
{
  "total_hours_realistic": int,
  "capacity_hours_available": int|null,
  "deadline_feasible": bool,
  "risk_message": str,
  "dropped_phases": [str,...],
  "allow_overflow_requested": bool,
  "allow_overflow_used": bool,
  "overflow_hours": int,
  "overflow_plan": object|null,
  "reality_check_used": bool,
  "suggested_deadline_extension_days": int|null,
  "used_minimum_deadline": bool,
  "primary_recommendation": "extend_deadline|add_fte|compress_scope|accept_risk_with_mitigation",
  "primary_recommendation_rationale": str,
  "additional_FTEs_required": int|null,
  "api_rate_limits": [...],
  "phase_risks": [...],          # optional, only if any
  "owner_unassigned_phases": [...], # list of phase_names with unassigned owner_member_id
  "conflicts": [...],            # date/dependency conflicts if any
  "assumptions_internal_count": int
}
END OF RULES.
Produce the JSON object now following the schema above.
""")
    # safe Template usage
    try:
        tmpl = Template(prompt_template)
    except Exception as exc:
        logger.exception("Template creation failed: %s", exc)
        return (
            '{"error":"prompt-template-failed","message":"internal generator error"}'
        )
    try:
        prompt_filled = tmpl.safe_substitute(mapping)
    except Exception as exc:
        logger.exception("Template substitution failed: %s", exc)
        prompt_filled = (
            '{"error":"prompt-substitute-failed","message":"substitution failed; provide basic schema in output"}'
        )
    # final guard
    if not isinstance(prompt_filled, str) or not prompt_filled.strip():
        return '{"error":"prompt-empty-fallback","message":"failed to construct prompt"}'
    return prompt_filled

def _compute_capacity_from_deadline(proposal: Dict[str, Any]) -> Optional[int]:
    """
    Compute team capacity in hours using same logic as Streamlit but with exact workday counting.
    Returns int or None if cannot compute.
    """
    try:
        deadline_raw = proposal.get("deadline") or proposal.get("deadline_date") or ""
        if not deadline_raw:
            return None
        if isinstance(deadline_raw, (date, datetime)):
            deadline_date = deadline_raw if isinstance(deadline_raw, date) else deadline_raw.date()
        else:
            deadline_date = datetime.strptime(str(deadline_raw), "%Y-%m-%d").date()
        today = date.today()
        if deadline_date <= today:
            return 0
        work_days = _count_workdays(today, deadline_date)
        available_hours_single = work_days * 8
        team_size = int(proposal.get("team_size", 1) or 1)
        total_capacity = int(max(0, available_hours_single * team_size))
        if work_days > 0 and total_capacity < 8:
            total_capacity = 8
        return total_capacity
    except Exception:        return None


def _extract_text_from_openai_response(resp: Any) -> str:

    """
    Always return a JSON/text string. If the client returned structured content (dict/list),
    dump to JSON string. Fallback to str(resp).
    """
    try:
        # handle new-client structured response
        if isinstance(resp, dict):
            # try to extract message content
            choices = resp.get("choices")
            if choices and isinstance(choices, list):
                first = choices[0]
                msg = first.get("message") if isinstance(first, dict) else None
                if isinstance(msg, dict):
                    content = msg.get("content") or msg.get("text")
                else:
                    content = first.get("text") or first.get("message") or None
            else:
                content = resp.get("text") or resp.get("message") or None
        else:
            # object-like (client objects): try attribute access
            content = None
            if hasattr(resp, "choices"):
                choices = resp.choices
                if choices:
                    first = choices[0]
                    msg = getattr(first, "message", None)
                    if isinstance(msg, dict):
                        content = msg.get("content") or msg.get("text")
                    else:
                        content = getattr(msg, "content", None) or getattr(first, "text", None)
        # If content is structured (dict/list), dump to JSON string
        if isinstance(content, (dict, list)):
            return json.dumps(content, ensure_ascii=False)
        if isinstance(content, str):
            return content
    except Exception:
        logger.debug("Failed to extract content from OpenAI response", exc_info=True)

    try:
        return json.dumps(resp, default=str, ensure_ascii=False)
    except Exception:
        return str(resp)


def _clean_and_load_json(text: str) -> Optional[Any]:
    """Удаляет ограждающие скобки ```json и парсит JSON."""
    blob = (text or "").strip()
    if blob.startswith("```"):
        blob = blob.strip("` \n")
        if blob.lower().startswith("json"):
            blob = blob[4:].strip()
    try:
        return json.loads(blob)
    except json.JSONDecodeError as e:
        logger.warning("JSON decode failed: %s", e)
        return None


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
                response_format=json_format 
            )
        except TypeError:
            # Fallback (если request_timeout не поддерживается, 
            resp = create_fn(
                model=model_name, 
                messages=messages, 
                max_tokens=OPENAI_MAX_TOKENS, 
                temperature=OPENAI_TEMPERATURE,
                response_format=json_format 
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
        
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        response = model.generate_content(prompt_str)
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


def _clean_and_parse_json(text: str, expected_type: type) -> Any:
    if not text:
        raise ValueError("Empty response text.")
    blob = text.strip()
    if blob.startswith("```"):
        blob = blob.strip("` \n")
        if blob.lower().startswith("json"):
            blob = blob[4:].strip()
    parsed = json.loads(blob)
    # soft-normalization: if list expected but dict returned, try common keys
    if expected_type is list and isinstance(parsed, dict):
        for k in ("stages","lifecycle_stages","items","result","data"):
            if k in parsed and isinstance(parsed[k], list):
                logger.warning("Normalized dict->list using key '%s'", k)
                return parsed[k]
    if not isinstance(parsed, expected_type):
        raise TypeError(f"Parsed JSON is {type(parsed).__name__}, expected {expected_type.__name__}")
    return parsed

def _invoke_with_fallback(prompt: str, stub_value: Any, parse_json: bool = False, expected_json_type: Optional[type] = None):
    # 1) Try OpenAI (with retries)
    last_exc = None
    for attempt in range(1, max(1, OPENAI_RETRY_ATTEMPTS) + 1):
        try:
            text = _call_openai_new_client(prompt, OPENAI_MODEL)
            if not text:
                last_exc = RuntimeError("Empty response from OpenAI")
                continue
            
            # Case 1: Raw text requested (e.g., generate_ai_json)
            if expected_json_type is str:
                logger.info("OpenAI attempt %d succeeded (raw text).", attempt)
                return text

            # Case 2: Parsed list/dict requested
            parsed = _clean_and_parse_json(text, expected_json_type)
            
            # Extra validation for list: must be non-empty
            if expected_json_type is list and not parsed:
                last_exc = RuntimeError("OpenAI returned empty JSON list")
                continue

            logger.info("OpenAI attempt %d succeeded (parsed %s).", attempt, expected_json_type.__name__)
            return parsed
            
        except Exception as e:
            last_exc = e
            logger.warning("OpenAI attempt %d failed: %s", attempt, str(e)[:200])
            
            # Check for immediate fail conditions (like model not found)
            if "model_not_found" in str(e).lower() or "does not exist" in str(e).lower():
                logger.warning("OpenAI model not found, switching to Gemini fallback.")
                break
                
            if attempt < OPENAI_RETRY_ATTEMPTS:
                backoff = OPENAI_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                time.sleep(backoff + random.random() * 0.5)
            else:
                break # Last attempt failed

    # 2) Try Gemini fallback
    if genai is not None and GOOGLE_API_KEY:
        try:
            logger.info("Trying Gemini fallback...")
            gemini_text, gemini_reason = _call_gemini(prompt)
            
            if not gemini_text:
                logger.warning("Gemini returned empty: %s", gemini_reason)
            else:
                # Case 1: Raw text requested
                if expected_json_type is str:
                    logger.info("Gemini fallback succeeded (raw text).")
                    return gemini_text

                # Case 2: Parsed list/dict requested
                try:
                    parsed = _clean_and_parse_json(gemini_text, expected_json_type)
                    
                    # Extra validation for list: must be non-empty
                    if expected_json_type is list and not parsed:
                        logger.warning("Gemini returned empty JSON list.")
                    else:
                        logger.info("Gemini fallback succeeded (parsed %s).", expected_json_type.__name__)
                        return parsed
                except Exception as e:
                    logger.warning("Failed to parse Gemini JSON: %s", e)
        except Exception as e:
            logger.exception("Gemini fallback attempt failed entirely: %s", e)

    # 3) Final deterministic fallback
    logger.error("Both OpenAI and Gemini failed -> returning deterministic stub.")
    # If the stub is a dictionary/list, and we were asked for raw string, we must dump it.
    if expected_json_type is str and not isinstance(stub_value, str):
        # This handles the case for generate_ai_json's output
        return json.dumps(stub_value, ensure_ascii=False)
        
    return stub_value


FALLBACK_LIFECYCLE_STAGES = [
    {"name": "Discovery & Planning", "description": "Define scope, success criteria and architecture.", "depends_on": []},
    {"name": "Design & Setup", "description": "Environment, infra and schema setup.", "depends_on": ["Discovery & Planning"]},
    {"name": "Implementation", "description": "Core development and integration.", "depends_on": ["Design & Setup"]},
    {"name": "QA & UAT", "description": "Testing and client acceptance.", "depends_on": ["Implementation"]},
    {"name": "Deployment & Monitoring", "description": "Go-live and production monitoring.", "depends_on": ["QA & UAT"]},
]

# Фоллбэк для generate_ai_json (сокращенный фоллбэк из конца функции)
FALLBACK_AI_JSON_DICT_MINIMAL = {
    "suggested_deliverables": [
        {
            "title": "Requirements & Analysis",
            "description": "Gather and analyze functional and non-functional requirements for the project.",
            "acceptance": "Requirements document approved by client."
        },
        {
            "title": "Prompt Engineering Module",
            "description": "Design and implement the prompt optimization subsystem for AI text generation.",
            "acceptance": "Module integrated and verified with 95% prompt quality success rate."
        },
        {
            "title": "CRM API Integration",
            "description": "Implement secure data synchronization between CRM and backend.",
            "acceptance": "Successful CRM data exchange verified in staging."
        },
        {
            "title": "Testing & Deployment",
            "description": "Perform end-to-end testing and deploy the AI proposal system.",
            "acceptance": "Deployment verified and accepted after QA sign-off."
        }
    ],
    "suggested_phases": [
        {
            "phase_name": "Setup & Data Modeling",
            "duration_hours": 40,
            "tasks": "Environment setup, database schema design, requirements finalization"
        },
        {
            "phase_name": "Prompt Engineering & LLM Fine-Tuning",
            "duration_hours": 120,
            "tasks": "Prompt optimization, model integration, API testing"
        },
        {
            "phase_name": "CRM Integration & Backend Development",
            "duration_hours": 120,
            "tasks": "Backend API, CRM connectors, authentication, business logic"
        },
        {
            "phase_name": "Testing & QA Automation",
            "duration_hours": 80,
            "tasks": "Unit tests, integration tests, QA review"
        },
        {
            "phase_name": "Deployment & Monitoring",
            "duration_hours": 40,
            "tasks": "Production release, observability setup, performance tuning"
        }
    ]
}



def _generate_lifecycle_stages_with_agent(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    project_goal = data.get("project_goal", "generic AI project")
    client_name = data.get("client_name", "A generic client")
    technologies = data.get("technologies") or []
    tech_str = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)
    prompt = f"""
You are an expert Project Manager and Solution Architect specializing in AI/ML project delivery.

Your task is to generate realistic project lifecycle stages (phases) for the project described below.

Project Goal: "{project_goal}"
Technologies: {tech_str}
Client Context: {client_name}

**Output Instruction:**
1. You MUST return **ONLY** one valid JSON array (list) and **NOTHING ELSE**.
2. Each item in the array must be an object (dictionary) with the following **EXACT** keys:
   - **name**: (string) A clear, professional title for the stage (e.g., "Data Acquisition & Cleaning").
   - **description**: (string) A concise summary of the stage (1 very short sentence).
   - **depends_on**: (list of strings) A list of the **exact 'name'** values of the preceding stages that this stage depends on. Use an empty list [] for the first stage.
   - **type**: (string) The category of the stage. You must use one of these specific categories: 
     **'Planning', 'Setup', 'Development', 'Integration', 'Testing', 'Deployment'**.

**Example of an Expected JSON Element:**
{{{{
    "name": "Discovery",
    "description": "Finalize detailed requirements and establish clear success metrics.",
    "depends_on": [],
    "type": "Planning"
}}}}

Generate a realistic, logical sequence of lifecycle stages for the project.
"""

    # Детерминированный фоллбэк (возвращается, если LLMs не сработали)
    # Используем извлеченную константу
    stub_stages = FALLBACK_LIFECYCLE_STAGES 
    
    # 1) Заменяем всю логику вызова LLM на _invoke_with_fallback
    return _invoke_with_fallback(
        prompt=prompt,
        stub_value=stub_stages,
        expected_json_type=list # Ожидаем JSON list
    )

def _generate_uml_with_agent(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Генерирует структуру UML (JSON с keys: components, relations) через общий _invoke_with_fallback.
    Возвращает dict с указанной структурой или детерминированный stub в случае фолбэка.
    """
    project_goal = data.get("project_goal", "generic AI project")
    client_name = data.get("client_name", data.get("client", "Generic client"))
    technologies = data.get("technologies") or []
    tech_str = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)

    prompt = f"""
You are an expert solution architect. Using ONLY the context below, produce EXACTLY ONE JSON object (and nothing else)
describing the **system components** and **relations** for the project.

Context:
- Project goal: "{project_goal}"
- Client: "{client_name}"
- Technologies: {tech_str}

**Output instructions (STRICT):**
Return exactly one JSON object with TWO keys: "components" and "relations".

- "components": array of objects with keys:
  - id (string, unique)
  - name (string)
  - stereotype (string, e.g. "service", "database", "worker", "frontend", "pipeline")
  - responsibilities (array of short strings) -- mark inferred items with "(inferred)" if unsure
  - attributes (array of short strings)
  - notes (string)

- "relations": array of objects with keys:
  - from (component id)
  - to (component id)
  - type (one of "dependency","association","aggregation","composition","inherits")
  - label (short string, may be empty)

Return JSON only. Example:
{{"components":[{{"id":"ui","name":"User UI","stereotype":"frontend","responsibilities":["display results"]}}],"relations":[{{"from":"ui","to":"api","type":"dependency","label":"calls"}}]}}
"""

    # Deterministic fallback stub (returned by _invoke_with_fallback if LLMs fail)
    stub = {
        "components": [
            {"id": "ui", "name": "User Interface", "stereotype": "frontend",
             "responsibilities": ["user interactions (inferred)"], "attributes": [], "notes": ""},
            {"id": "api", "name": "API Service", "stereotype": "service",
             "responsibilities": ["business logic (inferred)"], "attributes": [], "notes": ""},
            {"id": "db", "name": "Primary Database", "stereotype": "database",
             "responsibilities": ["persistent storage (inferred)"], "attributes": [], "notes": ""}
        ],
        "relations": [
            {"from": "ui", "to": "api", "type": "dependency", "label": "HTTP calls"},
            {"from": "api", "to": "db", "type": "dependency", "label": "reads/writes"}
        ]
    }

    # Use the same centralized fallback/invoke helper as lifecycle stages
    # expected_json_type = dict ensures the wrapper validates top-level JSON is an object
    return _invoke_with_fallback(
        prompt=prompt,
        stub_value=stub,
        expected_json_type=dict
    )

def _count_workdays(start_date: date, end_date: date) -> int:
    """
    Count business days from start_date (exclusive) to end_date (inclusive).
    Returns 0 if end_date <= start_date.
    """
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        return 0
    if start_date >= end_date:
        return 0
    days = (end_date - start_date).days
    # Fast approach: full weeks + leftover days
    full_weeks, extra_days = divmod(days, 7)
    workdays = full_weeks * 5
    # handle leftover
    for i in range(1, extra_days + 1):
        if (start_date + _td(days=i)).weekday() < 5:
            workdays += 1
    return workdays

def generate_ai_json(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    if OPENAI_USE_STUB:
        client = proposal.get("client_company_name", "Client")
        stub = {"executive_summary_text": f"Fallback for {client}.", "suggested_deliverables": [], "suggested_phases": [], "visualization": {}, "metadata": {}}
        return json.dumps(stub, ensure_ascii=False)

    # ensure lifecycle stages
    if not proposal.get("lifecycle_stages"):
        try:
            proposal["lifecycle_stages"] = _generate_lifecycle_stages_with_agent(proposal)
        except Exception:
            # keep going even if lifecycle generation fails
            proposal.setdefault("_meta", {}).setdefault("warnings", []).append("lifecycle_generation_failed")

    # ensure UML structure exists (use central helper which has fallback)
    if not proposal.get("uml_structure"):
        try:
            proposal["uml_structure"] = _generate_uml_with_agent(proposal)
        except Exception:
            proposal.setdefault("_meta", {}).setdefault("warnings", []).append("uml_generation_failed")

    prompt = _build_prompt(proposal, tone)
    try:
        cached = _invoke_openai_cached(prompt, OPENAI_MODEL)
        if cached:
            try:
                json.loads(cached)
                return cached
            except Exception:
                return cached
    except Exception:
        pass

    res = _invoke_with_fallback(prompt=prompt, stub_value=FALLBACK_AI_JSON_DICT_MINIMAL, expected_json_type=str)
    return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)

def generate_suggestions(
    proposal: Dict[str, Any],
    tone: str = "Formal",
    max_deliverables: int = 20,
    max_phases: int = 20
) -> Dict[str, Any]:
    """
    Return a dict with 'suggested_deliverables' and 'suggested_phases'.
    If LLM fails, returns deterministic fallback with realistic AI project phases.
    """
    prompt = _build_suggestion_prompt(proposal, tone, max_deliverables=max_deliverables, max_phases=max_phases)
    
    # Deterministic fallback dict
    client = proposal.get("client_name", "Client")
    stub_data = {
        "suggested_deliverables": [
            {
                "title": "Requirements & Analysis",
                "description": f"Gather and analyze functional and non-functional requirements for {client}'s AI proposal generator.",
                "acceptance": "Requirements document approved by client."
            },
            {
                "title": "Prompt Engineering Module",
                "description": "Design and implement the prompt optimization subsystem for AI text generation.",
                "acceptance": "Module integrated and verified with 95% prompt quality success rate."
            },
            {
                "title": "CRM API Integration",
                "description": "Implement secure data synchronization between CRM and proposal generator backend.",
                "acceptance": "Successful CRM data exchange verified in staging."
            },
            {
                "title": "Testing & Deployment",
                "description": "Perform end-to-end testing and deploy the AI proposal system to production environment.",
                "acceptance": "Deployment verified and accepted after QA sign-off."
            }
        ],
        "suggested_phases": [
            {
                "phase_name": "Setup & Data Modeling",
                "duration_hours": 80,
                "tasks": "Environment setup, database schema design, requirements finalization"
            },
            {
                "phase_name": "Prompt Engineering & LLM Fine-Tuning",
                "duration_hours": 120,
                "tasks": "Prompt optimization, model integration, API testing"
            },
            {
                "phase_name": "CRM Integration & Backend Development",
                "duration_hours": 120,
                "tasks": "Backend API, CRM connectors, authentication, business logic"
            },
            {
                "phase_name": "Testing & QA Automation",
                "duration_hours": 80,
                "tasks": "Unit tests, integration tests, QA review"
            },
            {
                "phase_name": "Deployment & Monitoring",
                "duration_hours": 40,
                "tasks": "Production release, observability setup, performance tuning"
            }
        ]
    }

    # Try cached fast path (KEEPING CACHE LOGIC HERE)
    try:
        cached = None
        try:
            cached = _invoke_openai_cached(prompt, OPENAI_MODEL)
        except Exception:
            cached = None

        if cached:
            try:
                parsed = _clean_and_parse_json(cached, dict)
                if isinstance(parsed, dict):
                    parsed = _postprocess_suggestion_result(parsed, proposal, max_phases=max_phases, max_deliverables=max_deliverables)
                    return {
                        "suggested_deliverables": parsed.get("suggested_deliverables", []),
                        "suggested_phases": parsed.get("suggested_phases", []),
                        "metadata": parsed.get("metadata", {}),
                        "risks_text": parsed.get("risks_text", ""),
                        "assumptions_text": parsed.get("assumptions_text", "")
                    }
            except Exception:
                pass
    except Exception:
        pass

    parsed_result = _invoke_with_fallback(
        prompt=prompt,
        stub_value=stub_data,
        expected_json_type=dict
    )

    # postprocess to ensure consistency
    try:
        parsed_result = _postprocess_suggestion_result(parsed_result, proposal, max_phases=max_phases, max_deliverables=max_deliverables)
    except Exception:
        logger.exception("Postprocessing suggestion result failed; returning raw parsed_result")

    return {
        "suggested_deliverables": parsed_result.get("suggested_deliverables", []),
        "suggested_phases": parsed_result.get("suggested_phases", []),
        "metadata": parsed_result.get("metadata", {}),
        "risks_text": parsed_result.get("risks_text", ""),
        "assumptions_text": parsed_result.get("assumptions_text", "")
    }

def _postprocess_suggestion_result(parsed: Dict[str, Any], proposal: Dict[str, Any], max_phases: int = 20, max_deliverables: int = 20) -> Dict[str, Any]:
    """
    Ensure parsed suggestion dict conforms to schema invariants:
     - durations ints >=4
     - metadata.total_hours_realistic equals sum(suggested_phases.duration_hours)
     - compute overflow_hours and suggested_deadline_extension_days based on reality_check / computed capacity
     - ensure keys exist
    """
    if not isinstance(parsed, dict):
        return parsed  # leave to caller's error handling

    # ensure keys
    parsed.setdefault("suggested_phases", [])
    parsed.setdefault("suggested_deliverables", [])
    parsed.setdefault("metadata", {})
    parsed.setdefault("risks_text", "")
    parsed.setdefault("assumptions_text", "")

    # normalize phases
    phases = parsed["suggested_phases"]
    normalized_phases = []
    for p in phases[:max_phases]:
        # ensure shape
        phase_name = p.get("phase_name") or p.get("name") or "Phase"
        duration = p.get("duration_hours") or p.get("duration") or 0
        try:
            duration = int(float(duration))
        except Exception:
            duration = 0
        # enforce minimum 4h unless explicitly 0 (but LLM generally shouldn't give 0)
        if duration > 0:
            duration = max(4, duration)
        owner = p.get("owner") or p.get("resource") or "Engineering"
        tasks = p.get("tasks") or p.get("description") or ""
        priority = p.get("priority") or "must"
        normalized_phases.append({
            "phase_name": str(phase_name),
            "duration_hours": int(duration),
            "tasks": str(tasks),
            "owner": str(owner),
            "priority": str(priority)
        })
    parsed["suggested_phases"] = normalized_phases
    # ---- deterministic enforcement to fit capacity (server-side) ----
    try:
        # Получаем capacity: приоритет — reality_check.team_capacity_hours, иначе — вычисление из дедлайна
        rc = None
        try:
            rc = _extract_reality_check(proposal) or {}
        except Exception:
            rc = proposal.get("reality_check") or {}

        rc_capacity = None
        if rc and rc.get("team_capacity_hours") is not None:
            try:
                rc_capacity = int(rc.get("team_capacity_hours"))
            except Exception:
                rc_capacity = None

        # fallback compute from deadline if rc_capacity is None
        if rc_capacity is None:
            try:
                computed = _compute_capacity_from_deadline(proposal)
                if isinstance(computed, (int, float)):
                    rc_capacity = int(computed)
            except Exception:
                rc_capacity = None

        # team size fallback
        try:
            team_size = int(proposal.get("team_size") or 1)
        except Exception:
            team_size = 1

        # enforce only when capacity known and allow_overflow is False
        allow_overflow_flag = bool(rc.get("allow_overflow")) if isinstance(rc, dict) else bool(proposal.get("allow_overflow", False))
        if rc_capacity is not None and not allow_overflow_flag:
            original_phases = parsed.get("suggested_phases", [])
            adjusted_phases, enforcement_info = _enforce_capacity_on_phases(
                original_phases,
                rc_capacity,
                team_size,
                aggressive_pct=int(parsed.get("metadata", {}).get("aggressive_compression_limit_pct", 30)),
                min_phase_hours=int(parsed.get("metadata", {}).get("min_phase_hours", 4))
            )
            # apply adjustments
            parsed["suggested_phases"] = adjusted_phases
            # compute new total_hours_realistic
            new_total = sum(int(p.get("duration_hours") or 0) for p in adjusted_phases)
            parsed.setdefault("metadata", {})
            parsed["metadata"]["total_hours_realistic"] = int(new_total)
            parsed["metadata"]["capacity_hours_available"] = int(rc_capacity)
            parsed["metadata"].setdefault("dropped_phases", [])
            parsed["metadata"]["dropped_phases"].extend(enforcement_info.get("dropped_phases", []))
            parsed["metadata"]["allow_overflow_used"] = False
            parsed["metadata"]["enforcement"] = enforcement_info
            parsed["metadata"]["overflow_hours"] = int(enforcement_info.get("overflow_hours", 0))
            # If enforcement produced any compressions/drops, produce phase_risks entries
            phase_risks = []
            for c in enforcement_info.get("compressions", []):
                phase_risks.append({
                    "phase_name": c["phase"],
                    "duration_hours": next((p["duration_hours"] for p in adjusted_phases if p["phase_name"] == c["phase"]), None),
                    "risk_level": "medium",
                    "reason": f"Phase compressed by {c['reduced_hours']} hours to fit capacity",
                    "mitigations": "Increase parallelisation, extend deadline or add FTEs",
                    "additional_hours_needed": 0,
                    "recommended_action": "Monitor in PHASE RISKS",
                    "affected_downstream_phases": []
                })
            for d in enforcement_info.get("dropped_phases", []):
                phase_risks.append({
                    "phase_name": d,
                    "duration_hours": None,
                    "risk_level": "high",
                    "reason": "Phase dropped to fit capacity",
                    "mitigations": "Consider extend_deadline or add_fte to restore scope",
                    "additional_hours_needed": None,
                    "recommended_action": "List in PHASE RISKS and escalate",
                    "affected_downstream_phases": []
                })
            if phase_risks:
                parsed["metadata"]["phase_risks"] = phase_risks
                # Prepend phase-level bullets to risks_text (if exists) or create
                existing_risks = parsed.get("risks_text", "")
                phase_bullets = []
                for r in phase_risks:
                    phase_bullets.append(f"- {r['phase_name']}: {r['reason']}; mitigation: {r['mitigations']}")
                parsed["risks_text"] = ("\n".join(phase_bullets) + ("\n\n" + existing_risks if existing_risks else ""))
        else:
            # if capacity unknown, set metadata.capacity_hours_available=null
            parsed.setdefault("metadata", {})
            parsed["metadata"].setdefault("capacity_hours_available", None)
    except Exception as e:
        # не ломаем основную генерацию — логируем и продолжаем
        try:
            logger = globals().get("logger")
            if logger:
                logger.exception("Capacity enforcement failed: %s", e)
        except Exception:
            pass
    # ---- end enforcement block ----

    # Enforce capacity deterministically if capacity known and overflow > 0 and allow_overflow is False
    rc = _extract_reality_check(proposal)
    capacity = rc.get("team_capacity_hours") if rc.get("team_capacity_hours") is not None else _compute_capacity_from_deadline(proposal)
    team_size = int(proposal.get("team_size", 1) or 1)
    parsed["metadata"].setdefault("enforcement", {})

    # Only enforce when capacity is numeric and LLM plan exceeds capacity and allow_overflow not requested
    if capacity is not None:
        adjusted_phases, enforcement_info = _enforce_capacity_on_phases(parsed["suggested_phases"], int(capacity), team_size, aggressive_pct=aggressive_compression_limit_pct, min_phase_hours=min_phase_hours)
        parsed["suggested_phases"] = adjusted_phases
        # recompute total_hours after enforcement
        total_hours = sum(int(p.get("duration_hours") or 0) for p in parsed["suggested_phases"])
        parsed["metadata"]["enforcement"] = enforcement_info

    # normalize deliverables (truncate to max_deliverables)
    dels = parsed.get("suggested_deliverables", [])[:max_deliverables]
    parsed["suggested_deliverables"] = dels

    # compute totals
    total_hours = sum(int(p.get("duration_hours") or 0) for p in parsed["suggested_phases"])
    meta = parsed["metadata"]
    meta_total = meta.get("total_hours_realistic")
    try:
        meta_total = int(meta_total) if meta_total is not None else None
    except Exception:
        meta_total = None
    # override metadata value to be consistent
    meta["total_hours_realistic"] = int(total_hours)

    # capacity
    rc = _extract_reality_check(proposal)
    capacity = rc.get("team_capacity_hours")
    if capacity is None:
        capacity = _compute_capacity_from_deadline(proposal)

    meta["capacity_hours_available"] = int(capacity) if capacity is not None else None
    meta["reality_check_used"] = bool(rc.get("planned_effort_hours") or rc.get("team_capacity_hours") or rc.get("requested_deadline_extension_days"))

    # overflow calc
    overflow = 0
    if capacity is not None:
        overflow = max(0, int(total_hours) - int(capacity))
    meta["overflow_hours"] = int(overflow)
    allow_overflow_requested = bool(rc.get("allow_overflow", False))
    meta["allow_overflow_requested"] = allow_overflow_requested
    # determine allow_overflow_used and final feasibility
    if overflow > 0:
        if allow_overflow_requested:
            meta["allow_overflow_used"] = True
            meta["deadline_feasible"] = True
        else:
            meta["allow_overflow_used"] = False
            meta["deadline_feasible"] = False
            # add risk message if empty
            if not meta.get("risk_message"):
                meta["risk_message"] = f"Plan requires {total_hours}h but capacity is {capacity}h."
    else:
        meta["allow_overflow_used"] = False
        meta["deadline_feasible"] = True
        if not meta.get("risk_message"):
            meta["risk_message"] = ""

    # suggested_deadline_extension_days when overflow exists
    if overflow > 0 and capacity and proposal.get("team_size", 1):
        per_day_capacity = int(proposal.get("team_size", 1)) * 8
        if per_day_capacity > 0:
            meta["suggested_deadline_extension_days"] = int(math.ceil(overflow / per_day_capacity))
        else:
            meta["suggested_deadline_extension_days"] = None
    else:
        meta["suggested_deadline_extension_days"] = meta.get("suggested_deadline_extension_days")

    parsed["metadata"] = meta
    return parsed
# END PATCH

def _extract_reality_check(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract canonical reality-check fields from the incoming proposal payload.
    Accepts several input shapes (streamlit form, top-level keys, or nested dict).
    Returns dict with integer fields:
      {
        "planned_effort_hours": int or None,
        "team_capacity_hours": int or None,
        "delta_hours": int or None,
        "allow_overflow": bool,
        "requested_deadline_extension_days": int or None
      }
    """
    rc = proposal.get("reality_check") or proposal.get("reality") or {}
    out = {
        "planned_effort_hours": None,
        "team_capacity_hours": None,
        "delta_hours": None,
        "allow_overflow": False,
        "requested_deadline_extension_days": None
    }
    # top-level aliases
    aliases = {
        "planned_effort_hours": ["planned_effort_hours", "planned_effort", "planned_hours", "planned_effort_h"],
        "team_capacity_hours": ["team_capacity_hours", "capacity_hours", "team_capacity", "capacity_h"],
        "delta_hours": ["delta_hours", "effort_delta_hours", "delta_h"],
        "allow_overflow": ["allow_overflow", "overflow_allowed", "permit_overflow"],
        "requested_deadline_extension_days": ["requested_deadline_extension_days", "deadline_extension_days", "extend_days"]
    }
    # normalize if reality_check nested dict exists
    if isinstance(rc, dict):
        for k, keys in aliases.items():
            for key in keys:
                if key in rc:
                    out[k] = rc.get(key)
                    break
    # fallback to top-level fields
    for k, keys in aliases.items():
        if out[k] is None:
            for key in keys:
                if key in proposal:
                    out[k] = proposal.get(key)
                    break
    # coerce types
    def to_int(v):
        try:
            if v is None or v == "":
                return None
            return int(float(v))
        except Exception:
            return None
    out["planned_effort_hours"] = to_int(out["planned_effort_hours"])
    out["team_capacity_hours"] = to_int(out["team_capacity_hours"])
    out["delta_hours"] = to_int(out["delta_hours"])
    out["allow_overflow"] = bool(out["allow_overflow"])
    out["requested_deadline_extension_days"] = to_int(out["requested_deadline_extension_days"])
    return out

def _enforce_capacity_on_phases(phases: list, capacity_hours: Optional[int], team_size: int,
                                aggressive_pct: int = 30, min_phase_hours: int = 4):
    """
    Deterministically try to fit 'phases' into capacity_hours.
    Strategy (order):
      1) Try conservative compression of 'should' and 'optional' phases up to aggressive_pct each.
      2) Drop 'optional' phases if needed (record dropped names).
      3) Further compression on 'should' if still needed (second pass).
      4) If still over and capacity_hours is not None, return overflow_hours > 0.
    Returns: adjusted_phases, adjustments_info dict:
       {"dropped_phases": [...], "compressions": [{"phase":..,"reduced":..}], "overflow_hours": int}
    """
    from math import floor, ceil

    # Defensive copy
    phases = [dict(p) for p in phases]
    # ensure ints
    for p in phases:
        p["duration_hours"] = int(p.get("duration_hours") or 0)

    total = sum(p["duration_hours"] for p in phases)
    adjustments = {"dropped_phases": [], "compressions": [], "overflow_hours": 0}

    if capacity_hours is None:
        # nothing to enforce deterministically
        adjustments["overflow_hours"] = max(0, total - (team_size * 8 * 20))  # heuristic
        return phases, adjustments

    if total <= capacity_hours:
        adjustments["overflow_hours"] = 0
        return phases, adjustments

    # Priority ordering: optional -> should -> must (optional are first candidates to modify)
    # First pass: compress should/optional by up to aggressive_pct
    for p in phases:
        if total <= capacity_hours:
            break
        if p.get("priority", "must") in ("should", "optional"):
            original = p["duration_hours"]
            max_reduction = int(floor(original * (aggressive_pct / 100.0)))
            if max_reduction <= 0:
                continue
            needed = total - capacity_hours
            reduction = min(max_reduction, needed)
            if reduction > 0:
                p["duration_hours"] = max(min_phase_hours, original - reduction)
                actual_reduction = original - p["duration_hours"]
                total -= actual_reduction
                adjustments["compressions"].append({"phase": p["phase_name"], "reduced_hours": int(actual_reduction)})

    # Second step: drop optional phases entirely (largest first)
    if total > capacity_hours:
        optional_phases = sorted([p for p in phases if p.get("priority", "must") == "optional"],
                                 key=lambda x: x["duration_hours"], reverse=True)
        for opt in optional_phases:
            if total <= capacity_hours:
                break
            phases.remove(opt)
            adjustments["dropped_phases"].append(opt["phase_name"])
            total -= opt["duration_hours"]

    # Third step: additional compression on 'should' phases (another aggressive_pct)
    if total > capacity_hours:
        should_phases = sorted([p for p in phases if p.get("priority", "must") == "should"],
                               key=lambda x: x["duration_hours"], reverse=True)
        for p in should_phases:
            if total <= capacity_hours:
                break
            original = p["duration_hours"]
            extra_reduction = int(floor(original * (aggressive_pct / 100.0)))
            if extra_reduction <= 0:
                continue
            needed = total - capacity_hours
            reduction = min(extra_reduction, needed)
            p["duration_hours"] = max(min_phase_hours, original - reduction)
            actual_reduction = original - p["duration_hours"]
            total -= actual_reduction
            adjustments["compressions"].append({"phase": p["phase_name"], "reduced_hours": int(actual_reduction)})

    # Final overflow if still > capacity
    adjustments["overflow_hours"] = max(0, total - capacity_hours)
    return phases, adjustments

def _build_suggestion_prompt(
    proposal: Dict[str, Any],
    tone: str = "Formal",
    max_deliverables: int = 8,
    max_phases: int = 20,   # расширённый верхний предел — LLM сама выбирает разумное число <= this
) -> str:
    """
    Builds a prompt for the suggestion endpoint with "Smart Fit" logic.
    Uses JSON.dumps to safely embed the required output schema to avoid
    Python f-string format specifier errors.

    Key changes:
    - LLM chooses number of phases based on project complexity and deadline,
      constrained to 1..max_phases (default 20).
    - Deadline feasibility check MUST be conservative and MUST NOT assume
      parallel execution: compute calendar_days = ceil(SUM(duration_hours) / 8)
      (i.e., single-stream execution, 8h/day). Compare calendar_days to days_until_deadline.
    - All other adaptation rules (compression, dropping optional phases, etc.)
      remain in force.
    """
    import json
    import math
    from datetime import date, datetime

    rc = _extract_reality_check(proposal)
    allow_overflow_requested = rc.get("allow_overflow", False)

    # team size (coerce safe)
    try:
        team_size = int(proposal.get("team_size", 1) or 1)
    except Exception:
        team_size = 1

    # Capacity resolution (prefer reality check)
    capacity_int = None
    if rc.get("team_capacity_hours") is not None:
        try:
            capacity_int = int(rc["team_capacity_hours"])
        except Exception:
            capacity_int = None
    else:
        # compute from deadline if provided (used as fallback capacity estimate only)
        capacity_int = None
        deadline_raw = proposal.get("deadline") or proposal.get("deadline_date")
        if deadline_raw:
            try:
                if isinstance(deadline_raw, (date, datetime)):
                    d_obj = deadline_raw if isinstance(deadline_raw, date) else deadline_raw.date()
                else:
                    # be permissive parsing ISO-like strings
                    d_obj = datetime.fromisoformat(str(deadline_raw)).date()
                today = date.today()
                if d_obj > today:
                    days_diff = (d_obj - today).days
                    work_days = max(0, math.floor(days_diff * (5.0 / 7.0)))
                    capacity_int = int(max(0, work_days * 8 * team_size))
                else:
                    capacity_int = 0
            except Exception:
                capacity_int = None

    total_team_capacity_hours = capacity_int if capacity_int is not None else "null"
    # target budget (safety buffer)
    target_budget_hours = int(capacity_int * 0.95) if isinstance(capacity_int, int) and capacity_int > 0 else ("null" if capacity_int is None else int(capacity_int))

    client = proposal.get("client_company_name") or proposal.get("client_name") or "Client"
    project_goal = proposal.get("project_goal", "") or proposal.get("goal", "")
    scope = proposal.get("scope", "") or proposal.get("description", "")
    technologies = proposal.get("technologies") or proposal.get("tech") or []
    techs = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)

    # canonical JSON schema (assistant must return exactly this structure)
    schema = {
      "suggested_phases": [
        {
          "phase_name": "string",
          "duration_hours": 0,
          "original_hours": None,
          "tasks": "string",
          "owner": "string",
          "priority": "must|should|optional",
          "compression_pct": None
        }
      ],
      "suggested_deliverables": [
        { "title": "string", "description": "string", "acceptance_criteria": "string" }
      ],
      "risks_text": "string (phase-level bullets first if any flagged, then system-level risks)",
      "assumptions_text": "string (one assumption per line, newline escaped '\\n')",
      "metadata": {
        "total_hours_realistic": 0,
        "capacity_hours_available": None,
        "deadline_feasible": False,
        "risk_message": "",
        "dropped_phases": [],
        "allow_overflow_requested": bool(allow_overflow_requested),
        "allow_overflow_used": False,
        "overflow_hours": 0,
        "overflow_plan": None,
        "primary_recommendation": None,
        "primary_recommendation_rationale": ""
      }
    }

    # Prepare days until deadline (nullable)
    days_until_deadline = None
    deadline_raw = proposal.get("deadline") or proposal.get("deadline_date")
    try:
        if deadline_raw:
            if isinstance(deadline_raw, (date, datetime)):
                d_obj = deadline_raw if isinstance(deadline_raw, date) else deadline_raw.date()
            else:
                d_obj = datetime.fromisoformat(str(deadline_raw)).date()
            today = date.today()
            days_until_deadline = max(0, (d_obj - today).days)
    except Exception:
        days_until_deadline = None

    # Build the prompt body (variables inserted safely)
    header = (
        "You are a Senior Project Manager and Solution Architect.\n"
        "Produce a single JSON object exactly matching the provided OUTPUT_SCHEMA (no markdown, no extra text).\n\n"
        "KEY REQUIREMENTS:\n"
        "- ALL phase durations MUST be TOTAL MAN-HOURS (integer) in field `duration_hours`.\n"
        "- Return a sensible number of phases chosen by you (the LLM) based on the project's scope, complexity and the deadline.\n"
        "- The assistant MAY return between 1 and " + str(max_phases) + " phases. Choose the number that best balances clarity and feasibility.\n"
        "- Provide `original_hours` when you compress a phase and `compression_pct` where applicable.\n"
        "- metadata.total_hours_realistic MUST equal the integer SUM of all suggested_phases.duration_hours.\n"
        "- **Deadline check (CONSERVATIVE / MANDATORY):** When deciding feasibility against a provided deadline, do NOT assume parallel execution across team members.\n"
        "  Compute calendar_days_required = ceil( SUM(duration_hours) / 8 ). Compare calendar_days_required to the days available until the deadline (days_until_deadline).\n"
        "  If days_until_deadline is provided and calendar_days_required <= days_until_deadline then metadata.deadline_feasible = true. Otherwise metadata.deadline_feasible = false and run the ADAPTATION ALGORITHM.\n"
        "- If capacity is known and allow_overflow is false, do NOT return total_hours_realistic > capacity (apply compression/dropping rules if needed).\n"
        "- If compressing/dropping, document every change in metadata.dropped_phases, metadata.risk_message and assumptions_text.\n"
        "- Provide structured phase-level risks in metadata.phase_risks if any phase is flagged (duration too short, compressed > allowed, external dependency risk).\n\n"
    )

    # context summary (safe insert)
    context = {
        "client": client,
        "goal": project_goal,
        "scope": scope,
        "tech_stack": techs,
        "team_size": team_size,
        "capacity_hours_available": total_team_capacity_hours,
        "target_budget_hours": target_budget_hours,
        "allow_overflow_requested": bool(allow_overflow_requested),
        "deadline_days_available": days_until_deadline if days_until_deadline is not None else "null",
        "deadline": proposal.get("deadline") or proposal.get("deadline_date") or ""
    }

    # instruction block with schema injected via json.dumps to avoid braces issues
    prompt_parts = [
        header,
        "INPUT SUMMARY:\n" + json.dumps(context, ensure_ascii=False, indent=2) + "\n\n",
        "OUTPUT_SCHEMA (return EXACTLY one JSON object following this schema):\n",
        json.dumps(schema, ensure_ascii=False, indent=2),
        "\n\nADAPTATION ALGORITHM (MANDATORY - execute in this order):\n"
        "1) Compute honest baseline (original_hours per phase) and SUM total_hours_realistic.\n"
        "2) Perform the CONSERVATIVE deadline check (do NOT assume parallelism):\n"
        "     calendar_days_required = ceil(total_hours_realistic / 8)\n"
        "     Compare calendar_days_required to deadline_days_available (from INPUT SUMMARY).\n"
        "     If calendar_days_required <= deadline_days_available -> set metadata.deadline_feasible = true and return realistic plan.\n"
        "3) If baseline > capacity -> apply in order:\n"
        "   A) Re-sequence where logically possible (this does NOT change SUM of man-hours and is NOT to be used to claim deadline feasibility — deadline check remains conservative single-stream).\n"
        "   B) Compress 'should'/'optional' phases up to aggressive_compression_limit_pct each (document per-phase compression)\n"
        "   C) Drop 'optional' phases (list in metadata.dropped_phases)\n"
        "   D) If still > capacity or deadline infeasible:\n"
        "        - If allow_overflow true: set allow_overflow_used=true and provide overflow_plan and overflow_hours\n"
        "        - Else: set deadline_feasible=false and pick one primary_recommendation with numeric rationale (extend_deadline | add_fte | compress_scope | accept_risk_with_mitigation)\n\n"
        "PHASE RISK RULES (MANDATORY):\n"
        "- too_short_threshold_h = 8\n"
        "- preferred_must_phase_h = 24\n"
        "- aggressive_compression_limit_pct = 30\n"
        "- If any phase triggers a 'too short' or 'compression > allowed' condition, the assistant MUST:\n"
        "  1) add a detailed object to metadata.phase_risks (array of structured risk objects),\n"
        "  2) add a short bullet (one line) describing the same phase-level risk UNDER the appropriate severity in risks_text BEFORE system-level risks.\n"
        "- If no phase is flagged, omit metadata.phase_risks entirely and do not include phase-level bullets in risks_text.\n"
        "\n"
        "ADDITIONAL GUIDANCE:\n"
        "- Choose phase granularity appropriate to the project: fewer phases (big buckets) for short/urgent engagements, more phases (detailed workstreams) for long/complex programs.\n"
        "- Do NOT invent client-specific facts; only use the INPUT SUMMARY and reasonable assumptions — list all assumptions in assumptions_text (one per line).\n"
        "- Use '\\n' for paragraph breaks in narrative fields. All numbers as integers where specified.\n"
    ]

    prompt = "\n".join(prompt_parts)
    return prompt




import os
from docx import Document

def extract_request_from_proposal(proposal_file_path: str, output_file_path: str) -> str:
    """
    Извлекает текст миссии и резюме из документа Proposal, 
    которые максимально точно отражают исходный Запрос (Request) клиента.
    """
    try:
        # 1. Загрузка документа
        document = Document(proposal_file_path)
        
        # 2. Инициализация переменных для хранения извлеченного текста
        executive_summary = ""
        project_mission = ""
        
        # 3. Флаги для определения, в каком разделе мы находимся
        in_summary = False
        in_mission = False

        # 4. Поиск и извлечение текста
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            
            if "1. Executive Summary" in text or "1. Резюме" in text:
                in_summary = True
                in_mission = False
                continue
            
            if "2. Product Overview & Mission" in text or "2. Обзор продукта и миссия" in text:
                in_summary = False
                in_mission = True
                continue
            
            # Останавливаем извлечение, как только дойдем до следующего раздела (3. Technical...)
            if text.startswith("3. Technical Implementation Proposal") or text.startswith("3. Техническое Предложение"):
                break
                
            if in_summary and text and text != "Innovative Solutions LLC":
                # Добавляем текст, пропуская пустые строки и дублирование заголовка
                executive_summary += text + "\n"
            
            if in_mission and text and text != "Innovative Solutions LLC":
                # Добавляем текст, пропуская пустые строки и дублирование заголовка
                project_mission += text + "\n"

        # 5. Формирование итогового текста запроса
        request_text = (
            f"=== ИСХОДНЫЙ ЗАПРОС КЛИЕНТА (РЕКОНСТРУКЦИЯ) ===\n\n"
            f"--- 1. РЕЗЮМЕ ЗАДАЧИ (Executive Summary) ---\n"
            f"{executive_summary.strip()}\n\n"
            f"--- 2. МИССИЯ ПРОЕКТА (Project Mission) ---\n"
            f"{project_mission.strip()}"
        )

        # 6. Сохранение в отдельный файл
        with open(output_file_path, 'w', encoding='utf-8') as f:
            f.write(request_text)
            
        return f"Исходный запрос успешно извлечен и сохранен в файл: {os.path.abspath(output_file_path)}"

    except FileNotFoundError:
        return f"Ошибка: Файл {proposal_file_path} не найден."
    except Exception as e:
        return f"Произошла ошибка при обработке документа: {e}"

# --- ЗАПУСК ---
proposal_filename = "Proposal_Innovative Solutions LLC(10).docx"
output_filename = "Extracted_Client_Request.txt"

# Убедитесь, что файл {proposal_filename} находится в той же директории, что и скрипт
result_message = extract_request_from_proposal(proposal_filename, output_filename)
print(result_message)