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
    """
    Строит промпт для генерации полного документа. 
    Включает логику учета Team Size и сокращения Scope.
    """
    client = proposal.get("client_company_name") or proposal.get("client_name") or ""
    provider = proposal.get("provider_company_name") or proposal.get("provider_name") or ""
    project_goal = proposal.get("project_goal", "")
    scope = proposal.get("scope", "")
    technologies = proposal.get("technologies") or []
    techs = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)
    deadline = proposal.get("deadline", "")
    manual_deliverables = proposal.get("deliverables", [])
    manual_phases = proposal.get("phases", [])
    deliverables_input_str = json.dumps(manual_deliverables, indent=2, ensure_ascii=False) if manual_deliverables else "[]"
    phases_input_str = json.dumps(manual_phases, indent=2, ensure_ascii=False) if manual_phases else "[]"
    
    team_size = proposal.get("team_size", 1)

    backend_tech = "Python (FastAPI)"
    frontend_tech = "Не указан (API-only)"

    # --- compute available time in hours ---
    time_available_hours = "N/A"
    total_team_capacity_hours = "Unknown"
    
    try:
        deadline_raw = proposal.get("deadline", "")
        if deadline_raw:
            if isinstance(deadline_raw, date):
                deadline_str = deadline_raw.strftime("%Y-%m-%d")
            else:
                deadline_str = str(deadline_raw)
            deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()
            today = date.today()
            if deadline_date > today:
                time_delta = deadline_date - today
                # Расчет рабочих дней (5/7)
                import math
                work_days = max(0, math.floor(time_delta.days * (5/7)))
                available_hours_single = work_days * 8
                
                # Общая емкость команды
                total_capacity = available_hours_single * team_size
                
                if total_capacity < 8:
                    total_capacity = 8
                
                time_available_hours = f"{total_capacity} hours (Team Size: {team_size})"
                total_team_capacity_hours = str(total_capacity)
            else:
                time_available_hours = "0 hours (deadline passed)"
                total_team_capacity_hours = "0"
    except Exception:
        pass

    # adjust tech hints
    if isinstance(technologies, list) and technologies:
        py_techs = [t for t in technologies if isinstance(t, str) and t.lower() in ('python', 'fastapi', 'django')]
        js_techs = [t for t in technologies if isinstance(t, str) and t.lower() in ('react', 'vue', 'angular', 'frontend')]
        if py_techs:
            backend_tech = ", ".join(py_techs)
        elif not js_techs:
            backend_tech = "Node.js (Express/NestJS)"
        if js_techs:
            frontend_tech = ", ".join(js_techs)
        elif not py_techs and not js_techs:
            backend_tech = f"Указано: {techs}"
            frontend_tech = "Не указан"
    
    prompt = f"""
You are the "Expert Committee" from the company "{provider}", preparing a Commercial Proposal (CP) for "{client}".
You must INTERNALLY perform role-based reasoning,
and then output a SINGLE JSON that exactly matches the REQUESTED KEYS.

### PROJECT INPUT DATA:
* **Client:** "{client}"
* **Contractor:** "{provider}"
* **Project Goal:** "{project_goal}"
* **Description (Scope):** "{scope}"
* **Technologies (Input):** "{techs}"
* **Deadline:** "{deadline}"
* **Team Size:** {team_size} people
* **TOTAL TEAM CAPACITY (CRITICAL LIMIT):** {total_team_capacity_hours} hours
* **Tone:** "{tone}"

---

### USER-PROVIDED DATA (Source of Truth):
* **Provided Deliverables:** {deliverables_input_str}
* **Provided Phases:** {phases_input_str}

---

### OVERALL GUIDELINES (do not change input names or schema)
1. ALL generated narrative text MUST be **project-specific**. ...
2. ALL lists of items (deliverables, phases, components, milestones) MUST include, where applicable: **owner**, **purpose**, **acceptance criteria**, and **reasonable effort estimate** (`duration_hours` as integer).
3. Keep and enforce the original JSON output schema and keys exactly. Use `\\n` in JSON strings for newlines. Highlight key concepts and technologies in **bold Markdown** inside all text fields.
4. Be conservative: when making inferences (durations, owners, tasks) prefer minimal safe assumptions and state them in `assumptions_text`.
5. Format: use Markdown headings for sections, and `\\n` (escaped newline) inside JSON text fields. Use `**Role:**` and a newline for each role entry under `team_structure_text`.
6. **NO SIGN-OFFS:** Do NOT end sections with "Sincerely,", "Prepared by", or the company name. Output ONLY the content of the section.
7. **Capacity Rule:** You have {team_size} developers and {total_team_capacity_hours} hours total. You MUST respect this limit.
---

### STEP 1: INTERNAL REASONING (Internal Reasoning - DO NOT SHOW IN JSON)
Perform role-specific internal reasoning. For each role, include a short internal planning checklist (these are internal notes and should NOT be placed directly into final free-text fields except where the schema demands fields derived from them).

1. **Agent "Solution Architect":**
    * **Estimation:** Estimate the total effort required for the full scope.
    * **Capacity Check:** Compare Estimated Effort vs {total_team_capacity_hours} hours.
    * **STRATEGY (CRITICAL):** - IF Estimated Effort > {total_team_capacity_hours}: 
        You MUST **DROP** non-essential phases (e.g., "Nice-to-have UI", "Advanced Analytics") or **COMPRESS** time (e.g., "Lean QA").
      - You must NOT propose a schedule that exceeds the capacity significantly.
    * Primary objective: produce a **technical design** explicitly mapped to the provided `scope` (e.g., CRM↔Shopify integration), the `technologies` input, and the **{time_available_hours}** constraint.
    * **Architecture priority:** choose the simplest architecture that meets project goals within the available time: favor managed services, tested libraries, and standard integration patterns (webhooks, retry queues, idempotent APIs).
    * **Deliverables from this agent (exact fields to produce):**
        - `technical_backend_text`: Must include **Solution Architecture**, **Backend Stack ({backend_tech})**, **Database**, **API Contracts** (list of endpoints with purpose & brief payload summary), and **Error/Retry Strategy**. Each subsection must use a Markdown heading and `\\n`.
        - `technical_frontend_text`: If frontend is out-of-scope, state **explicitly** "API-only" and include "future UI considerations" with concrete suggestions (e.g., "admin dashboard to monitor sync status: endpoints required, sample views").
        - `technical_deployment_text`: Must include **CI/CD (DevOps)**, **Environments**, **Monitoring & Observability** (metrics, logs, alerting), and **Backup/Recovery** notes.
    * For every technical claim, if it depends on an assumption (e.g., API rate limits are acceptable), list that assumption in `assumptions_text`.
    * Provide a compact list `visualization.components` — each component must include `id`, `title`, `description` (single sentence tied to the project), `type`, and `depends_on`.

2. **Agent "Project Manager (PM)":**
    * **Audit the PM's Strategy:**
      - IF the PM dropped a phase, create a risk: `* **Scope Reduction:** To meet the deadline, [Phase Name] was excluded. **Impact:** [Consequence].`
      - IF the PM compressed time, create a risk: `* **Quality Risk:** [Phase] duration compressed. **Impact:** Higher risk of bugs.`
      - IF Team Size > 1, create a risk: `* **Resourcing:** Requires {team_size} FTEs working in parallel.`
    * **Analysis:** Compare the Ideal Scope Effort vs **{time_available_hours}**.
    * **Strategy:** - If Ideal Effort > Available Time: You MUST **drop non-critical phases** (e.g., "Advanced Reporting", "Nice-to-have UI") OR **compress durations** (e.g., reduce QA time, remove Load Testing).
        - **CRITICAL:** Remember exactly what you dropped or compressed. This is a trade-off.
    * **Deliverables (CRITICAL):**
        - If `Provided Deliverables` is NOT empty: use the provided list as `suggested_deliverables` but **augment** each entry with `description`, `acceptance` (specific acceptance tests or criteria) and a likely `owner` and `effort_estimate_hours` if missing. 
        - If `Provided Deliverables` is empty: generate `suggested_deliverables` from `scope`.
    * **Phases (CRITICAL):**
        - If `Provided Phases` is NOT empty: use them as `suggested_phases` and `visualization.milestones`. If they miss `duration_hours` or `key_tasks`, infer them conservatively.
        - If `Provided Phases` is empty: generate `suggested_phases` where the sum of `duration_hours` **MUST NOT exceed** the available whole hours in **{time_available_hours}** (assuming 1 FTE), unless impossible...
        - Each phase object must include `name`, `description`, `duration_hours` (integer), and `key_tasks`.
    * **Team:** For each required role produce an item in `team_structure_text` using `**Role:**\\n` followed by 3–6 concrete bullets.

3. **Agent "QA Lead":**
    * Given **{time_available_hours}**, propose a **lean, automation-first** QA strategy: unit tests, contract/API tests, CI gate, and a compressed UAT plan.
    * Provide `qa_strategy_text` including **Test Coverage Targets**, **Automation Scope**, **Testing Tools**, and **UAT approach** (how client will perform and sign-off).
    * Provide `qa_testing_types_text`: use Markdown headings and ensure each testing type is accompanied by a one-sentence project-specific example (e.g., **API Testing:** Verify webhook retry and idempotency for order updates between Shopify and CRM).
    * If scope was reduced, mention "MVP Approach" in `executive_summary_text`.
    * Ensure `phases_summary_text` explains the logic of the chosen schedule.
4. **Agent "Risk Manager":**
    * **Analyze the PM's Strategy:** Look at the `suggested_phases`. Did the PM have to cut corners to meet the deadline?
    * **MANDATORY RISK REPORTING:**
      - If any standard phase was **dropped** (e.g., "Load Testing skipped"), you MUST add a risk: `* **Scope Reduction:** To meet the deadline, [Phase Name] was excluded. **Impact:** [Consequence].`
      - If any phase was **compressed** (e.g., "QA reduced by 50%"), you MUST add a risk: `* **Quality Risk:** QA duration is compressed. **Impact:** Higher risk of post-launch bugs.`
      - If the schedule requires **Team Scaling** (more than 1 dev), add a risk: `* **Resourcing Risk:** Timeline requires parallel execution (Team Size > 1).`
    * Format these findings into `risks_text` using Markdown bullets.
    * Identify and list **project-specific** risks and tie each risk to a **mitigation** and an **owner** (who will take responsibility for mitigation).
    * IMPORTANT: If the **USER-PROVIDED DATA** (the `Provided Phases` or `metadata`) contains a non-empty `dropped_phases` list (or any phases marked removed/priority:"optional"), you MUST add a risk entry for each dropped phase using the pattern:
      `* **Scope Reduction:** [Dropped Phase Name]. **Impact:** <short consequence>. **Mitigation:** <plan>. **Owner:** <role>.`
    * Produce `assumptions_text` and `risks_text`. Each bullet MUST be on a new line and risks should use the pattern `* **Risk:** Description. **Mitigation:** ... **Owner:** ...`.

    * Produce `assumptions_text`.

5. **Agent "Technical Writer":**
    * Aggregate all agent outputs and produce final copy that is consistent across sections.
    * Ensure **every** text block in the final JSON is:
        - Project-specific,
        - Uses Markdown headings where required,
        - Highlights key terms in **bold**,
        - Uses `\\n` for newlines,
        - Consistent with `suggested_phases` and `suggested_deliverables`.
    * When the schema requests 2–3 paragraphs, produce exactly that amount of paragraphs (no more, no less).
    * `executive_summary_text`: If risks are high (phases dropped), mention that the proposal focuses on an **MVP** approach.
    * Make `executive_summary_text` and `project_mission_text` clearly map to the client's business outcomes and the deliverables (e.g., reduced manual data entry, real-time customer sync).

---

### STEP 2: FINAL JSON (Final JSON Output)
(RETURN ONLY THIS JSON OBJECT. Ensure JSON strings containing formatting use `\\n` for a newline.
**CRITICAL:** For readability, highlight key terms in **bold Markdown** within all text fields.)

{{
    // --- Sections 1-5 (General) ---
    "executive_summary_text": "(Detailed text from Technical Writer. 3-4 paragraphs. Must be consistent with the phases/deliverables. If scope was reduced, mention this is an MVP delivery)",
    "project_mission_text": "(Detailed text from Technical Writer. 3-4 paragraphs)",

    // --- Section 6 (Assumptions and Risks) ---
    "assumptions_text": " (Text from Risk Manager. Each point MUST be on a new line with `\\n`. \\n* Assumption 1...\\n* Assumption 2...)",
    "risks_text": "(Text from Risk Manager. MUST include Scope Reductions/Compressions if applicable. \\n* **Risk 1:** ... \\n* **Scope Trade-off:** To meet the {deadline} deadline, we excluded [Feature X]. **Impact:** ...)",(CRITICAL: Must include Scope Reductions/Compressions. \\n* **Risk 1:** ... \\n* **Scope Trade-off:** To meet the deadline, we excluded...)",
    // --- Section 7 (Technical Solution) ---
    "technical_backend_text": " (Text from Solution Architect. MUST include Markdown headings and `\\n`. \\n**Solution Architecture:**\\nDescription including API contracts and error strategy...\\n**Backend Stack ({backend_tech}):**\\nDescription...\\n**Database (PostgreSQL/MongoDB):**\\nDescription...\\n**API Contracts:**\\n- POST /sync/products -> purpose, brief payload, acceptance...)",
    "technical_frontend_text": " (Text from Solution Architect. MUST include Markdown headings and `\\n`. \\n**UI Approach ({frontend_tech}):**\\nDescription including "API-only" or minimal admin UI requirements...\\n**Responsiveness and Accessibility:**\\nDescription (if applicable)...)",
    "technical_deployment_text": "(Text from Solution Architect. MUST include Markdown headings and `\\n`. \\n**CI/CD (DevOps):**\\nDescription including pipeline gates and automated test steps...\\n**Environments:**\\nDescription (Dev, Staging, Prod) and monitoring...)",
    "engagement_model_text": "(Text from PM. 2-3 paragraphs. Justification for Fixed Price or T&M)",

    // --- Section 8 (Project Execution) ---
    "delivery_approach_text": " (Text from PM. MUST include Markdown headings and `\\n`. \\n**Methodology (Agile/Scrum):**\\nDescription...\\n**Change Management:**\\nDescription...)",
    "team_structure_text": "(Text from PM. MUST include `**Role:**` and `\\n` for EVERY role. Each role MUST list 3-6 concrete tasks tied to the PROJECT. Example: \\n**Lead Backend Engineer:**\\nImplement FastAPI endpoints for product import; Design audit log schema; Ensure idempotent sync flows; Write unit tests for mapping logic.)",
    "status_reporting_text": " (Text from PM. MUST include Markdown headings and `\\n`. \\n**Communications and Meetings:**\\nDescription (Daily standups, Sprint Demos, UAT windows)...\\n**Tools:**\\nDescription (Jira, Slack, Confluence)...)",
    
    // --- NEW KEY (Section 8b) ---
    "phases_summary_text": "(Text from PM/Writer. 3-4 paragraphs. A narrative summary of the phases and deliverables. MUST NOT just repeat the lists. Must explain the flow and connection between stages. The **phases_summary_text** MUST NOT simply repeat the list or diagram data. It must be a 3-4 paragraph narrative.)",

    // --- Section 9 (Quality Assurance) ---
    "qa_strategy_text": " (Text from QA Lead. MUST include Markdown headings and `\\n`. \\n**Overall QA Strategy:**\\nDescription...\\n**Test Documentation (TestRail):**\\nDescription...\\n**Tools:**\\nDescription...)",
    "qa_testing_types_text": " (Text from QA Lead. MUST include Markdown headings and `\\n`. \\n**Types of QA Testing:**\\nDescription...\\n**Functional Testing:**\\nProject-specific examples...\\n**Non-Functional Testing:**\\nPerformance and reliability tests descriptions...\\n**Regression Testing:**\\nDescription...\\n**Integration Testing:**\\nDescription...\\n**User Acceptance Testing (UAT):**\\nDescription with acceptance criteria...)",

    // --- Section 10 (Finance - Notes) ---
    "financial_justification_text": "(2-3 paragraphs from Technical Writer about ROI, specifically referencing time savings and error reduction from automation.)",
    "payment_terms_text": "(2-3 paragraphs from Technical Writer about payment conditions)",
    "development_note": "(2-3 sentences)",
    "licenses_note": "(1-2 sentences)",
    "support_note": "(1-2 sentences)",

    // --- Lists for Tables (Deliverables & Phases) ---
    "suggested_deliverables": [
        // (List of Deliverables from PM. Detailed.)
        {{"title": "Project Knowledge Base (Confluence)", "description": "Complete project knowledge base, including specifications, User Stories, and diagrams.", "acceptance": "Documentation is current and approved"}},
        {{"title": "Source Code (GitLab/GitHub)", "description": "Full access to source code with CI/CD pipelines.", "acceptance": "Code has passed review and meets standards"}},
        {{"title": "Deployed Staging & Production Environments", "description": "Configured and operational environments for testing and production.", "acceptance": "Environments are deployed and stable"}}
    ],
    "suggested_phases": [
        // (List of Phases from PM. Detailed and matches milestones)
        {{"phase_name": "Phase 1: Analysis and Design (Discovery)", "duration_hours": 8, "tasks": "Requirements gathering, finalization of specifications, architecture design, environment setup."}},
        {{"phase_name": "Phase 2: Development (Implementation Sprints)", "duration_hours": 8, "tasks": "Backend API development, integration with CRM/E-commerce, UI development (if applicable), Unit tests."}},
        {{"phase_name": "Phase 3: Stabilization and UAT", "duration_hours": 16, "tasks": "Comprehensive QA, API testing, UAT (User Acceptance Testing), bug fixing."}},
        {{"phase_name": "Phase 4: Deployment and Support", "duration_hours": 24, "tasks": "Deployment to Production, training, handover of documentation, launch of support."}}
    ],

    // --- Data for Diagrams (Synchronized with agents) ---
    "visualization": {{
        "components": [
            // (List of Components from Solution Architect)
            {{"id": "user", "title": "User (Admin)", "description": "...", "type": "ui", "depends_on": []}},
            {{"id": "frontend", "title": "Frontend ({frontend_tech})", "description": "...", "type": "ui", "depends_on": ["user"]}},
            {{"id": "api_gw", "title": "API Gateway ({backend_tech})", "description": "...", "type": "service", "depends_on": ["frontend"]}},
            {{"id": "crm_sync", "title": "CRM Synchronization Service", "description": "...", "type": "service", "depends_on": ["api_gw"]}},
            {{"id": "db", "title": "PostgreSQL Database", "description": "...", "type": "db", "depends_on": ["api_gw", "crm_sync"]}}
        ],
        "milestones": [
            // (List of Milestones from PM, EXACTLY MATCHES suggested_phases)
            {{"name": "Phase 1: Analysis and Design (Discovery)", "start": null, "end": null, "duration_days": 21, "percent_complete": 0, "owner": "Project Manager"}},
            {{"name": "Phase 2: Development (Implementation Sprints)", "start": null, "end": null, "duration_days": 56, "percent_complete": 0, "owner": "Backend Engineer"}},
            {{"name": "Phase 3: Stabilization and UAT", "start": null, "end": null, "duration_days": 21, "percent_complete": 0, "owner": "QA Engineer"}},
            {{"name": "Phase 4: Deployment and Support", "start": null, "end": null, "duration_days": 14, "percent_complete": 0, "owner": "DevOps"}}
        ],
        "infrastructure": [],
        "data_flows": [],
        "connections": []
    }}
}}
"""
    return prompt.strip()


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


def generate_ai_json(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Modify the function to check for lifecycle stages and generate them if missing.
    """
    if OPENAI_USE_STUB:
        # Create a deterministic stub compatible with the schema (fallback data)
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

    # Check if lifecycle stages exist in the proposal
    lifecycle_stages = proposal.get("lifecycle_stages", [])
    if not lifecycle_stages:
        logger.info("No lifecycle stages provided, using agent to generate stages.")
        # Здесь мы используем агент для генерации этапов жизненного цикла
        lifecycle_stages = _generate_lifecycle_stages_with_agent(proposal)

    # Ensure lifecycle stages are present
    if not lifecycle_stages:
        logger.error("No lifecycle stages available after agent generation")
        # Возвращаем детерминированный фоллбэк для консистентности, хотя лучше поднять ошибку
        return _invoke_with_fallback("", FALLBACK_AI_JSON_DICT_MINIMAL, expected_json_type=str)
        # raise ValueError("No lifecycle stages available")

    prompt = _build_prompt(proposal, tone)
    
    # Try cached fast path (KEEPING CACHE LOGIC HERE as it's separate from live invocation/fallback)
    try:
        cached = _invoke_openai_cached(prompt, OPENAI_MODEL)
        if cached:
            # Try to parse as JSON (to ensure it's not malformed)
            try:
                json.loads(cached)
                return cached
            except Exception:
                # Not strict JSON, still use it as text (this decision is kept from original)
                return cached
    except Exception:
        pass


    return _invoke_with_fallback(
        prompt=prompt,
        stub_value=FALLBACK_AI_JSON_DICT_MINIMAL, 
        expected_json_type=str
    )


def generate_suggestions(
    proposal: Dict[str, Any],
    tone: str = "Formal",
    max_deliverables: int = 10,
    max_phases: int = 10
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
            # cached is raw text; try parse JSON
            try:
                parsed = _clean_and_parse_json(cached, dict)
                if isinstance(parsed, dict):
                    return {
                        "suggested_deliverables": parsed.get("suggested_deliverables", []),
                        "suggested_phases": parsed.get("suggested_phases", [])
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


    return {
        "suggested_deliverables": parsed_result.get("suggested_deliverables", []),
        "suggested_phases": parsed_result.get("suggested_phases", [])
    }




def _build_suggestion_prompt(
    proposal: Dict[str, Any],
    tone: str = "Formal",
    max_deliverables: int = 8,
    max_phases: int = 8,
) -> str:
    """
    Builds a prompt where all timelines are in hours (1 workday = 8h).
    The returned JSON must use duration_hours and metadata.total_hours_realistic.
    Additional rule: IF the TRUE estimated hours exceed the computed team capacity,
    the model MUST remove the least-critical phases and return the list of removed
    phases in `metadata.dropped_phases` and include an explanatory `metadata.risk_message`.
    """
    deadline_str = proposal.get("deadline", "")
    team_size = proposal.get("team_size", 1) 
    
    total_team_capacity_hours = "null" 
    used_minimum = False
    
    if deadline_str:
        try:
            deadline_raw = proposal.get("deadline", "")
            if isinstance(deadline_raw, date):
                deadline_str = deadline_raw.strftime("%Y-%m-%d")
            else:
                deadline_str = str(deadline_raw)
            deadline_date = datetime.strptime(deadline_str, "%Y-%m-%d").date()
            today = date.today()
            
            if deadline_date > today:
                time_delta = deadline_date - today
                
                # Расчет рабочих дней (5/7)
                import math
                work_days = max(0, math.floor(time_delta.days * (5/7)))
                available_hours_single_dev = work_days * 8
                
                # *** ГЛАВНАЯ ЛОГИКА: УЧЕТ РАЗМЕРА КОМАНДЫ ***
                total_capacity = available_hours_single_dev * team_size
                
                if total_capacity < 8 and work_days > 0:
                    used_minimum = True
                    total_capacity = 8  # Правило: минимум 8 часов
                elif work_days == 0:
                    total_capacity = 0

                if total_capacity > 0:
                    total_team_capacity_hours = str(int(total_capacity))
                else:
                    total_team_capacity_hours = "0"
            else:
                total_team_capacity_hours = "0"
        except Exception:
            total_team_capacity_hours = "null"

    client = proposal.get("client_company_name") or proposal.get("client_name") or ""
    project_goal = proposal.get("project_goal", "") or proposal.get("goal", "")
    scope = proposal.get("scope", "") or proposal.get("description", "")
    technologies = proposal.get("technologies") or proposal.get("tech") or []
    techs = ", ".join(technologies) if isinstance(technologies, (list, tuple)) else str(technologies)

    prompt = f"""
You are an experienced IT/AI project manager and proposal architect. Produce a concise,
professional plan (deliverables + phased timeline) that fits the available schedule.

IMPORTANT: You MUST return exactly one valid JSON object and NOTHING ELSE — no explanations,
no markdown, no commentary. If you output anything other than the single JSON object, it will be
treated as invalid. 
INPUT DATA:
- Client: "{client}"
- Goal: "{project_goal}"
- Scope: "{scope}"
- Tech Stack: "{techs}"
- Hard Deadline Provided: "{deadline_str}"
- Team Size: {team_size} people

- **MAX TEAM CAPACITY (HOURS):** {total_team_capacity_hours} (Calculated as 8 working hours/day * 5 days/week * Weeks Available * {team_size})

OUTPUT SCHEMA (JSON only):
{{
  "suggested_deliverables": [
    {{ "title": "...", "description": "...", "acceptance": "..." }}
  ],
  "suggested_phases": [
    {{
      "phase_name": "...",
      "duration_hours": <INTEGER, realistic estimate regardless of deadline>,
      "tasks": "...",
      "owner": "...",
      "priority": "<must|should|optional>"
    }}
  ],
  "metadata": {{
    "total_hours_realistic": <INTEGER, sum of phases>,
    "capacity_hours_available": <INTEGER or null if unknown>,
    "deadline_feasible": <BOOLEAN>,
    "risk_message": "<String: warning message if not feasible, else empty>",
    "used_minimum_deadline": <BOOLEAN>,
    "dropped_phases": [ "<phase_name>", ... ]   // MUST list names of phases removed to fit deadline (can be empty)
  }}
}}

CRITICAL ADDITIONAL RULE (DROP-TO-FIT BEHAVIOR):
1. Compute the TRUE realistic effort for the full scope (baseline) and set `total_hours_realistic`.
2. Compare Baseline vs Capacity (`capacity_hours_available` = {total_team_capacity_hours}).
3. If Baseline <= Capacity:
   - Return suggested_phases covering the scope; set "deadline_feasible": true and "dropped_phases": [].
4. If Baseline > Capacity:
   - FIRST, try to **re-scope**: remove or move **optional** phases (lowest priority) to a `dropped_phases` list until the sum of remaining `duration_hours` <= Capacity.
   - SECOND, if necessary, compress "should" phases to realistic minimums (but never below 4 hours per phase) and recalc.
   - THIRD, if after removing all optional and compressing "should" phases the plan still exceeds Capacity, then:
       a) set "deadline_feasible": false,
       b) return the full honest baseline (all phases and their true `duration_hours`) in `suggested_phases`,
       c) set `metadata.dropped_phases` = [] and put a clear mismatch in `risk_message` explaining "requires Xh but only Yh available".
   - When you **do** remove phases to fit the deadline, ensure:
       * Removed phases are NOT present in `suggested_phases`.
       * Their names are included in `metadata.dropped_phases` (array of strings).
       * Set `deadline_feasible` = true (because the *new* plan fits) and put a short `risk_message` like: "Scope reduced: dropped [A, B] to fit deadline; see trade-offs."
5. Always include `priority` for each returned phase ("must" for MVP items).
6. Phase durations must be integers >= 4 hours.
7. The sum of `duration_hours` must equal metadata.total_hours_realistic.
8. If you removed phases, include a one-line justification for each removed phase inside `metadata.risk_message` (comma-separated short phrases).

STRATEGY SUMMARY (strict order of application):
A) Calculate honest baseline (realistic hours).
B) Try to fit by removing optional phases.
C) If still over, compress "should" phases but never below 4h each.
D) If still over, report honest baseline and set `deadline_feasible`: false.

VALIDATION BEFORE RETURN (self-check):
- Ensure schema keys exist exactly as above.
- Ensure all numbers are integers.
- Ensure `len(suggested_phases) <= {max_phases}` and `len(suggested_deliverables) <= {max_deliverables}`.
- Ensure `total_hours_realistic` equals the sum of `duration_hours`.
- Ensure `metadata.dropped_phases` is present (can be empty list).

Return only the JSON object — absolutely no extra text.
"""
    return prompt.strip()
