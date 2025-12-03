# backend/app/services/visualization_service.py
"""
Visualization service — focused on robust Gantt (agent-mode) and helpers.

This file replaces/updates the previous Gantt-related logic:
- agent_enrich_schedule(...)  -> produce consistent milestone dictionaries
- generate_gantt_image(...)  -> professional Gantt chart PNG (Plotly)
"""
import io
from io import BytesIO
import json
import math
import logging
import datetime
import re
from typing import Dict, Any, List, Optional
from PIL import Image, ImageDraw, ImageFont
import graphviz

import pandas as pd
import plotly.express as px
import plotly.io as pio
from backend.app.services.openai_service import _call_openai_new_client
# в visualization_service.py

from backend.app.services.openai_service import _generate_lifecycle_stages_with_agent, _generate_uml_with_agent

logger = logging.getLogger("uvicorn.error")

# ------------------ Helpers ------------------

def _placeholder_png_bytes(text: str = "UML unavailable", width: int = 1000, height: int = 400) -> bytes:
    """
    Генерирует заглушку PNG с текстом в центре. 
    ИСПРАВЛЕНО: использует draw.textbbox() вместо draw.textsize().
    """
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except IOError:
        font = ImageFont.load_default()
    
    # 1. Используем textbbox для получения размера текста
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]  # Ширина и высота текста
    except Exception as e:
        # Fallback values
        logger.error("Error calculating text size in doc_engine: %s", str(e))
        w, h = 300, 25 
    
    # 2. Вычисляем позицию для центрирования
    x = (width - w) / 2
    y = (height - h) / 2
    
    # 3. Рисуем текст
    draw.text((x, y), text, fill=(50, 50, 50), font=font)
    
    # 4. Сохраняем в буфер
    buf = BytesIO()
    img.save(buf, format="PNG")
    
    return buf.getvalue()


def agent_enrich_schedule(proposal: Dict[str, Any],
                          default_week_duration: int = 2,
                          hours_per_week: int = 40,
                          min_weeks: int = 1,
                          max_weeks: int = 52) -> List[Dict[str, Any]]:
    """
    Agent schedule enricher — owner-aware, effort (man-hours) -> calendar conversion,
    iterative reflow respecting dependencies and owner availability.

    Key behavior:
      - Treats phase's 'effort_hours' / 'duration_hours' as man-hours (canonical).
      - Converts to calendar days using owner capacity heuristic (8h/day per person).
      - Uses owner availability (resource leveling heuristic): phases assigned to an owner
        start when that owner is free (not simply global cursor).
      - Respects dependencies (depends_on).
      - Iteratively reflows schedule to satisfy dependencies + owner availability.
    """
    try:
        import re, math, datetime
        from collections import defaultdict

        # Local helper fallbacks (prefer global if available)
        def _ensure_list(x):
            if x is None:
                return []
            if isinstance(x, (list, tuple)):
                return list(x)
            return [x]

        def _to_datetime(val):
            # prefer existing global if available
            try:
                # if global helper exists and works, use it
                if "_to_datetime" in globals() and callable(globals()["_to_datetime"]):
                    return globals()["_to_datetime"](val)
            except Exception:
                pass
            if val is None:
                return None
            if isinstance(val, datetime.datetime):
                return val
            if isinstance(val, datetime.date):
                return datetime.datetime.combine(val, datetime.time.min)
            s = str(val).strip()
            if not s:
                return None
            # try ISO
            try:
                return datetime.datetime.fromisoformat(s)
            except Exception:
                pass
            # try dateutil if present
            try:
                from dateutil import parser as _dp
                return _dp.parse(s)
            except Exception:
                pass
            # try common formats
            fmts = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"]
            for f in fmts:
                try:
                    return datetime.datetime.strptime(s, f)
                except Exception:
                    pass
            return None

        def _sanitize_label(txt: Any) -> str:
            if txt is None:
                return ""
            s = str(txt)
            s = re.sub(r"[\r\n\t]+", " ", s)
            s = re.sub(r"\s{2,}", " ", s)
            s = s.strip(" '\"")
            return s.strip()

        # Optional: reuse your guess_owner if it exists globally
        if "guess_owner" in globals() and callable(globals()["guess_owner"]):
            guess_owner = globals()["guess_owner"]
        else:
            def guess_owner(tasks_text: Any) -> str:
                t = _sanitize_label(tasks_text).lower()
                if not t:
                    return "Engineering"
                for kw in ("prompt", "prompting", "data", "model", "qa", "deploy", "devops", "design", "project"):
                    if kw in t:
                        if kw in ("qa", "test", "validation", "acceptance"):
                            return "QA Engineer"
                        if kw in ("deploy", "deployment", "release", "orchestration"):
                            return "DevOps"
                        if kw in ("project", "planning", "management", "meeting"):
                            return "Project Manager"
                        if kw in ("prompt", "model", "prompting"):
                            return "AI Developer"
                        return "Engineering"
                return "Engineering"

        # source phases
        raw = proposal.get("milestones") or proposal.get("phases_list") or []
        if (not raw) and isinstance(proposal.get("suggested_phases"), list):
            raw = proposal.get("suggested_phases")
        items = _ensure_list(raw)

        # fallback defaults
        if not items:
            logger.info("agent_enrich_schedule: no input phases — using defaults")
            items = [
                {"phase_name": "Setup & Data Modeling", "duration_hours": default_week_duration * hours_per_week,
                 "tasks": "Environment setup, data inventory, schema design"},
                {"phase_name": "Integration & Testing", "duration_hours": default_week_duration * 2 * hours_per_week,
                 "tasks": "Integrate model, prompts, API tests, QA"}
            ]

        # today/base
        today_dt = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        base_iso = proposal.get("proposal_date") or proposal.get("proposal_date_iso") or proposal.get("proposal_start") or None
        base_dt = _to_datetime(base_iso) or today_dt

        # team_size determination
        try:
            team_size = int(proposal.get("team_size") or 1)
            team_size = max(1, team_size)
        except Exception:
            team_size = 1

        # Build members list from proposal if provided
        members = []
        if isinstance(proposal.get("team_members"), list):
            for m in proposal.get("team_members"):
                if isinstance(m, dict):
                    members.append({"name": (m.get("name") or "").strip(), "role": (m.get("role") or "").strip()})
                else:
                    members.append({"name": str(m).strip(), "role": ""})
        elif isinstance(proposal.get("team_structure_text"), str) and proposal.get("team_structure_text").strip():
            # try global helper _extract_real_roles
            if "_extract_real_roles" in globals() and callable(globals()["_extract_real_roles"]):
                roles = globals()["_extract_real_roles"](proposal.get("team_structure_text"))
                if roles:
                    members = [{"name": r, "role": r} for r in roles]
        # placeholders if none
        if not members:
            for i in range(team_size):
                members.append({"name": f"TBD #{i+1}", "role": "TBD"})

        name_index = { (m["name"] or "").strip().lower(): m for m in members if m.get("name") }
        role_index = {}
        for m in members:
            role = (m.get("role") or "").strip().lower()
            if role:
                for tok in re.split(r"[\/,;|&\s\-]+", role):
                    if tok:
                        role_index.setdefault(tok, []).append(m)

        # owner capacity per day heuristic
        def _owner_capacity_hours_per_day(owner_name: str) -> float:
            if not owner_name:
                return float(team_size * 8)
            low = owner_name.strip().lower()
            if low in name_index:
                return 8.0
            # if role token matches several members -> capacity = count * 8
            matched = 0
            for tok in re.split(r"[\/,;|&\s\-]+", low):
                if tok in role_index:
                    matched += len(role_index[tok])
            if matched > 0:
                return float(matched * 8)
            if any(k in low for k in ("engineer", "dev", "team", "engineering", "backend", "frontend")):
                return float(team_size * 8)
            return 8.0

        enriched = []
        used_names = {}
        owner_availability = defaultdict(lambda: base_dt)

        # PASS 1: greedy schedule using dependencies seen so far + owner availability
        for i, it in enumerate(items):
            if isinstance(it, str):
                it = {"phase_name": it}

            raw_name = it.get("phase_name") or it.get("name") or it.get("title") or f"Phase {i+1}"
            name = _sanitize_label(raw_name) or f"Phase {i+1}"
            cnt = used_names.get(name, 0)
            if cnt:
                name = f"{name} ({cnt+1})"
            used_names[name] = cnt + 1

            # canonical effort (man-hours)
            man_hours = None
            for k in ("effort_hours", "duration_hours", "hours"):
                if it.get(k) is not None:
                    try:
                        man_hours = float(it.get(k))
                        break
                    except Exception:
                        pass
            if man_hours is None:
                # fallback to weeks/days
                if it.get("duration_weeks") is not None:
                    try:
                        man_hours = float(it.get("duration_weeks")) * float(hours_per_week)
                    except Exception:
                        man_hours = float(default_week_duration * hours_per_week)
                elif it.get("duration_days") is not None:
                    try:
                        man_hours = float(it.get("duration_days")) * 8.0
                    except Exception:
                        man_hours = float(default_week_duration * hours_per_week)
                else:
                    man_hours = float(default_week_duration * hours_per_week)

            # percent complete (optional)
            try:
                pct = float(it.get("percent_complete") or it.get("percent") or 0.0)
            except Exception:
                pct = 0.0

            # owner detection
            owner_raw = None
            for ok in ["owner", "owner_name", "assigned_to", "resource", "responsible", "ownerName"]:
                if isinstance(it, dict) and it.get(ok):
                    owner_raw = it.get(ok)
                    break
            if isinstance(owner_raw, dict):
                owner_cand = owner_raw.get("name") or owner_raw.get("title") or next(iter(owner_raw.values()), None)
            else:
                owner_cand = owner_raw
            owner_cand = _sanitize_label(owner_cand) if owner_cand is not None else ""
            if not owner_cand:
                owner_cand = guess_owner(it.get("tasks") or it.get("description") or it.get("notes") or it.get("title") or it.get("phase_name") or "")
            owner = owner_cand or "Engineering"

            # dependencies
            depends_raw = it.get("depends_on") or it.get("after") or it.get("depends") or it.get("predecessors") or []
            if isinstance(depends_raw, str):
                depends = [d.strip() for d in re.split(r"[;,/]|->", depends_raw) if d.strip()]
            else:
                depends = _ensure_list(depends_raw)

            # earliest by resolved predecessors (only those already in enriched)
            earliest = base_dt
            for d in depends:
                ds = _sanitize_label(d)
                if not ds:
                    continue
                matched = None
                for prev in enriched:
                    if ds == prev["name"] or ds.lower() in prev["name"].lower() or prev["name"].lower() in ds.lower():
                        matched = prev
                        break
                if matched:
                    try:
                        prev_end = _to_datetime(matched.get("end"))
                        if prev_end:
                            cand = prev_end + datetime.timedelta(days=1)
                            if cand > earliest:
                                earliest = cand
                    except Exception:
                        pass

            # owner availability
            owner_free = owner_availability.get(owner, base_dt)
            owner_day_capacity = _owner_capacity_hours_per_day(owner)
            # convert to calendar days
            days_needed = int(math.ceil(max(0.0, man_hours) / max(1e-6, owner_day_capacity)))
            days_needed = max(1, days_needed)

            # start = max(earliest, owner_free, explicit start if present)
            explicit_start = _to_datetime(it.get("start") or it.get("start_date"))
            start_dt = explicit_start if explicit_start is not None else max(earliest, owner_free)
            explicit_end = _to_datetime(it.get("end") or it.get("end_date"))
            if explicit_end is not None and explicit_start is not None:
                # if both provided, ensure they fit effort; else adjust end to cover effort
                if (explicit_end - explicit_start).days < days_needed:
                    end_dt = explicit_start + datetime.timedelta(days=days_needed)
                else:
                    end_dt = explicit_end
                start_dt = explicit_start
            else:
                end_dt = start_dt + datetime.timedelta(days=days_needed)

            # dynamic percent_complete if not provided
            if pct <= 0:
                if today_dt >= end_dt:
                    pct = 100.0
                elif today_dt > start_dt and end_dt > start_dt:
                    days_passed = (today_dt - start_dt).days
                    total_days = max(1, (end_dt - start_dt).days)
                    pct = (days_passed / total_days) * 100.0
                else:
                    pct = 0.0
            pct = max(0.0, min(100.0, pct))

            # update owner availability
            owner_availability[owner] = end_dt

            enriched.append({
                "name": name,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "duration_days": int((end_dt - start_dt).days),
                "duration_weeks": int(max(1, round((end_dt - start_dt).days / 7.0))),
                "percent_complete": float(pct),
                "owner": str(owner),
                "effort_hours": float(man_hours),
                "depends_on": [str(d) for d in depends]
            })

        # POST-PROCESS: fuzzy resolve depends_on on final names
        names = [m["name"] for m in enriched]
        lc_map = {n.lower(): n for n in names}
        for m in enriched:
            deps = _ensure_list(m.get("depends_on") or [])
            resolved = []
            for d in deps:
                ds = _sanitize_label(d)
                if not ds:
                    continue
                if ds in names:
                    resolved.append(ds); continue
                low = ds.lower()
                if low in lc_map:
                    resolved.append(lc_map[low]); continue
                matched = None
                for n in names:
                    if low in n.lower() or n.lower() in low:
                        matched = n; break
                if matched:
                    resolved.append(matched)
            if not resolved:
                idx = names.index(m["name"])
                if idx > 0:
                    resolved = [names[idx - 1]]
            m["depends_on"] = list(dict.fromkeys(resolved))

        # SECOND PASS: iterative relaxation to respect deps & owner availability
        name_to_idx = {m["name"]: idx for idx, m in enumerate(enriched)}
        for m in enriched:
            m["_start_dt"] = _to_datetime(m.get("start"))
            m["_end_dt"] = _to_datetime(m.get("end"))

        changed = True
        max_iters = max(3, len(enriched) + 3)
        for _ in range(max_iters):
            if not changed:
                break
            changed = False
            ov = defaultdict(lambda: base_dt)
            for m in enriched:
                cur_start = m.get("_start_dt") or base_dt
                cur_end = m.get("_end_dt") or (cur_start + datetime.timedelta(days=max(1, int(m.get("duration_days") or 1))))
                earliest = base_dt
                for d in _ensure_list(m.get("depends_on") or []):
                    if d in name_to_idx:
                        dep = enriched[name_to_idx[d]]
                        dep_end = dep.get("_end_dt")
                        if dep_end:
                            cand = dep_end + datetime.timedelta(days=1)
                            if cand > earliest:
                                earliest = cand
                owner = m.get("owner") or ""
                owner_free = ov.get(owner, base_dt)
                new_start = max(earliest, owner_free, cur_start)
                new_end = new_start + datetime.timedelta(days=max(1, int(m.get("duration_days") or 1)))
                if new_start != cur_start or new_end != cur_end:
                    m["_start_dt"] = new_start
                    m["_end_dt"] = new_end
                    changed = True
                ov[owner] = m["_end_dt"]

        # finalize and normalize fields
        for m in enriched:
            st = m.pop("_start_dt", None)
            et = m.pop("_end_dt", None)
            if st and et:
                m["start"] = st.isoformat()
                m["end"] = et.isoformat()
                m["duration_days"] = int(max(1, (et - st).days))
            try:
                m["duration_weeks"] = int(m.get("duration_weeks") or max(1, int(round((m.get("duration_days") or 7) / 7.0))))
            except Exception:
                m["duration_weeks"] = 1
            try:
                m["effort_hours"] = float(m.get("effort_hours") or (m["duration_days"] * 8.0))
            except Exception:
                m["effort_hours"] = float(40.0)
            try:
                m["percent_complete"] = float(m.get("percent_complete") or 0.0)
            except Exception:
                m["percent_complete"] = 0.0

        logger.info("agent_enrich_schedule: produced %d enriched phases (owner-aware)", len(enriched))
        return enriched

    except Exception as e:
        logger.exception("agent_enrich_schedule failed: %s", e)
        today = datetime.date.today()
        start_dt = datetime.datetime.combine(today, datetime.time.min)
        return [{
            "name": "Setup & Data Modeling",
            "start": start_dt.isoformat(),
            "end": (start_dt + datetime.timedelta(days=14)).isoformat(),
            "duration_days": 14,
            "duration_weeks": 2,
            "percent_complete": 0.0,
            "owner": "Engineering",
            "effort_hours": float(80),
            "depends_on": []
        }]


def generate_gantt_image(proposal: Dict[str, Any], width: int = 1200, agent_mode: bool = True) -> bytes:
    """
    Professional agent-mode Gantt chart generator.

    Guarantees:
      - Uses agent_enrich_schedule output when agent_mode True.
      - Extracts team members early and respects team_size.
      - Maps owners to members and does lightweight resource leveling on display.
      - Chart range spans from today (or earliest phase) to the deadline (if provided).
      - Shows capacity/overflow summary in footer and a red banner when overflow detected.
    """
    try:
        import json, math, datetime, re
        import pandas as pd
        import plotly.express as px
        import plotly.io as pio

        # local helpers (prefer global versions if available)
        def _ensure_list(x):
            if x is None:
                return []
            if isinstance(x, (list, tuple)):
                return list(x)
            return [x]

        def _to_datetime(val):
            try:
                if "_to_datetime" in globals() and callable(globals()["_to_datetime"]):
                    return globals()["_to_datetime"](val)
            except Exception:
                pass
            if val is None:
                return None
            if isinstance(val, datetime.datetime):
                return val
            if isinstance(val, datetime.date):
                return datetime.datetime.combine(val, datetime.time.min)
            s = str(val).strip()
            if not s:
                return None
            try:
                return datetime.datetime.fromisoformat(s)
            except Exception:
                pass
            try:
                from dateutil import parser as _dp
                return _dp.parse(s)
            except Exception:
                pass
            fmts = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"]
            for f in fmts:
                try:
                    return datetime.datetime.strptime(s, f)
                except Exception:
                    pass
            return None

        # extract team members function (robust)
        def _extract_team_members_from_proposal(p: Dict[str, Any]) -> List[Dict[str, Any]]:
            keys = ["team_members", "team", "team_structure", "resources", "team_list", "members"]
            raw = None
            for k in keys:
                if k in p and p[k] not in (None, ""):
                    raw = p[k]
                    break
            members = []
            if isinstance(raw, str):
                s = raw.strip()
                try:
                    parsed = json.loads(s)
                    raw = parsed
                except Exception:
                    if "\n" in s:
                        raw = [r.strip() for r in s.splitlines() if r.strip()]
                    elif ";" in s and "," not in s:
                        raw = [r.strip() for r in s.split(";") if r.strip()]
                    elif "," in s:
                        raw = [r.strip() for r in s.split(",") if r.strip()]
                    else:
                        raw = [s]
            if isinstance(raw, dict):
                cand = []
                for k, v in raw.items():
                    if isinstance(v, dict):
                        name = v.get("name") or v.get("full_name") or k
                        role = v.get("role") or v.get("position") or ""
                        hours = v.get("hours")
                    else:
                        name = k
                        role = str(v) if v is not None else ""
                        hours = None
                    try:
                        hours_val = float(hours) if hours not in (None, "") else None
                    except Exception:
                        hours_val = None
                    cand.append({"name": str(name).strip(), "role": str(role).strip(), "hours": hours_val})
                raw = cand
            items = _ensure_list(raw)
            for it in items:
                if it is None:
                    continue
                if isinstance(it, str):
                    s = it.strip()
                    if not s:
                        continue
                    m = re.match(r"^(.+?)[\-\–\—]\s*(.+)$", s)
                    if m:
                        members.append({"name": m.group(1).strip(), "role": m.group(2).strip(), "hours": None})
                        continue
                    m2 = re.match(r"^(.+?)\s*\((.+)\)$", s)
                    if m2:
                        members.append({"name": m2.group(1).strip(), "role": m2.group(2).strip(), "hours": None})
                        continue
                    members.append({"name": s, "role": "", "hours": None})
                    continue
                if isinstance(it, dict):
                    name = it.get("name") or it.get("full_name") or it.get("title") or it.get("username") or ""
                    role = it.get("role") or it.get("position") or it.get("function") or ""
                    hours_val = None
                    if it.get("hours") is not None:
                        try:
                            hours_val = float(it.get("hours"))
                        except Exception:
                            hours_val = None
                    if not name and it.get("person"):
                        p2 = it.get("person")
                        if isinstance(p2, dict):
                            name = p2.get("name") or p2.get("full_name") or ""
                            role = role or p2.get("role") or ""
                    name = str(name).strip()
                    if name:
                        members.append({"name": name, "role": str(role).strip() if role else "", "hours": hours_val})
                    continue
                nm = str(it).strip()
                if nm:
                    members.append({"name": nm, "role": "", "hours": None})
            # unique by lowercase name
            seen = set()
            uniq = []
            for m in members:
                nm = (m.get("name") or "").strip()
                if not nm:
                    continue
                key = nm.lower()
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(m)
            return uniq

        # normalize proposal wrapper
        p = proposal if isinstance(proposal, dict) else {}
        team_text = (
            p.get("team_context")
            or p.get("team_structure_text")
            or p.get("team_structure")
            or p.get("team_roles")
            or (p.get("viz", {}) or {}).get("team_context")
            or (p.get("viz", {}) or {}).get("team_structure")
            or ""
        )

        raw_members = _extract_team_members_from_proposal(p)
        if (not raw_members) and team_text:
            try:
                if "_extract_real_roles" in globals() and callable(globals()["_extract_real_roles"]):
                    real_roles = globals()["_extract_real_roles"](team_text)
                else:
                    # simple heuristic: pick capitalized line starts before ':' as role
                    lines = [l.strip() for l in team_text.splitlines() if l.strip()]
                    real_roles = []
                    for ln in lines:
                        if ":" in ln:
                            cand = ln.split(":")[0].strip()
                            if 1 < len(cand) < 60:
                                real_roles.append(cand)
                    real_roles = real_roles
                if real_roles:
                    raw_members = [{"name": r, "role": r, "hours": None} for r in real_roles]
                    logger.info("generate_gantt_image: constructed members from team_context roles: %s", real_roles)
            except Exception:
                logger.exception("Failed parsing team_context into members")

        explicit_team_size = None
        try:
            explicit_team_size = int(p.get("team_size")) if p.get("team_size") not in (None, "") else None
        except Exception:
            explicit_team_size = None
        if explicit_team_size is None:
            viz = p.get("viz") if isinstance(p.get("viz"), dict) else {}
            try:
                explicit_team_size = int(viz.get("team_size")) if viz.get("team_size") not in (None, "") else None
            except Exception:
                explicit_team_size = None

        structured_team = None
        if isinstance(p.get("team_structure"), list):
            structured_team = p.get("team_structure")
        elif isinstance(p.get("team_structure_text"), str) and p.get("team_structure_text").strip():
            lines = [l.strip() for l in p["team_structure_text"].splitlines() if l.strip()]
            parsed = []
            for ln in lines:
                m = re.match(r"^(?P<role>[^:—\-\(]+)[\-\—:]\s*(?P<name>.+)$", ln)
                if m:
                    parsed.append({"name": m.group("name").strip().strip("()"), "role": m.group("role").strip()})
                else:
                    parsed.append({"name": ln, "role": ""})
            structured_team = parsed if parsed else None

        if explicit_team_size and explicit_team_size > 0:
            team_size = explicit_team_size
        else:
            if raw_members:
                team_size = max(1, len(raw_members))
            elif structured_team:
                team_size = max(1, len(structured_team))
            else:
                team_size = 1

        # normalize members to exactly team_size
        members = list(raw_members)
        adjusted_note = ""
        existing_names_lower = {m["name"].lower() for m in members if m.get("name")}
        if len(members) < team_size:
            needed = team_size - len(members)
            next_idx = 1
            for nm in existing_names_lower:
                m = re.match(r"^tbd\s*#\s*(\d+)$", nm, flags=re.IGNORECASE)
                if m:
                    try:
                        next_idx = max(next_idx, int(m.group(1)) + 1)
                    except Exception:
                        pass
            for j in range(needed):
                candidate = f"TBD #{next_idx}"
                next_idx += 1
                while candidate.lower() in existing_names_lower:
                    candidate = f"TBD #{next_idx}"
                    next_idx += 1
                members.append({"name": candidate, "role": "TBD", "hours": None})
                existing_names_lower.add(candidate.lower())
            adjusted_note = f"Added {needed} placeholder(s) to match team_size={team_size}."
        elif len(members) > team_size:
            extras = members[team_size:]
            members = members[:team_size]
            adjusted_note = f"Trimmed {len(extras)} extra team member(s) to match team_size={team_size} (first {team_size} used)."

        # obtain phases
        if agent_mode:
            try:
                ms = agent_enrich_schedule(p)
            except Exception:
                logger.exception("agent_enrich_schedule failed inside generate_gantt_image; falling back to raw milestones")
                raw = p.get("milestones") or p.get("phases_list") or []
                ms = []
                for i, it in enumerate(_ensure_list(raw)):
                    if isinstance(it, dict):
                        name = it.get("name") or it.get("phase_name") or it.get("title") or f"Phase {i+1}"
                        ms.append({
                            "name": name,
                            "start": it.get("start") if it.get("start") else None,
                            "end": it.get("end") if it.get("end") else None,
                            "duration_days": it.get("duration_days") if it.get("duration_days") else None,
                            "duration_weeks": it.get("duration_weeks") if it.get("duration_weeks") else None,
                            "percent_complete": it.get("percent_complete") if it.get("percent_complete") is not None else (it.get("percent") if it.get("percent") is not None else 0),
                            "owner": it.get("owner") if it.get("owner") else "",
                            "effort_hours": it.get("effort_hours") if it.get("effort_hours") is not None else None,
                            "depends_on": it.get("depends_on") if it.get("depends_on") else []
                        })
                    else:
                        ms.append({
                            "name": str(it),
                            "start": None, "end": None, "duration_days": None, "duration_weeks": None,
                            "percent_complete": 0, "owner": "", "effort_hours": None, "depends_on": []
                        })
        else:
            raw = p.get("milestones") or p.get("phases_list") or []
            ms = []
            for i, it in enumerate(_ensure_list(raw)):
                if isinstance(it, dict):
                    name = it.get("name") or it.get("phase_name") or it.get("title") or f"Phase {i+1}"
                    ms.append({
                        "name": name,
                        "start": it.get("start") if it.get("start") else None,
                        "end": it.get("end") if it.get("end") else None,
                        "duration_days": it.get("duration_days") if it.get("duration_days") else None,
                        "duration_weeks": it.get("duration_weeks") if it.get("duration_weeks") else None,
                        "percent_complete": it.get("percent_complete") if it.get("percent_complete") is not None else (it.get("percent") if it.get("percent") is not None else 0),
                        "owner": it.get("owner") if it.get("owner") else "",
                        "effort_hours": it.get("effort_hours") if it.get("effort_hours") is not None else None,
                        "depends_on": it.get("depends_on") if it.get("depends_on") else []
                    })
                else:
                    ms.append({
                        "name": str(it),
                        "start": None, "end": None, "duration_days": None, "duration_weeks": None,
                        "percent_complete": 0, "owner": "", "effort_hours": None, "depends_on": []
                    })
            if ms:
                ms = agent_enrich_schedule({"milestones": ms})

        # build mapping indices
        name_index = { (m["name"] or "").strip().lower(): (m["name"] or "").strip() for m in members if m.get("name")}
        role_index = {}
        for m in members:
            role = (m.get("role") or "").strip().lower()
            nm = m.get("name")
            if role and nm:
                for tok in re.split(r"[\/,;|&\s\-]+", role):
                    if tok:
                        role_index.setdefault(tok, []).append(nm)
        final_member_names = [m["name"] for m in members]

        # map owners on phases
        mapped_ms = []
        for idx, ph in enumerate(ms):
            owner_raw = (ph.get("owner") or "").strip()
            mapped_owner = None
            if owner_raw:
                low = owner_raw.lower()
                if low in name_index:
                    mapped_owner = name_index[low]
                else:
                    mapped = None
                    for tok in re.split(r"[\/,;|&\s\-]+", low):
                        if tok and tok in role_index and role_index[tok]:
                            mapped = role_index[tok][0]
                            break
                    if mapped:
                        mapped_owner = mapped
                    else:
                        for nm in final_member_names:
                            if low in nm.lower() or nm.lower() in low:
                                mapped_owner = nm
                                break
            if not mapped_owner:
                mapped_owner = final_member_names[idx % len(final_member_names)]
            ph["owner_mapped"] = mapped_owner
            mapped_ms.append(ph)
        ms = mapped_ms

        # convert to rows
        rows = []
        base_for_missing = _to_datetime(p.get("proposal_date")) or datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        for i, m in enumerate(ms):
            start_dt = _to_datetime(m.get("start"))
            end_dt = _to_datetime(m.get("end"))

            # if agent provided effort but no start/end, compute calendar span from effort and owner mapped
            effort = None
            try:
                if m.get("effort_hours") is not None:
                    effort = float(m.get("effort_hours"))
                elif m.get("original_hours") is not None:
                    effort = float(m.get("original_hours"))
                else:
                    effort = float(m.get("duration_weeks") or 2) * 40.0
            except Exception:
                effort = 40.0

            owner_label = m.get("owner_mapped") or final_member_names[i % len(final_member_names)]
            owner_cap = 8.0  # simple display conversion: 8h/day per owner; resource-leveling already in agent schedule

            if start_dt is None and end_dt is None:
                # stagger by earlier start approximations but default to base
                est_days = max(1, int(math.ceil(effort / owner_cap)))
                start_dt = base_for_missing + datetime.timedelta(days=i * 1)
                end_dt = start_dt + datetime.timedelta(days=est_days)
            elif start_dt is None:
                end_dt = end_dt
                est_days = max(1, int(math.ceil(effort / owner_cap)))
                start_dt = end_dt - datetime.timedelta(days=est_days)
            elif end_dt is None:
                est_days = max(1, int(math.ceil(effort / owner_cap)))
                end_dt = start_dt + datetime.timedelta(days=est_days)

            if start_dt >= end_dt:
                end_dt = start_dt + datetime.timedelta(days=max(1, int(m.get("duration_days") or 1)))

            pct = float(m.get("percent_complete") or 0.0)

            rows.append({
                "Task": str(m.get("name") or f"Phase {i+1}"),
                "Start": pd.to_datetime(start_dt),
                "Finish": pd.to_datetime(end_dt),
                "Percent": max(0.0, min(100.0, pct)),
                "Owner": owner_label,
                "Effort": max(0.0, float(effort)),
                "Depends": _ensure_list(m.get("depends_on") or []),
                "CompressionPct": int(m.get("compression_pct")) if m.get("compression_pct") is not None else None,
                "OriginalHours": int(m.get("original_hours")) if m.get("original_hours") is not None else None
            })

        if not rows:
            return _placeholder_png_bytes("No milestones")

        df = pd.DataFrame(rows)

        # ensure swimlanes use final members ordering
        unique_owners = list(dict.fromkeys(final_member_names + [o for o in list(df["Owner"].unique()) if o not in final_member_names]))
        owner_y_map = {owner: i for i, owner in enumerate(unique_owners)}
        df["Y_Val"] = df["Owner"].map(owner_y_map).fillna(len(unique_owners)-1).astype(int)

        # totals and capacity
        total_effort_hours = df["Effort"].sum()
        total_effort_str = f"{int(total_effort_hours):,}".replace(",", " ")
        est_ftes_str = f"{total_effort_hours / 40.0:.1f}"

        # capacity extraction (priority: reality_check -> explicit -> deadline heuristic)
        capacity_hours = None
        rc = None
        if isinstance(p.get("reality_check"), dict):
            rc = p.get("reality_check")
        elif isinstance(p.get("viz"), dict) and isinstance(p["viz"].get("reality_check"), dict):
            rc = p["viz"].get("reality_check")
        if rc and rc.get("team_capacity_hours") is not None:
            try:
                capacity_hours = int(float(rc.get("team_capacity_hours")))
            except Exception:
                capacity_hours = None
        elif p.get("team_capacity_hours") is not None:
            try:
                capacity_hours = int(float(p.get("team_capacity_hours")))
            except Exception:
                capacity_hours = None
        else:
            # compute from deadline & team_size
            deadline_raw = p.get("deadline") or p.get("deadline_date")
            try:
                if deadline_raw:
                    d_obj = _to_datetime(deadline_raw)
                    if isinstance(d_obj, datetime.datetime):
                        d_date = d_obj.date()
                    elif isinstance(d_obj, datetime.date):
                        d_date = d_obj
                    else:
                        d_date = None
                    if d_date:
                        today = datetime.date.today()
                        if d_date > today:
                            days_diff = (d_date - today).days
                            work_days = max(0, int(math.floor(days_diff * (5.0/7.0))))
                            cap = work_days * 8 * int(max(1, team_size))
                            if cap == 0 and work_days > 0:
                                cap = 8 * int(max(1, team_size))
                            capacity_hours = int(cap)
            except Exception:
                capacity_hours = None

        overflow_hours = None
        capacity_note = ""
        if capacity_hours is not None:
            if total_effort_hours > capacity_hours:
                overflow_hours = int(round(total_effort_hours - capacity_hours))
                capacity_note = f"WARNING: capacity exceeded by {overflow_hours}h."
            else:
                capacity_note = "Fits within estimated capacity."

        # plotting
        unique_tasks = list(df["Task"].unique())
        palette = px.colors.qualitative.Plotly
        task_color_map = {task: palette[i % len(palette)] for i, task in enumerate(unique_tasks)}

        overall_start = df["Start"].min().to_pydatetime()
        overall_end = df["Finish"].max().to_pydatetime()

        # ensure chart covers from today (or earlier) to deadline if provided
        today_dt = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        # consider deadline if available
        deadline_raw = p.get("deadline") or p.get("deadline_date")
        deadline_dt = None
        try:
            if deadline_raw:
                deadline_dt = _to_datetime(deadline_raw)
        except Exception:
            deadline_dt = None

        range_start = min(overall_start, today_dt)
        if deadline_dt:
            range_end = max(overall_end, deadline_dt)
        else:
            range_end = overall_end

        span_days = max(1, (range_end - range_start).days)
        pad = max(1, int(span_days * 0.07))
        range_start = range_start - datetime.timedelta(days=pad)
        range_end = range_end + datetime.timedelta(days=pad)

        fig = px.timeline(df, x_start="Start", x_end="Finish", y="Owner",
                          title="AI Proposal Generator — Project Schedule",
                          hover_data=["Task", "Owner", "Effort", "Percent", "OriginalHours", "CompressionPct"])

        fig.update_yaxes(
            title_text="",
            autorange="reversed",
            tickvals=list(range(len(unique_owners))),
            ticktext=unique_owners
        )

        row_height = 52
        height = max(420, row_height * len(unique_owners) + 320)
        fig.update_layout(
            margin=dict(l=140, r=30, t=110, b=260),
            width=width,
            height=height,
            showlegend=False,
            font=dict(family="Roboto", size=12),
            plot_bgcolor="rgba(245,247,250,1)",
            hoverlabel=dict(bgcolor="white", font_size=12)
        )

        fig.update_traces(marker=dict(line=dict(width=0), opacity=0.0))

        for _, row in df.iterrows():
            start = row["Start"].to_pydatetime()
            finish = row["Finish"].to_pydatetime()
            pct = float(row["Percent"])
            color = task_color_map[row["Task"]]
            y_val = int(row["Y_Val"])
            y0, y1 = y_val - 0.36, y_val + 0.36
            fig.add_shape(type="rect", x0=start, x1=finish, y0=y0, y1=y1,
                          xref="x", yref="y", fillcolor=color, line=dict(width=1, color=color), opacity=0.78)
            if pct > 0:
                prog_end = start + (finish - start) * (pct / 100.0)
                fig.add_shape(type="rect", x0=start, x1=prog_end, y0=y0, y1=y1,
                              xref="x", yref="y", fillcolor=color, line=dict(width=0), opacity=1.0)
            task_label = f"<b>{row['Task']}</b><br><span style='font-size:13px'>{int(row['Effort'])}h</span>"
            fig.add_annotation(
                x=start + datetime.timedelta(seconds=0.5),
                y=y_val,
                xref="x", yref="y",
                text=task_label, showarrow=False,
                font=dict(size=13, color="white"),
                align="left", xanchor="left"
            )

        # dependencies arrows
        name_to_y_val = {r["Task"]: r["Y_Val"] for _, r in df.iterrows()}
        name_to_finish = {r["Task"]: r["Finish"].to_pydatetime() for _, r in df.iterrows()}
        for _, row in df.iterrows():
            deps = _ensure_list(row.get("Depends") or [])
            cur_y_val = row["Y_Val"]
            cur_start = row["Start"].to_pydatetime()
            for dep_name in deps:
                match_name = None
                for n in name_to_y_val.keys():
                    if dep_name == n or dep_name.lower() in n.lower() or n.lower() in dep_name.lower():
                        match_name = n
                        break
                if match_name:
                    dep_y_val = name_to_y_val[match_name]
                    dep_finish = name_to_finish[match_name]
                    fig.add_annotation(x=cur_start, y=cur_y_val, ax=dep_finish, ay=dep_y_val,
                                       xref="x", yref="y", axref="x", ayref="y",
                                       standoff=8, showarrow=True, arrowhead=3, arrowsize=1.8, arrowwidth=1.2,
                                       arrowcolor="rgba(80,80,80,0.9)", opacity=0.9)

        # today marker
        try:
            if range_start <= today_dt <= range_end:
                fig.add_shape(type="line", x0=today_dt, x1=today_dt, y0=-0.5, y1=len(unique_owners)-0.5,
                              xref="x", yref="y", line=dict(color="crimson", dash="dash", width=1.6), opacity=0.9)
                fig.add_annotation(x=today_dt, y=len(unique_owners)-0.5 + 0.85, xref="x", yref="y",
                                   text="Today", showarrow=False, font=dict(color="crimson", size=11))
        except Exception:
            logger.debug("Could not draw today marker", exc_info=True)

        # footer
        project_end_str = range_end.strftime("%d %b %Y")
        footer = f"Project Range: {range_start.strftime('%d %b %Y')} — {project_end_str}  |  Total Effort: {total_effort_str} hours  |  Estimated FTEs: {est_ftes_str}"
        if adjusted_note:
            footer = footer + "  |  " + adjusted_note
        if capacity_hours is not None:
            footer = footer + f"  |  Capacity (est): {capacity_hours}h"
        if capacity_note:
            footer = footer + "  |  " + capacity_note
        fig.add_annotation(xref="paper", yref="paper", x=0.01, y=-0.36, text=footer,
                           showarrow=False, font=dict(size=12, color="#222222"), align="left")

        # team annotation
        if members:
            lines = []
            for m in members:
                nm = m.get("name")
                rl = m.get("role") or ""
                hrs = m.get("hours")
                if hrs:
                    lines.append(f"<b>{nm}</b> — {rl} — {int(hrs)}h")
                else:
                    lines.append(f"<b>{nm}</b>" + (f" — {rl}" if rl else ""))
            team_text = " | ".join(lines)
            fig.add_annotation(xref="paper", yref="paper", x=0.01, y=-0.48,
                               text=f"Team ({len(members)}): {team_text}", showarrow=False,
                               font=dict(size=11, color="#333333"), align="left")

        # overflow banner
        if capacity_hours is not None and overflow_hours and overflow_hours > 0:
            try:
                fig.add_annotation(xref="paper", yref="paper", x=0.5, y=-0.02,
                                   text=f"⚠️ Capacity exceeded by {int(overflow_hours)} hours — consider extending deadline or adding FTEs",
                                   showarrow=False, font=dict(size=13, color="red"), align="center")
            except Exception:
                logger.debug("Could not draw overflow annotation", exc_info=True)

        # axis format and range
        tickformat = "%d %b %Y" if (range_end - range_start).days <= 90 else ("%b %Y" if (range_end - range_start).days <= 730 else "%Y")
        fig.update_xaxes(range=[range_start, range_end], tickformat=tickformat, tickangle=-30, automargin=True)

        png = pio.to_image(fig, format="png", width=width, height=height, scale=2)
        if png and len(png) > 200:
            return png
        else:
            logger.warning("Gantt export produced tiny image (len=%d).", len(png) if png else 0)
            return _placeholder_png_bytes("Empty chart")

    except Exception as e:
        logger.exception("generate_gantt_image failed: %s", e)
        return _placeholder_png_bytes("Gantt chart failed")
    
def _get_stage_style(stage_type: str) -> Dict[str, str]:
    """
    Определяет профессиональные стили Graphviz (цвет, текст) на основе типа этапа.
    """
    # Палитра цветов (взято из Bootstrap/Flat UI для профессионального вида)
    colors = {
        "Planning": "#007ACC",      # Blue (Design/Plan)
        "Setup": "#9A67BF",         # Purple (Infrastructure/Setup)
        "Development": "#5CBA5C",   # Green (Build/Progress)
        "Integration": "#218838",   # Darker Green (Integration)
        "Testing": "#F0AD4E",       # Orange (Review/Quality)
        "Deployment": "#D9534F",    # Red (Finalization/Go-Live)
        "Generic": "#6C757D",       # Gray (Default)
    }
    
    # Приводим тип к общему формату для надежного сопоставления
    normalized_type = stage_type.strip().title()
    fill_color = colors.get(normalized_type, colors["Generic"])
    
    # Цвет текста: белый для темного фона, черный для светлого.
    font_color = "#FFFFFF" if fill_color in ["#007ACC", "#9A67BF", "#218838", "#D9534F", "#6C757D"] else "#333333"

    return {
        "fillcolor": fill_color,
        "fontcolor": font_color
    }


def generate_lifecycle_diagram(data: Dict[str, Any], width: int = 1100, height: int = None) -> bytes:

    """
    Генерирует профессиональную диаграмму жизненного цикла проекта (DAG) 
    с цветовым кодированием этапов.
    """
    
    # Получаем этапы (либо из ввода, либо генерируем через LLM)
    lifecycle_stages = data.get("lifecycle_stages") or _generate_lifecycle_stages_with_agent(data)
    if not lifecycle_stages:
        logger.warning("No lifecycle stages available for diagram.")
        # Предполагаем наличие функции заглушки
        return _placeholder_png_bytes("Lifecycle diagram unavailable", width=800, height=300)

    # 1. Настройка Графа (Улучшено)
    # 💡 ИЗМЕНЕНИЕ ДВИЖКА: Рекомендуется fdp для более плотной компоновки
    g = graphviz.Digraph(format="png", engine="dot") 
    
    # 💡 ИЗМЕНЕНИЕ РАССТОЯНИЙ: Уменьшаем, чтобы сблизить узлы
    # nodesep - горизонтальное расстояние; ranksep - вертикальное расстояние
    g.attr(splines="curved", nodesep="0.01", ranksep="0.02", bgcolor="transparent", overlap="false", K="0.6")
    
    # Общие атрибуты Узлов 
    g.attr("node", 
           shape="box", 
           style="rounded,filled", 
           fontname="Arial", 
           fontsize="16",
           color="#333333", 
           penwidth="1.0",
           fixedsize="false",
           width="4.0",
           height="1.5"
    )
    


    id_map = {}
    
    # 2. Создание Узлов
    for i, s in enumerate(lifecycle_stages):
        nid = f"n{i}"
        label = s.get("name", f"Stage {i+1}")
        desc = s.get("description", "")
        stage_type = s.get("type", "Generic") 

        # ИСПРАВЛЕНИЕ ОШИБКИ: Экранируем символ '&' для HTML-меток
        label = label.replace("&", "&amp;")
        desc = desc.replace("&", "&amp;") 
        
        # 💡 НОВОЕ УЛУЧШЕНИЕ: ИСПОЛЬЗУЕМ HTML-ТАБЛИЦЫ ДЛЯ ВЫРАВНИВАНИЯ
# 💡 НОВОЕ УЛУЧШЕНИЕ: ИСПОЛЬЗУЕМ HTML-ТАБЛИЦЫ ДЛЯ ВЫРАВНИВАНИЯ
        if desc:
            # Используем <TABLE> с ALIGN="CENTER" и CELLPADDING/CELLSPACING=0
            # Уменьшим CELLPADDING/CELLSPACING, если нужно сжать текст
            html_label = f'''<
<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="2" ALIGN="CENTER"> 
    <TR><TD ALIGN="CENTER"><B>{label}</B></TD></TR>
    <TR><TD ALIGN="CENTER"><FONT POINT-SIZE="14">{desc}</FONT></TD></TR>
</TABLE>
>'''
        else:
            html_label = f'<<B>{label}</B>>'
            html_label = f'<<B>{label}</B>>'
            
        styles = _get_stage_style(stage_type)
        
        id_map[label] = nid
        # Передаем HTML-метку
        g.node(nid, label=html_label, **styles)


   # 3. Создание Ребер (Связей)
    for s in lifecycle_stages:
        name = s.get("name")
        # 🔔 ИСПРАВЛЕНИЕ: Экранируем name для поиска в id_map
        safe_name = name.replace("&", "&amp;") 
        nid = id_map.get(safe_name) 
        
        if not nid: 
             # Если не найдено по экранированному имени, попробуем по неэкранированному (на всякий случай)
             nid = id_map.get(name)
             if not nid: continue 
        
        for dep in s.get("depends_on", []):
            dep = str(dep)
            # 🔔 ИСПРАВЛЕНИЕ: Экранируем зависимость для поиска в id_map
            safe_dep = dep.replace("&", "&amp;")
            dep_id = id_map.get(safe_dep) # Ищем по экранированному имени
            
            # Если не найдено по экранированному имени, то создаем заглушку, используя оригинальное 'dep'
            if not dep_id:
                # ... (остальной код для обработки неизвестной/отсутствующей зависимости)
                # Обработка неизвестной (отсутствующей) зависимости
                dep_id = f"missing_{abs(hash(dep)) % (10**8)}"
                if dep_id not in id_map.values():
                    # Создаем заглушку для отсутствующего узла
                    g.node(dep_id, 
                           label=f"MISSING: {dep}", 
                           style="dashed,filled", 
                           fillcolor="#F9EBEA", # Светло-красный/бежевый
                           fontcolor="#D9534F",
                           penwidth="2.0")
                    id_map[dep] = dep_id
            
            # Добавление связи
            g.edge(dep_id, nid)

    # 4. Рендеринг и Возврат

    try:

        TARGET_DPI = 400
        g.attr(dpi=str(TARGET_DPI))


        MAX_HEIGHT_INCHES = 8.0

        MAX_WIDTH_INCHES = 50.0  # достаточно большое значение, чтобы ширина не была принудительно ограничена
        g.attr(size=f"{MAX_WIDTH_INCHES},{MAX_HEIGHT_INCHES}", ratio="auto")

        # Рендерим в PNG
        png_bytes = g.pipe(format="png")

        # --- Пост-обработка: если картинка всё ещё выше, уменьшаем её пропорционально ---
        if png_bytes:
            try:
                img = Image.open(BytesIO(png_bytes))
                # целевое максимальное количество пикселей по высоте
                max_pixels = int(MAX_HEIGHT_INCHES * TARGET_DPI)

                if img.height > max_pixels:
                    # вычисляем новые размеры, сохраняя пропорции
                    new_height = max_pixels
                    new_width = int(img.width * (new_height / img.height))

                    # ресайз с высококачественной фильтрацией
                    img = img.resize((new_width, new_height), Image.LANCZOS)

                    out_buf = BytesIO()
                    # сохраняем обратно в PNG, указывая DPI для встраивания метаданных
                    img.save(out_buf, format="PNG", dpi=(TARGET_DPI, TARGET_DPI))
                    png_bytes = out_buf.getvalue()

                # возвращаем итоговый PNG
                if png_bytes and len(png_bytes) > 100:
                    return png_bytes
            except Exception:
                logger.exception("Lifecycle post-processing (resize) failed, returning raw render", exc_info=True)

        # fallback если png_bytes отсутствует или мелкий
        if png_bytes and len(png_bytes) > 100:
            return png_bytes

    except Exception:
        logger.exception("Graphviz pipe() failed during rendering")
    
    # Возврат ошибки в виде заглушки, если рендеринг не удался
    return _placeholder_png_bytes("Lifecycle diagram failed to render")


def _safe_label(s: Any) -> str:
    """Sanitize labels for Graphviz, avoid None and escape &."""
    if s is None:
        return ""
    t = str(s)
    # simple cleanup
    t = t.strip()
    t = t.replace("&", "&amp;")
    t = t.replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"[\r\n]+", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t

def generate_uml_diagram(
    proposal: Dict[str, Any],
    width: int = 1100,
    height: Optional[int] = None,
    agent_mode: bool = True,
    llm_timeout_seconds: int = 30,
) -> bytes:
    """
    Генерирует UML-диаграмму в точности как на ваших скриншотах:
      • Светло-бирюзовый фон (#E0F7FA)
      • Белые блоки с серой рамкой
      • Пунктирные стрелки
      • Ортогональные линии
      • Читаемый шрифт Arial
      • Автоматическое масштабирование под страницу
    """
    from io import BytesIO
    import time
    import traceback
    import graphviz
    from PIL import Image

    uml_meta = {
        "source": "agent" if agent_mode else "direct",
        "llm_called": False,
        "duration_ms": None,
        "node_count": 0,
        "relation_count": 0,
        "errors": [],
    }
    t0 = time.time()

    try:
        # 1. Получаем UML-структуру
        uml_struct = (
            proposal.get("uml_structure")
            or proposal.get("uml")
            or (proposal.get("visualization") or {}).get("uml_structure")
        )

        if not uml_struct and agent_mode:
            try:
                uml_struct = _generate_uml_with_agent(proposal) or {}
                uml_meta["llm_called"] = True
            except Exception as e:
                tb = traceback.format_exc()
                uml_meta["errors"].append({"code": "agent_failed", "message": str(e), "trace": tb})
                uml_struct = {}

        # Если всё ещё пусто — создаём минимальную структуру из scope/technologies
        if not isinstance(uml_struct, dict) or not uml_struct.get("components"):
            uml_meta["errors"].append({"code": "fallback_used", "message": "Generated minimal UML from proposal"})
            uml_struct = _generate_minimal_uml_from_proposal(proposal)

        components = uml_struct.get("components", [])
        relations = uml_struct.get("relations", [])

        if not components:
            proposal["_uml_meta"] = uml_meta
            return _placeholder_png_bytes("UML structure empty")

        # 2. Нормализация компонентов
        def _safe(v, max_len=60):
            if v is None:
                return ""
            s = str(v).strip()
            return s if len(s) <= max_len else s[: max_len - 3] + "..."

        seen_ids = set()
        normalized_components = []
        for c in components:
            raw_id = str(c.get("id") or c.get("name") or "")
            cid = re.sub(r"\s+", "_", raw_id.strip()) or f"comp_{len(seen_ids)+1}"
            if cid in seen_ids:
                suffix = 1
                while f"{cid}_{suffix}" in seen_ids:
                    suffix += 1
                cid = f"{cid}_{suffix}"
            seen_ids.add(cid)

            normalized_components.append({
                "id": cid,
                "name": _safe(c.get("name") or raw_id),
                "stereotype": _safe(c.get("stereotype") or "component"),
                "responsibilities": c.get("responsibilities") or [],
                "attributes": c.get("attributes") or [],
                "notes": _safe(c.get("notes") or ""),
            })

        normalized_relations = []
        for r in relations:
            try:
                normalized_relations.append({
                    "from": str(r.get("from")),
                    "to": str(r.get("to")),
                    "type": (r.get("type") or "dependency").lower(),
                    "label": _safe(r.get("label") or ""),
                })
            except Exception:
                uml_meta["errors"].append({"code": "bad_relation", "item": r})

        uml_meta["node_count"] = len(normalized_components)
        uml_meta["relation_count"] = len(normalized_relations)

        # 3. Graphviz — точная копия стиля из ваших скриншотов
        g = graphviz.Digraph(format="png", engine="dot")
        g.attr(
            bgcolor="#E0F7FA",           # светло-бирюзовый фон
            splines="ortho",             # ортогональные линии
            nodesep="0.4",
            ranksep="0.7",
            overlap="false",
            concentrate="true",
            rankdir="TB",                # сверху вниз
        )
        g.attr(
            "node",
            shape="box",
            style="filled,rounded",
            fillcolor="white",
            fontname="Arial",
            fontsize="12",
            color="#CCCCCC",
            penwidth="1.0",
            margin="0.15",
        )
        g.attr(
            "edge",
            fontname="Arial",
            fontsize="10",
            arrowsize="0.8",
            penwidth="1.0",
            style="dotted",
            color="#333333",
        )

        id_map = {}
        for i, comp in enumerate(normalized_components):
            cid = comp["id"]
            name = comp["name"]
            stereo = comp.get("stereotype", "component").lower()
            attrs = comp.get("attributes") or []
            resps = comp.get("responsibilities") or []
            notes = comp.get("notes", "")

            def _collapse(lines, max_lines=5):
                out = [_safe(l) for l in lines[:max_lines]]
                if len(lines) > max_lines:
                    out.append("…")
                return out

            details = _collapse(resps + attrs)

            header = f"<B>{name}</B>"
            if stereo and stereo != "component":
                header = f"<I>{stereo}</I><BR/>{header}"

            rows = [f"<TR><TD ALIGN='CENTER' CELLPADDING='6'>{header}</TD></TR>"]
            if details:
                rows.append(f"<TR><TD ALIGN='LEFT' CELLPADDING='4'><FONT POINT-SIZE='11'>{'<BR/>'.join(details)}</FONT></TD></TR>")
            if notes:
                rows.append(f"<TR><TD ALIGN='LEFT' CELLPADDING='4'><FONT POINT-SIZE='10' COLOR='#666666'><I>{notes}</I></FONT></TD></TR>")

            html_label = f"""<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="0" COLOR="#CCCCCC">{''.join(rows)}</TABLE>>"""
            node_name = f"node_{i}"
            g.node(node_name, label=html_label, fillcolor="white")
            id_map[cid] = node_name

        # Стрелки — как в примерах
        for rel in normalized_relations:
            try:
                frm = rel["from"]
                to = rel["to"]
                frm_node = id_map.get(frm)
                to_node = id_map.get(to)

                # Поиск по частичному совпадению
                if not frm_node:
                    for k, v in id_map.items():
                        if str(frm).lower() in k.lower() or k.lower() in str(frm).lower():
                            frm_node = v
                            break
                if not to_node:
                    for k, v in id_map.items():
                        if str(to).lower() in k.lower() or k.lower() in str(to).lower():
                            to_node = v
                            break

                if not frm_node or not to_node:
                    continue

                rtype = rel.get("type", "dependency").lower()
                arrowhead = "vee" if rtype == "dependency" else "none"
                g.edge(frm_node, to_node, label=rel.get("label", ""), arrowhead=arrowhead, style="dotted")
            except Exception:
                pass  # игнорируем ошибки связей

        # 4. Рендер и масштабирование
        try:
            TARGET_DPI = 300
            MAX_HEIGHT_INCHES = 9.0
            MAX_WIDTH_INCHES = 12.0
            g.attr(dpi=str(TARGET_DPI))
            g.attr(size=f"{MAX_WIDTH_INCHES},{MAX_HEIGHT_INCHES}", ratio="compress")

            png_bytes = g.pipe(format="png")
            if png_bytes:
                img = Image.open(BytesIO(png_bytes))
                max_h = int(MAX_HEIGHT_INCHES * TARGET_DPI)
                max_w = int(MAX_WIDTH_INCHES * TARGET_DPI)
                if img.height > max_h or img.width > max_w:
                    ratio = min(max_h / img.height, max_w / img.width)
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                    buf = BytesIO()
                    img.save(buf, format="PNG", dpi=(TARGET_DPI, TARGET_DPI))
                    png_bytes = buf.getvalue()

                uml_meta["rendered_bytes"] = len(png_bytes)
                proposal["_uml_meta"] = uml_meta
                return png_bytes
        except Exception:
            uml_meta["errors"].append({"code": "render_failed", "trace": traceback.format_exc()})

        proposal["_uml_meta"] = uml_meta
        return _placeholder_png_bytes("UML render failed")

    except Exception as e:
        tb = traceback.format_exc()
        uml_meta["errors"].append({"code": "top_level_error", "message": str(e), "trace": tb})
        proposal["_uml_meta"] = uml_meta
        return _placeholder_png_bytes("UML generation failed")
    finally:
        uml_meta["duration_ms"] = int((time.time() - t0) * 1000)


def _generate_minimal_uml_from_proposal(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """Генерирует минимальную рабочую UML-структуру, если агент не дал ничего"""
    techs = ", ".join(proposal.get("technologies", [])) or "Python/FastAPI"
    scope = proposal.get("scope", "") or "Integration project"

    return {
        "components": [
            {"id": "client", "name": "Клиент", "stereotype": "actor", "responsibilities": ["Внешние пользователи"], "attributes": [], "notes": ""},
            {"id": "frontend", "name": "Веб-интерфейс", "stereotype": "service", "responsibilities": ["UI/UX"], "attributes": [], "notes": ""},
            {"id": "api", "name": "REST API", "stereotype": "service", "responsibilities": ["Обработка запросов"], "attributes": [f"Tech: {techs}"], "notes": ""},
            {"id": "worker", "name": "Фоновые задачи", "stereotype": "worker", "responsibilities": ["Обработка данных"], "attributes": [], "notes": ""},
            {"id": "db", "name": "PostgreSQL", "stereotype": "database", "responsibilities": ["Хранение данных"], "attributes": [], "notes": ""},
            {"id": "external", "name": "Внешние системы", "stereotype": "saas", "responsibilities": ["CRM / Shopify"], "attributes": [], "notes": ""},
        ],
        "relations": [
            {"from": "client", "to": "frontend", "type": "dependency", "label": ""},
            {"from": "frontend", "to": "api", "type": "dependency", "label": ""},
            {"from": "api", "to": "worker", "type": "dependency", "label": ""},
            {"from": "api", "to": "db", "type": "dependency", "label": ""},
            {"from": "worker", "to": "db", "type": "dependency", "label": ""},
            {"from": "api", "to": "external", "type": "dependency", "label": ""},
        ],
    }