# streamlit_app.py
"""
Streamlit UI for AI Sales Proposal Generator
- Place in repository root and run: streamlit run streamlit_app.py
- Requires: streamlit, requests
"""
import os
import json
import requests
import streamlit as st
from datetime import date, datetime, timedelta
from typing import List, Dict, Any
import locale

# ---------------- Configuration ----------------
API_BASE_DEFAULT = os.getenv("PROPOSAL_API_BASE", "http://localhost:8000")
GENERATE_SUFFIX = "/api/v1/generate-proposal"
SUGGEST_SUFFIX = "/api/v1/suggest"  # suggestions endpoint
REGENERATE_SUFFIX = "/proposal/regenerate"

# Pydantic-derived constraints (mirror backend/models.py)
MIN_CLIENT_NAME = 2
MAX_CLIENT_NAME = 200
MIN_PROVIDER_NAME = 2
MAX_PROVIDER_NAME = 200
MIN_DELV_TITLE = 3
MAX_DELV_TITLE = 200
MIN_DELV_DESC = 10
MAX_DELV_DESC = 2000
MIN_DELV_ACC = 3
MAX_DELV_ACC = 1000
MIN_PHASE_TASKS = 3
MAX_PHASE_TASKS = 3000
MIN_PHASE_HOURS = 4     # Минимум полдня
MAX_PHASE_HOURS = 2080  
MIN_DEADLINE_DAYS = 14  # Снижаем порог, так как считаем в часах (можно даже меньше)

# ---------------- Helpers ----------------
def _format_currency(value) -> str:
    if value is None:
        return ""
    try:
        try:
            locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8')
        except locale.Error:
            try:
                locale.setlocale(locale.LC_ALL, 'Russian_Russia')
            except Exception:
                pass
        amount = float(value)
        formatted_value = locale.format_string("%.2f", amount, grouping=True)
        return f"{formatted_value}"
    except Exception:
        return str(value)

def build_api_urls(base: str):
    base = base.rstrip("/")
    return f"{base}{GENERATE_SUFFIX}", f"{base}{REGENERATE_SUFFIX}", f"{base}{SUGGEST_SUFFIX}"

def safe_date_to_iso(d):
    if d is None:
        return None
    if isinstance(d, str):
        return d
    return d.isoformat()

def validate_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Validate payload using HOURS logic.
    """
    errors: List[Dict[str, Any]] = []

    # Defensive guards
    if payload is None:
        return [{"loc": ["payload"], "msg": "payload is missing (None)"}]
    if not isinstance(payload, dict):
        return [{"loc": ["payload"], "msg": f"payload must be an object/dict, got {type(payload).__name__}"}]

    # ... (проверки имен client/provider оставляем как есть) ...

    # --- DEADLINE VALIDATION ---
    dl = payload.get("deadline")
    if dl:
        try:
            if isinstance(dl, str):
                d = date.fromisoformat(dl)
            elif isinstance(dl, (date, datetime)):
                d = dl if isinstance(dl, date) else dl.date()
            else:
                raise ValueError("invalid type")

            today_utc = datetime.utcnow().date()
            if d < today_utc:
                errors.append({"loc": ["deadline"], "msg": "deadline must not be in the past"})
            
            # Проверяем минимальный горизонт планирования (опционально)
            min_allowed = today_utc + timedelta(days=MIN_DEADLINE_DAYS)
            if d < min_allowed:
                errors.append({
                    "loc": ["deadline"],
                    "msg": f"deadline implies extremely tight schedule (<{MIN_DEADLINE_DAYS} days). Verify feasibility."
                })
        except Exception:
            errors.append({"loc": ["deadline"], "msg": "deadline invalid ISO date"})

    # financials validation
    fin = payload.get("financials") or {}
    if fin is None:
        fin = {}
    for k in ("development_cost", "licenses_cost", "support_cost"):
        v = fin.get(k)
        if v is not None and v != "":
            try:
                fv = float(v)
                if fv < 0:
                    errors.append({"loc": [k], "msg": "must be >= 0"})
            except Exception:
                errors.append({"loc": [k], "msg": "must be numeric"})

    # deliverables (backend expects list of dicts with acceptance_criteria)
    for i, d in enumerate(payload.get("deliverables", []) or []):
        if not isinstance(d, dict):
            errors.append({"loc": ["deliverables", i], "msg": "must be object"})
            continue
        if len((d.get("title") or "").strip()) < MIN_DELV_TITLE:
            errors.append({"loc": ["deliverables", i, "title"], "msg": f"title must be at least {MIN_DELV_TITLE} chars"})
        if len((d.get("description") or "").strip()) < MIN_DELV_DESC:
            errors.append({"loc": ["deliverables", i, "description"], "msg": f"description must be at least {MIN_DELV_DESC} chars"})
        if len((d.get("acceptance_criteria") or "").strip()) < MIN_DELV_ACC:
            errors.append({"loc": ["deliverables", i, "acceptance_criteria"], "msg": f"acceptance_criteria must be at least {MIN_DELV_ACC} chars"})

    # phases: require phase_name, duration_weeks, tasks
    phases = payload.get("phases", []) or []
    for i, p in enumerate(phases):
        if not isinstance(p, dict):
            errors.append({"loc": ["phases", i], "msg": "must be object"})
            continue

        # phase_name
        phase_name = p.get("phase_name") or p.get("name") or ""
        if not isinstance(phase_name, str) or len(phase_name.strip()) < 3:
            errors.append({"loc": ["phases", i, "phase_name"], "msg": "phase_name must be at least 3 characters"})

        # duration_hours validation
        val = p.get("duration_hours")
        # Если вдруг пришли недели (легаси), пытаемся конвертировать, но валидируем результат
        if val is None and p.get("duration_weeks"):
             val = int(p.get("duration_weeks")) * 40
        
        try:
            h = int(val)
            if h < MIN_PHASE_HOURS or h > MAX_PHASE_HOURS:
                errors.append({"loc": ["phases", i, "duration_hours"], 
                               "msg": f"duration must be between {MIN_PHASE_HOURS} and {MAX_PHASE_HOURS} hours"})
        except Exception:
            errors.append({"loc": ["phases", i, "duration_hours"], "msg": "must be integer (hours)"})

        if len((p.get("tasks") or "").strip()) < MIN_PHASE_TASKS:
            errors.append({"loc": ["phases", i, "tasks"], "msg": f"tasks must be at least {MIN_PHASE_TASKS} chars"})

    return errors


# add_selected_suggestions unchanged (kept minimal)
def add_selected_suggestions(list_type: str):
    selected_count = 0
    if list_type == 'deliverables':
        suggestions = st.session_state.get("suggestions_data", {}).get("suggested_deliverables", [])
        target_state = "deliverables_state"
        prefix = "sdeliv_pick_"
    else:
        suggestions = st.session_state.get("suggestions_data", {}).get("suggested_phases", [])
        target_state = "phases_state"
        prefix = "sphase_pick_"

    if not suggestions:
        return

    for i, item in enumerate(suggestions):
        checkbox_key = f"{prefix}{i}"
        if st.session_state.get(checkbox_key):
            if list_type == 'deliverables':
                acceptance_text = item.get('acceptance', item.get('acceptance_criteria', ''))
                st.session_state.setdefault(target_state, []).append({
                    "title": item.get("title",""),
                    "description": item.get("description",""),
                    "acceptance_criteria": acceptance_text
                })
            else:

                raw_hours = item.get("duration_hours")
                
                if raw_hours is not None:
                    duration_hours = int(raw_hours)
                else:
                    # --- SMART DEFAULT ---
                    # Если LLM не дала часы, не берем тупо 160 (4 недели).
                    # Попробуем взять (Оставшееся время / кол-во выбранных фаз), но не менее 8 часов.
                    # Для простоты здесь берем консервативные 40 часов (1 неделя), а не 160.
                    weeks = item.get("duration_weeks", item.get("duration", 1)) # Default 1 неделя
                    duration_hours = int(float(weeks) * 40)
                
                # Защита от гигантизма
                if duration_hours > 320: # Если более 2 месяцев на фазу
                     duration_hours = 160 # Срезаем до 1 месяца

                st.session_state.setdefault(target_state, []).append({
                    "phase_name": item.get("phase_name", f"Phase {i+1}"),
                    "duration_hours": duration_hours,
                    "tasks": item.get("tasks","")
                })
            st.session_state[checkbox_key] = False
            selected_count += 1

    if selected_count > 0:
        st.rerun()

# ---------------- UI ----------------
st.set_page_config(page_title="AI Sales Proposal Generator", layout="wide")
st.title("AI Sales Proposal Generator ")

# --- Sidebar Configuration ---
with st.sidebar:
    st.header("Backend / Settings")
    api_base = st.text_input("API base URL", API_BASE_DEFAULT)
    timeout_sec = st.number_input("Request timeout (s)", min_value=5, max_value=1300, value=1300, step=5)
    st.markdown("**Tips:** Set API base to your FastAPI host, e.g. http://localhost:8000")

generate_url, regenerate_url, suggest_url = build_api_urls(api_base)

# --- Proposal brief ---
st.header("1. Proposal Brief & Financials")
col_left, col_right = st.columns([2, 1])

with col_left:
    # NOTE: change labels and keys to match template/backend expected names
    client_company_name = st.text_input("Client company name ", value="ООО Инновационные Решения", key="client_company_name")
    provider_company_name = st.text_input("Provider company name", value="Digital Forge Group", key="provider_company_name")
    project_goal = st.text_input("Project goal (short)", value="Integrate CRM and migrate e-commerce platforms", key="project_goal")
    scope = st.text_area("Scope (detailed)", height=150, value="Migrate catalog, sync customers, create REST API for data sync.", key="scope")
    technologies = st.text_input("Technologies (comma-separated)", value="Python, FastAPI, Shopify", key="technologies")
    tone = st.selectbox("Tone", options=["Formal", "Marketing", "Technical", "Friendly"], index=0, key="tone")
    
with col_right:
    st.subheader("Dates & Financials (USD)")
    deadline = st.date_input("Expected completion date", value=date.today(), key="deadline")
    st.caption(f"Deadline must be at least {MIN_DEADLINE_DAYS} weeks from today.")
    development_cost = st.number_input("Development cost", min_value=0.0, value=45000.0, step=100.0, format="%.2f", key="development_cost")
    licenses_cost = st.number_input("Licenses cost", min_value=0.0, value=5000.0, step=50.0, format="%.2f", key="licenses_cost")
    support_cost = st.number_input("Support & maintenance", min_value=0.0, value=2500.0, step=50.0, format="%.2f", key="support_cost")
    total_cost = (development_cost or 0.0) + (licenses_cost or 0.0) + (support_cost or 0.0)
    st.markdown("---")
    st.markdown(f"**Total Estimated Investment:**")
    st.markdown(f"### $ {_format_currency(total_cost)}")
    team_size = st.number_input("Team Size (FTEs)", min_value=1, max_value=20, value=1, step=1, 
                                help="How many people will work on this project in parallel?")

# --- Build payload helper (USED BY BACKEND) ---
def build_payload(include_manual_deliverables=True, include_manual_phases=True):
    """
    IMPORTANT:
    This function intentionally uses the exact key names:
      - client_company_name, provider_company_name
      - deliverables: list of {title, description, acceptance_criteria}
      - phases: list of {phase_name, duration_weeks, tasks}
      - financials: nested object
    These keys match the backend/template expectations.
    """
    payload = {
        "client_company_name": client_company_name.strip(),
        "provider_company_name": provider_company_name.strip(),
        "project_goal": project_goal.strip(),
        "scope": scope.strip(),
        "technologies": [t.strip() for t in technologies.split(",") if t.strip()],
        "deadline": safe_date_to_iso(deadline),
        "tone": tone,
        "proposal_date": safe_date_to_iso(date.today()),
        "valid_until_date": safe_date_to_iso(date.today()),
        "financials": {
             "development_cost": development_cost,
             "licenses_cost": licenses_cost,
             "support_cost": support_cost,
        }
    }
    # include deliverables and phases in canonical shape expected by backend
    if include_manual_deliverables:
        payload["deliverables"] = st.session_state.get("deliverables_state", [])
    else:
        payload["deliverables"] = []
    if include_manual_phases:
        payload["phases"] = [
            {
                "phase_name": p.get("phase_name",""),
                "duration_weeks": int(p.get("duration_weeks",4)),
                "tasks": p.get("tasks","")
            }
            for p in st.session_state.get("phases_state", [])
        ]
    else:
        payload["phases"] = []

    # return the constructed payload (important!)
    return payload


# --- Buttons & status ---
st.markdown("---")
action_cols = st.columns([2, 1, 1, 1])

with action_cols[0]:
    btn_generate = st.button(" **Generate final DOCX**", type="primary", use_container_width=True)
with action_cols[1]:
    btn_suggest = st.button(" Get LLM suggestions", use_container_width=True)
with action_cols[2]:
    st.button("Clear Suggestions", key="clear_suggestions_btn", use_container_width=True, on_click=lambda: st.session_state.pop("suggestions_data", None))
with action_cols[3]:
    st.button("Clear manual lists", key="clear_lists_btn", use_container_width=True, on_click=lambda: st.session_state.update({"deliverables_state":[],"phases_state":[]}))

generation_status = st.empty()

# --- Manual editors (deliverables/phases) ---
st.markdown("---")
st.header("2. Deliverables & Phases (Manual Input)")
edit_cols = st.columns(2)

with edit_cols[0]:
    st.subheader("Deliverables")
    if "deliverables_state" not in st.session_state:
        st.session_state["deliverables_state"] = []
    def add_empty_deliverable():
        st.session_state["deliverables_state"].append({"title":"", "description":"", "acceptance_criteria":""})
    st.button("Add new deliverable", on_click=add_empty_deliverable, key="add_deliv_btn")
    for idx, d in enumerate(st.session_state["deliverables_state"]):
        with st.expander(f"Deliverable #{idx+1}: {d.get('title','(Click to edit)')}", expanded=False):
            t = st.text_input(f"Title #{idx+1}", value=d.get("title",""), key=f"deliv_title_{idx}", max_chars=MAX_DELV_TITLE)
            desc = st.text_area(f"Description #{idx+1}", value=d.get("description",""), key=f"deliv_desc_{idx}", max_chars=MAX_DELV_DESC)
            acc = st.text_input(f"Acceptance criteria #{idx+1}", value=d.get("acceptance_criteria",""), key=f"deliv_acc_{idx}", max_chars=MAX_DELV_ACC)
            st.session_state["deliverables_state"][idx] = {"title":t, "description":desc, "acceptance_criteria":acc}
        if st.button(f"Remove Deliverable #{idx+1}", key=f"deliv_remove_{idx}"):
            st.session_state["deliverables_state"].pop(idx)
            st.rerun()

with edit_cols[1]:
    st.subheader("Phases / Timeline")
    if "phases_state" not in st.session_state:
        st.session_state["phases_state"] = []
    def add_empty_phase():
        st.session_state["phases_state"].append({"phase_name":"", "duration_hours":4, "tasks":""})
    st.button("Add new phase", on_click=add_empty_phase, key="add_phase_btn")
    for idx, p in enumerate(st.session_state["phases_state"]):
        summary_preview = p.get("phase_name") or "(Click to edit)"
        with st.expander(f"Phase #{idx+1}: {summary_preview}", expanded=False):
            name = st.text_input(f"Phase name #{idx+1}", value=p.get("phase_name",""), key=f"phase_name_{idx}", max_chars=200)
            hours = st.number_input(f"Duration (hours) #{idx+1}", value=p.get("duration_hours", 160),  # 160ч = 4 недели * 40ч
                        min_value=MIN_PHASE_HOURS, max_value=MAX_PHASE_HOURS, step=8,  # шаг 8 часов = 1 день
                        help="1 day = 8h, 1 week = 40h", key=f"phase_hours_{idx}")
            tasks = st.text_area(f"Tasks #{idx+1}", value=p.get("tasks",""), key=f"phase_tasks_{idx}", max_chars=MAX_PHASE_TASKS)
            st.session_state["phases_state"][idx] = {"phase_name":name, "duration_hours":int(hours), "tasks":tasks}
        if st.button(f"Remove Phase #{idx+1}", key=f"phase_remove_{idx}"):
            st.session_state["phases_state"].pop(idx)
            st.rerun()
    total_planned_hours = sum([int(p.get("duration_hours", 0)) for p in st.session_state["phases_state"]])

    if deadline:
        days_remaining = (deadline - date.today()).days
        import math
        # Считаем только рабочие дни (5/7)
        work_days = max(0, math.floor(days_remaining * (5/7)))
        
        # ВАЖНО: Умножаем на размер команды!
        capacity_per_person = work_days * 8
        total_team_capacity = capacity_per_person * team_size
        
        st.markdown("#### ⏳ Time Reality Check")
        c1, c2, c3 = st.columns(3)
        c1.metric("Planned Effort", f"{total_planned_hours} h")
        c2.metric("Team Capacity", f"{total_team_capacity} h", help=f"{work_days} days * 8h * {team_size} people")
        c3.metric("Delta", f"{total_team_capacity - total_planned_hours} h", 
                  delta_color="normal" if total_team_capacity >= total_planned_hours else "inverse")
        
        if total_planned_hours > total_team_capacity:
            st.warning(
                #f"to finish {total_planned_hours}h of work by {deadline}. You selected {team_size}."
            )

# --- Suggestion retrieval ---
if btn_suggest:
    st.session_state.pop("suggestions_data", None)
    payload = build_payload(include_manual_deliverables=False, include_manual_phases=False)
    if payload is None:
        generation_status.error("Payload construction failed (None). Please check inputs.")
    else:
        val_errs = validate_payload(payload)
        if val_errs:
            generation_status.error("**Fix validation errors** before requesting suggestions.")
            for e in val_errs:
                st.write(f"- **{'/'.join(map(str, e['loc']))}**: {e['msg']}")
        else:
            generation_status.info(" Requesting suggestions — please wait (calling /api/v1/suggest)")
            try:
                with st.spinner("Calling backend for suggestions..."):
                    r = requests.post(suggest_url, json=payload, timeout=timeout_sec)

                if r.status_code == 200:
                    data = r.json()
                    st.session_state["suggestions_data"] = data
                    
                    meta = data.get("metadata", {})
                    is_feasible = meta.get("deadline_feasible", True)
                    risk_msg = meta.get("risk_message", "")
                    total_est = meta.get("total_hours_realistic", 0)
                    cap = meta.get("capacity_hours_available", 0)

                    # Логика отображения
                    if is_feasible:
                        # Если план вписался, но часов много, значит предполагается команда
                        if cap > 0 and total_est > (cap * 1.2): # Если оценка больше емкости одного человека на 20%
                             generation_status.info(
                                f"**Plan Fits Deadline (with Team):**\n"
                                f"Total Effort: {total_est}h. Available (1 dev): {cap}h.\n"
                                f"**Suggestion:** This plan requires multiple developers working in parallel to meet the deadline."
                             )
                        else:
                             generation_status.success(f"Plan fits comfortably within the deadline.")
                    else:
                        # Если даже с оптимизацией не влезли
                        generation_status.error(
                            f"🚨 **Deadline Risk:** {risk_msg}\n\n"
                            f"Minimal Realistic Estimate: **{total_est}h** vs Available: **{cap}h**.\n"
                            "The generated proposal will include this as a Critical Risk."
                        )
                    
                    st.rerun()
                else:
                    try:
                        err = r.json()
                        generation_status.error(f"Server error ({r.status_code}): {err}")
                    except Exception:
                        generation_status.error(f"Server returned status {r.status_code}: {r.text}")
            except requests.RequestException as re:
                generation_status.error(f" **Request failed**: {re}. Check backend at {suggest_url}")

if st.session_state.get("suggestions_data"):
    st.markdown("---")
    st.header("3. LLM Suggestions (Preview)")
    data = st.session_state["suggestions_data"]
    s_delivs = data.get("suggested_deliverables") or []
    s_phases = data.get("suggested_phases") or []
    sugg_cols = st.columns(2)
    with sugg_cols[0]:
        if s_delivs:
            st.markdown("### Suggested Deliverables")
            st.caption("Check boxes to select items, then click 'Add Selected Deliverables'.")
            st.button("➕ Add Selected Deliverables", key="add_selected_delivs_btn", on_click=add_selected_suggestions, args=('deliverables',))
            for i, d in enumerate(s_delivs):
                checkbox_key = f"sdeliv_pick_{i}"
                st.session_state.setdefault(checkbox_key, False)
                with st.container():
                    c1, c2 = st.columns([0.08, 1])
                    c1.checkbox("", key=checkbox_key)
                    c2.markdown(f"**{d.get('title','(No Title)')}**")
                    st.caption(d.get('description',''))
                    st.text(f"Acceptance: {d.get('acceptance', d.get('acceptance_criteria',''))}")
    with sugg_cols[1]:
        if s_phases:
            st.markdown("### Suggested Phases")
            st.caption("Check boxes to select items, then click 'Add Selected Phases'.")
            st.button("➕ Add Selected Phases", key="add_selected_phases_btn", on_click=add_selected_suggestions, args=('phases',))
            for i, p in enumerate(s_phases):
                checkbox_key = f"sphase_pick_{i}"
                st.session_state.setdefault(checkbox_key, False)
                duration_hours = int(p.get("duration_hours", p.get("duration", 4)))
                with st.container():
                    c1, c2 = st.columns([0.08, 1])
                    c1.checkbox("", key=checkbox_key)
                    c2.markdown(f"**{p.get('phase_name','Phase')}** — **{duration_hours} hours**")
                    st.caption(p.get('tasks',''))

# --- Generate final DOCX ---
if btn_generate:
    payload = build_payload(include_manual_deliverables=True, include_manual_phases=True)
    if payload is None:
        generation_status.error("Payload construction failed (None). Please check inputs.")
    else:
        val_errs = validate_payload(payload)
        if val_errs:
            generation_status.error(" **Fix validation errors** before generating:")
            for e in val_errs:
                st.write(f"- **{'/'.join(map(str, e['loc']))}**: {e['msg']}")
        else:
            generation_status.info(" **Generating DOCX** — please wait")
            try:
                with st.spinner("Calling backend to generate DOCX..."):
                    r = requests.post(generate_url, json=payload, timeout=timeout_sec, stream=True)
                if r.status_code == 200:
                    ct = r.headers.get("Content-Type","")
                    cd = r.headers.get("Content-Disposition","")
                    filename = f"Proposal_{client_company_name or 'proposal'}.docx"
                    if cd and "filename=" in cd:
                        try:
                            import urllib.parse, re
                            match = re.search(r"filename\*=UTF-8''([^;]+)", cd)
                            if match:
                                filename = urllib.parse.unquote(match.group(1))
                            else:
                                filename = cd.split("filename=")[1].strip().strip('"')
                        except Exception:
                            pass
                    if "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in ct:
                        data = r.content
                        ver = r.headers.get("X-Proposal-Version")
                        with generation_status.container():
                            st.success(" **Document Generated Successfully!**")
                            st.download_button("⬇️ Download DOCX", data=data, file_name=filename, mime=ct, use_container_width=True)
                            if ver:
                                st.info(f"Saved proposal version id: **{ver}**")
                    elif "application/json" in ct:
                        generation_status.json(r.json())
                    else:
                        generation_status.write("Received unexpected content type:", ct)
                else:
                    try:
                        err = r.json()
                        generation_status.error(f"❌ Server responded with error ({r.status_code}): {err.get('detail','')}")
                    except Exception:
                        generation_status.error(f"❌ Server error {r.status_code}: {r.text}")
            except requests.RequestException as re:
                generation_status.error(f"❌ Request failed: {re}")

# Regenerate by version id
st.markdown("---")
st.subheader("Regenerate by version id (from Database)")
regen_cols = st.columns([1, 4])
with regen_cols[0]:
    ver_input = st.text_input("Version ID", key="regen_version_id_input")
with regen_cols[1]:
    st.markdown("<br>", unsafe_allow_html=True)
    btn_regenerate = st.button("Regenerate DOCX from DB", key="btn_regenerate")

if btn_regenerate:
    if not ver_input.strip():
        st.error("Provide a version_id integer")
    else:
        try:
            vid = int(ver_input.strip())
        except Exception:
            st.error("version_id must be integer")
            vid = None
        if vid:
            regen_status = st.empty()
            regen_status.info(f"Regenerating version **{vid}**...")
            try:
                with st.spinner("Regenerating..."):
                    r = requests.post(regenerate_url, json={"version_id": vid}, timeout=timeout_sec, stream=True)
                if r.status_code == 200:
                    ct = r.headers.get("Content-Type","")
                    if "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in ct:
                        data = r.content
                        regen_status.success("Regenerated document ready.")
                        st.download_button("Download regenerated DOCX", data=data, file_name=f"proposal_regen_{vid}.docx", mime=ct)
                    else:
                        regen_status.write("Unexpected response:", r.text)
                else:
                    try:
                        err = r.json()
                        regen_status.error(f"Error {r.status_code}: {err.get('detail', 'Unknown')}")
                    except Exception:
                        regen_status.error(f"Error {r.status_code}: {r.text}")
            except Exception as e:
                regen_status.error(f"Regenerate failed: {e}")

st.markdown("---")
st.markdown("## Notes")
st.markdown("""
- **Backend requirement:** Ensure your FastAPI backend is running at the configured URL (`http://localhost:8000` by default).
- **Key mapping:** This UI sends `client_company_name` and `provider_company_name` to match the DOCX template placeholders.
- **Deliverables / Phases:** Deliverables are sent as `{"title","description","acceptance_criteria"}` and phases as `{"phase_name","duration_weeks","tasks"}`.
- **Suggestions:** Use "Get LLM suggestions" to receive suggested deliverables/phases; add selected suggestions into manual lists before generating.
""")
