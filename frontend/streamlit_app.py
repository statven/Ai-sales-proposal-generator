"""
Streamlit UI for AI Sales Proposal Generator
- Place in repository root and run: streamlit run streamlit_app.py
- Requires: streamlit, requests
"""
import os
import json
import requests
import streamlit as st
from datetime import date, datetime
from typing import List, Dict, Any
import locale

# ---------------- Configuration ----------------
API_BASE_DEFAULT = os.getenv("PROPOSAL_API_BASE", "http://localhost:8000")
GENERATE_SUFFIX = "/api/v1/generate-proposal"
SUGGEST_SUFFIX = "/api/v1/suggest"  # Эндпоинт для предложений
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
MIN_PHASE_WEEKS = 1
MAX_PHASE_WEEKS = 52

# ---------------- Helpers ----------------
def _format_currency(value) -> str:
    """Formats a number as currency using Russian locale style (Space thousands separator, comma decimal)."""
    if value is None:
        return ""
    try:
        # Устанавливаем русскую локаль для форматирования
        try:
            locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8') 
        except locale.Error:
            try:
                locale.setlocale(locale.LC_ALL, 'Russian_Russia') # Для некоторых Windows систем
            except locale.Error:
                pass # Используем дефолтное форматирование если локаль не установилась
                
        amount = float(value)
        # Форматирует с разделителем тысяч (пробел) и десятичным разделителем (запятая)
        # locale.currency возвращает символ валюты, locale.format_string только число
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
    """Return list of error dicts {'loc':..., 'msg':...}"""
    errors = []
    if not payload.get("client_company_name") or len(payload["client_company_name"].strip()) < MIN_CLIENT_NAME:
        errors.append({"loc":["client_company_name"], "msg": f"client_company_name must be at least {MIN_CLIENT_NAME} characters"})
    if not payload.get("provider_company_name") or len(payload["provider_company_name"].strip()) < MIN_PROVIDER_NAME:
        errors.append({"loc":["provider_company_name"], "msg": f"provider_company_name must be at least {MIN_PROVIDER_NAME} characters"})
    # deadline not in past
    dl = payload.get("deadline")
    if dl:
        try:
            if isinstance(dl, str):
                d = date.fromisoformat(dl)
            else:
                d = dl
            if d < datetime.utcnow().date():
                errors.append({"loc":["deadline"], "msg":"deadline must not be in the past"})
        except Exception:
            errors.append({"loc":["deadline"], "msg":"deadline invalid ISO date"})
    # financials
    fin = payload.get("financials") or {}
    for k in ("development_cost", "licenses_cost", "support_cost"):
        v = fin.get(k)
        if v is not None:
            try:
                fv = float(v)
                if fv < 0:
                    errors.append({"loc":[k], "msg":"must be >= 0"})
            except Exception:
                errors.append({"loc":[k], "msg":"must be numeric"})
    # deliverables
    for i, d in enumerate(payload.get("deliverables", []) or []):
        if not isinstance(d, dict):
            errors.append({"loc":["deliverables", i], "msg":"must be object"})
            continue
        if len((d.get("title") or "").strip()) < MIN_DELV_TITLE:
            errors.append({"loc":["deliverables", i, "title"], "msg": f"title must be at least {MIN_DELV_TITLE} chars"})
        if len((d.get("description") or "").strip()) < MIN_DELV_DESC:
            errors.append({"loc":["deliverables", i, "description"], "msg": f"description must be at least {MIN_DELV_DESC} chars"})
        if len((d.get("acceptance_criteria") or "").strip()) < MIN_DELV_ACC:
            errors.append({"loc":["deliverables", i, "acceptance_criteria"], "msg": f"acceptance_criteria must be at least {MIN_DELV_ACC} chars"})
    # phases
    for i, p in enumerate(payload.get("phases", []) or []):
        if not isinstance(p, dict):
             errors.append({"loc":["phases", i], "msg":"must be object"})
             continue
        try:
            w = int(p.get("duration_weeks"))
            if w < MIN_PHASE_WEEKS or w > MAX_PHASE_WEEKS:
                errors.append({"loc":["phases", i, "duration_weeks"], "msg": f"duration_weeks must be between {MIN_PHASE_WEEKS} and {MAX_PHASE_WEEKS}"})
        except Exception:
            errors.append({"loc":["phases", i, "duration_weeks"], "msg":"must be integer"})
        if len((p.get("tasks") or "").strip()) < MIN_PHASE_TASKS:
            errors.append({"loc":["phases", i, "tasks"], "msg": f"tasks must be at least {MIN_PHASE_TASKS} chars"})
    return errors

# --- Callback for Add Selected Button ---
def add_selected_suggestions(list_type: str):
    """Callback to add all selected items of a given type to the main list and reset their checkboxes."""
    
    selected_count = 0
    # 1. Determine list type and keys
    if list_type == 'deliverables':
        suggestions = st.session_state.get("suggestions_data", {}).get("suggested_deliverables", [])
        target_state = "deliverables_state"
        prefix = "sdeliv_pick_"
    else: # 'phases'
        suggestions = st.session_state.get("suggestions_data", {}).get("suggested_phases", [])
        target_state = "phases_state"
        prefix = "sphase_pick_"

    if not suggestions:
        return

    # 2. Iterate through suggestions and add selected ones
    for i, item in enumerate(suggestions):
        checkbox_key = f"{prefix}{i}"
        
        # Check if the checkbox was selected AND if the key exists in session_state
        if st.session_state.get(checkbox_key):
            
            # Add to target list
            if list_type == 'deliverables':
                # Handle potential key differences between LLM output
                acceptance_text = item.get('acceptance', item.get('acceptance_criteria', '')) 
                st.session_state.setdefault(target_state, []).append({
                    "title": item.get("title",""),
                    "description": item.get("description",""),
                    "acceptance_criteria": acceptance_text
                })
            else: # phases
                try:
                    duration_weeks = int(item.get("duration_weeks", 4))
                except (ValueError, TypeError):
                    duration_weeks = 4
                st.session_state.setdefault(target_state, []).append({
                    "duration_weeks": duration_weeks, 
                    "tasks": item.get("tasks","")
                })
                
            # 3. Reset the checkbox state
            st.session_state[checkbox_key] = False
            selected_count += 1
            
    # Trigger rerun to update the UI (lists and reset checkboxes) if something was added
    if selected_count > 0:
        st.rerun()


# ---------------- UI ----------------
st.set_page_config(page_title="AI Sales Proposal Generator", layout="wide")
st.title("AI Sales Proposal Generator ")

# --- Sidebar Configuration ---
with st.sidebar:
    st.header("Backend / Settings")
    api_base = st.text_input("API base URL", API_BASE_DEFAULT)
    timeout_sec = st.number_input("Request timeout (s)", min_value=5, max_value=300, value=60, step=5)
    st.markdown("**Tips:** Set API base to your FastAPI host, e.g. http://localhost:8000")

generate_url, regenerate_url, suggest_url = build_api_urls(api_base)

# --- Proposal brief ---
st.header("1. Proposal Brief & Financials")
col_left, col_right = st.columns([2, 1])

with col_left:
    client_company_name = st.text_input("Client name", value="ООО Инновационные Решения", key="client_company_name")
    provider_company_name = st.text_input("Provider name", value="Digital Forge Group", key="provider_company_name")
    project_goal = st.text_input("Project goal (short)", value="Integrate CRM and migrate e-commerce platforms", key="project_goal")
    scope = st.text_area("Scope (detailed)", height=150, value="Migrate catalog, sync customers, create REST API for data sync.", key="scope")
    technologies = st.text_input("Technologies (comma-separated)", value="Python, FastAPI, Shopify", key="technologies")
    tone = st.selectbox("Tone", options=["Formal", "Marketing", "Technical", "Friendly"], index=0, key="tone")
    
with col_right:
    st.subheader("Dates & Financials (USD)")
    deadline = st.date_input("Expected completion date", value=date.today(), key="deadline")
    
    # Financial Inputs
    development_cost = st.number_input("Development cost", min_value=0.0, value=45000.0, step=100.0, format="%.2f", key="development_cost")
    licenses_cost = st.number_input("Licenses cost", min_value=0.0, value=5000.0, step=50.0, format="%.2f", key="licenses_cost")
    support_cost = st.number_input("Support & maintenance", min_value=0.0, value=2500.0, step=50.0, format="%.2f", key="support_cost")

    # === ИСПРАВЛЕНИЕ: Расчет общей суммы в UI ===
    total_cost = (development_cost or 0.0) + (licenses_cost or 0.0) + (support_cost or 0.0)
    st.markdown("---")
    st.markdown(f"**Total Estimated Investment:**")
    st.markdown(f"### ${_format_currency(total_cost)}")
    # ============================================

# --- Build payload helper (used by both Generate and Suggest) ---
def build_payload(include_manual_deliverables=True, include_manual_phases=True):
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
    if include_manual_deliverables:
        payload["deliverables"] = st.session_state.get("deliverables_state", [])
    if include_manual_phases:
        payload["phases"] = st.session_state.get("phases_state", [])
    return payload

# --- 2. Action Buttons and Feedback ---
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

# Dedicated container for generation status
generation_status = st.empty()


# ---  Deliverables & Phases Manual Editors ---
st.markdown("---")
st.header("2. Deliverables & Phases (Manual Input)")
edit_cols = st.columns(2)

# --- Deliverables editor ---
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

# --- Phases editor ---
with edit_cols[1]:
    st.subheader("Phases / Timeline")
    if "phases_state" not in st.session_state:
        st.session_state["phases_state"] = []

    def add_empty_phase():
        st.session_state["phases_state"].append({"duration_weeks":4, "tasks":""})

    st.button("Add new phase", on_click=add_empty_phase, key="add_phase_btn")
    
    for idx, p in enumerate(st.session_state["phases_state"]):
        with st.expander(f"Phase #{idx+1}: {p.get('tasks','(Click to edit)')[0:30]}...", expanded=False):
            weeks = st.number_input(f"Duration weeks #{idx+1}", value=p.get("duration_weeks",4), min_value=MIN_PHASE_WEEKS, max_value=MAX_PHASE_WEEKS, key=f"phase_weeks_{idx}")
            tasks = st.text_area(f"Tasks #{idx+1}", value=p.get("tasks",""), key=f"phase_tasks_{idx}", max_chars=MAX_PHASE_TASKS)
            st.session_state["phases_state"][idx] = {"duration_weeks":int(weeks), "tasks":tasks}
        if st.button(f"Remove Phase #{idx+1}", key=f"phase_remove_{idx}"):
            st.session_state["phases_state"].pop(idx)
            st.rerun()


# --- 4. Suggestion Retrieval & Display Logic ---

if btn_suggest:
    st.session_state.pop("suggestions_data", None) # Clear old suggestions
    
    payload = build_payload(include_manual_deliverables=False, include_manual_phases=False)
    
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
                ct = r.headers.get("Content-Type","")
                if "application/json" in ct or r.text.strip().startswith("{"):
                    data = r.json()
                    st.session_state["suggestions_data"] = data 
                    generation_status.success("Suggestions received. Select items below to add them to your proposal.")
                    # Force rerun to display suggestions correctly
                    st.rerun() 
                else:
                    generation_status.warning("Server returned non-JSON response; check backend logs.")
            else:
                try:
                    err = r.json()
                    generation_status.error(f"Server error ({r.status_code}): {err}")
                except Exception:
                    generation_status.error(f"Server returned status {r.status_code}: {r.text}")
        except requests.RequestException as re:
            generation_status.error(f" **Request failed**: {re}. Check that the backend is running at {suggest_url}")
        except Exception as e:
            generation_status.error(f"Unexpected error: {e}")

if st.session_state.get("suggestions_data"):
    st.markdown("---")
    st.header("3. LLM Suggestions (Preview)")
    data = st.session_state["suggestions_data"]
    
    s_delivs = data.get("suggested_deliverables") or []
    s_phases = data.get("suggested_phases") or []
    
    sugg_cols = st.columns(2)

    # ------------------ DELIVERABLES DISPLAY ------------------
    with sugg_cols[0]:
        if s_delivs:
            st.markdown("### Suggested Deliverables")
            st.caption("Check the box(es) to select items, then click 'Add Selected'.")
            
            st.button(
                "➕ **Add Selected Deliverables**", 
                key="add_selected_delivs_btn_final", 
                on_click=add_selected_suggestions, 
                args=('deliverables',),
                type="secondary",
                use_container_width=True
            )
            
            for i,d in enumerate(s_delivs):
                if isinstance(d, dict):
                    try:
                        acceptance_text = d.get('acceptance', d.get('acceptance_criteria', '')) 
                        checkbox_key = f"sdeliv_pick_{i}" 
                        
                        st.session_state.setdefault(checkbox_key, False)

                        with st.container(border=True):
                            c1, c2 = st.columns([0.1, 4])
                            c1.checkbox("", key=checkbox_key) 
                            c2.markdown(f"**{d.get('title','(No Title)')}**")
                            st.caption(d.get('description',''))
                            st.text(f"Acceptance: {acceptance_text}")
                            
                    except Exception as e:
                        st.error(f"Error parsing suggested deliverable #{i+1}: {e}")
                        
    # ------------------ PHASES DISPLAY ------------------
    with sugg_cols[1]:
        if s_phases:
            st.markdown("### Suggested Phases")
            st.caption("Check the box(es) to select items, then click 'Add Selected'.")
            
            st.button(
                "➕ **Add Selected Phases**", 
                key="add_selected_phases_btn_final", 
                on_click=add_selected_suggestions, 
                args=('phases',),
                type="secondary",
                use_container_width=True
            )
            
            for i,p in enumerate(s_phases):
                if isinstance(p, dict):
                    try:
                        checkbox_key = f"sphase_pick_{i}" 
                        st.session_state.setdefault(checkbox_key, False)
                        
                        duration_weeks = int(p.get("duration_weeks", 4))
                        
                        with st.container(border=True):
                            c1, c2 = st.columns([0.1, 4])
                            c1.checkbox("", key=checkbox_key)
                            c2.markdown(f"**{p.get('phase_name','Phase')}** — **{duration_weeks} weeks**")
                            st.caption(p.get('tasks',''))

                    except Exception as e:
                        st.error(f"Error parsing suggested phase #{i+1}: {e}")

# --- Generate final DOCX execution ---
if btn_generate:
    payload = build_payload(include_manual_deliverables=True, include_manual_phases=True)
    val_errs = validate_payload(payload)
    
    if val_errs:
        generation_status.error(" **Fix validation errors** before generating:")
        for e in val_errs:
            st.write(f"- **{'/'.join(map(str, e['loc']))}**: {e['msg']}")
    else:
        generation_status.info(" **Generating DOCX** — please wait (this may take 10-60s depending on model and backend)")
        try:
            with st.spinner("Calling backend to generate DOCX..."):
                # Pass financial data in the payload for backend calculation
                r = requests.post(generate_url, json=payload, timeout=timeout_sec, stream=True)
                
            if r.status_code == 200:
                ct = r.headers.get("Content-Type","")
                cd = r.headers.get("Content-Disposition","")
                filename = f"Proposal_{client_company_name or 'proposal'}.docx"
                
                if "filename=" in cd:
                    try:
                        import urllib.parse
                        import re
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
                        st.download_button("⬇️ **Download DOCX**", data=data, file_name=filename, mime=ct, type="primary", use_container_width=True)
                        if ver:
                            st.info(f"Saved proposal version id: **{ver}**")
                            
                elif "application/json" in ct:
                    generation_status.json(r.json())
                else:
                    generation_status.write("Received unexpected content type:", ct)
            else:
                try:
                    err = r.json()
                    generation_status.error(f"❌ **Server responded with an error ({r.status_code}):** {err.get('detail', 'Unknown error')}")
                except Exception:
                    generation_status.error(f"❌ **Server error {r.status_code}**: {r.text}")
        except requests.RequestException as re:
            generation_status.error(f"❌ **Request failed**: {re}")
        except Exception as e:
            generation_status.error(f"❌ Unexpected error: {e}")

# Regenerate endpoint (by version_id)
st.markdown("---")
st.subheader("Regenerate by version id (from Database)")
regen_cols = st.columns([1, 4])
with regen_cols[0]:
    ver_input = st.text_input("Version ID", key="regen_version_id_input")
with regen_cols[1]:
    st.markdown("<br>", unsafe_allow_html=True) # Spacer
    btn_regenerate = st.button("Regenerate DOCX from DB", use_container_width=False, key="btn_regenerate")

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
                        regen_status.error(f"Error {r.status_code}: {err.get('detail', 'Unknown error')}")
                    except Exception:
                        regen_status.error(f"Error {r.status_code}: {r.text}")
            except Exception as e:
                regen_status.error(f"Regenerate failed: {e}")

st.markdown("---")
st.markdown("## Notes")
st.markdown("""
- **Backend requirement:** Ensure your FastAPI backend is running at the configured URL (`http://localhost:8000` by default).
- **Total Cost:** The total estimated investment is calculated live in the UI from the three input fields (Development, Licenses, Support).
- **Suggestions:** Use **" Get LLM suggestions"** to request content from the LLM. You can select items and click **"➕ Add Selected..."** to merge them into the manual lists above.
- **Generation:** **" Generate final DOCX"** calls the main endpoint, which handles AI content generation (if needed), document merging, and returns the file.
""")