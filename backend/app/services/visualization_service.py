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
from backend.app.services.openai_service import _generate_lifecycle_stages_with_agent

logger = logging.getLogger("uvicorn.error")

# ------------------ Helpers ------------------

def _ensure_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, (tuple, set)):
        return list(x)
    if isinstance(x, str):
        s = x.strip()
        # try parse json
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            # comma-separated fallback
            if "," in s and "->" not in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s]
    return [x]

def _to_datetime(obj) -> Optional[datetime.datetime]:
    if obj is None:
        return None
    if isinstance(obj, datetime.datetime):
        return obj
    if isinstance(obj, datetime.date):
        return datetime.datetime.combine(obj, datetime.time.min)
    if isinstance(obj, str):
        s = obj.strip()
        if not s:
            return None
        # common formats
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except Exception:
                continue
        # fallback to pandas
        try:
            return pd.to_datetime(s).to_pydatetime()
        except Exception:
            return None
    try:
        return pd.to_datetime(obj).to_pydatetime()
    except Exception:
        return None

def _safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return float(default)
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        return float(s)
    except Exception:
        try:
            # try replace comma decimal
            return float(str(v).replace(",", "."))
        except Exception:
            return float(default)




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
    Robust agent that enriches/normalizes phases/milestones for Gantt.

    Returns list of dicts with keys:
      - name (str), start (ISO str), end (ISO str), duration_days (int),
        duration_weeks (int), percent_complete (float), owner (str),
        effort_hours (float), depends_on (List[str])
        
    ---
    💡 УЛУЧШЕНИЕ:
    - Динамически рассчитывает 'percent_complete' на основе
      сегодняшней даты, если он не предоставлен (т.е. равен 0).
    ---
    """
    try:
        logger.debug("agent_enrich_schedule: start")

        import math
        try:
            from dateutil import parser as _dateutil_parser  # type: ignore
        except Exception:
            _dateutil_parser = None

        # ... (внутренние хелперы _sanitize_label, _parse_duration_to_weeks, _parse_date остаются без изменений) ...
        # (Просто скопируйте их из вашего существующего файла)

        # helper: sanitize label
        def _sanitize_label(txt: Any) -> str:
            if txt is None:
                return ""
            s = str(txt)
            s = re.sub(r"[\r\n\t]+", " ", s)
            s = re.sub(r"\s{2,}", " ", s)
            s = s.strip(" '\"")
            return s.strip()

        # helper: parse duration (weeks/days/months) from various forms
        def _parse_duration_to_weeks(val: Any, fallback_weeks: int = default_week_duration) -> float:
            if val is None:
                return float(fallback_weeks)
            # numeric
            try:
                if isinstance(val, (int, float)):
                    return float(val)
                vs = str(val).strip().lower()
                # plain number -> weeks
                if re.fullmatch(r"^\d+(\.\d+)?$", vs):
                    return float(vs)
                # forms: "14d", "14 days", "2w", "2 weeks", "1.5m" (months)
                m = re.match(r"^(\d+(\.\d+)?)[\s-]*(d|day|days)$", vs)
                if m:
                    days = float(m.group(1))
                    return max(1.0, days / 7.0)
                m = re.match(r"^(\d+(\.\d+)?)[\s-]*(w|week|weeks)$", vs)
                if m:
                    return float(m.group(1))
                m = re.match(r"^(\d+(\.\d+)?)[\s-]*(m|month|months)$", vs)
                if m:
                    months = float(m.group(1))
                    # convert months -> weeks roughly
                    return max(1.0, months * 4.345)
                # compact forms: "2w", "14d"
                m = re.match(r"^(\d+(\.\d+)?)([wdm])$", vs)
                if m:
                    v = float(m.group(1))
                    unit = m.group(3)
                    if unit == "d":
                        return max(1.0, v / 7.0)
                    if unit == "w":
                        return v
                    if unit == "m":
                        return max(1.0, v * 4.345)
                # fallback: contains number -> take first number as weeks
                m = re.search(r"(\d+(\.\d+)?)", vs)
                if m:
                    return float(m.group(1))
            except Exception:
                logger.debug("Duration parse failed for value=%r", val, exc_info=True)
            return float(fallback_weeks)

        # helper: parse date robustly (try dateutil, then _to_datetime, then iso)
        def _parse_date(val: Any):
            if val is None:
                return None
            # already datetime
            try:
                if isinstance(val, (datetime.datetime, datetime.date)):
                    return val if isinstance(val, datetime.datetime) else datetime.datetime.combine(val, datetime.time.min)
            except Exception:
                pass
            s = str(val).strip()
            if not s:
                return None
            # try ISO first
            try:
                dt = _to_datetime(s)
                if dt:
                    return dt
            except Exception:
                pass
            # try dateutil
            if _dateutil_parser is not None:
                try:
                    return _dateutil_parser.parse(s)
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


        # select source list
        raw = proposal.get("milestones") or proposal.get("phases_list") or []
        if (not raw) and isinstance(proposal.get("suggested_phases"), list):
            raw = proposal.get("suggested_phases")

        items = _ensure_list(raw)

        if not items:
            logger.info("agent_enrich_schedule: no input phases; creating sensible defaults")
            items = [
                {"phase_name": "Setup & Data Modeling", "duration_weeks": default_week_duration,
                 "tasks": "Environment setup, data inventory, schema design"},
                {"phase_name": "LLM Integration & Testing", "duration_weeks": default_week_duration * 2,
                 "tasks": "Integrate model, prompts, API tests, QA"}
            ]

        # 💡 ИЗМЕНЕНИЕ: Получаем 'today' для расчета %
        today_dt = datetime.datetime.combine(datetime.date.today(), datetime.time.min)

        # base date: prefer proposal_date, then today
        base_iso = proposal.get("proposal_date") or proposal.get("proposal_date_iso") or proposal.get("deadline") or None
        base_dt = _parse_date(base_iso)
        if base_dt is None:
            base_dt = today_dt

        # owner heuristics
        # ... (скопируйте вашу 'owner_map' и 'guess_owner' сюда без изменений) ...
        owner_map = [
            (["prompt", "prompting", "prompt engineering"], "AI Developer"),
            (["data audit", "data modeling", "architecture", "design", "requirements"], "Data Engineer"),
            (["crm", "e-commerce", "integration", "api", "sync", "shopify"], "Backend Engineer"),
            (["test", "qa", "cycle", "validation", "acceptance"], "QA Engineer"),
            (["deploy", "deployment", "release", "cutover", "orchestration"], "DevOps"),
            (["planning", "management", "status", "meeting"], "Project Manager"),
        ]


        def guess_owner(tasks_text: Any) -> str:
            
            txt = _sanitize_label(tasks_text).lower()
            if not txt:
                return "Engineering"
            for kws, role in owner_map:
                for kw in kws:
                    if kw in txt:
                        return role
            # if looks like client approval or sign-off
            if re.search(r"(sign-?off|approval|client)", txt):
                return "Client"
            return "Engineering"


        enriched: List[Dict[str, Any]] = []
        used_names = {}
        cursor = base_dt

        for i, it in enumerate(items):
            # ... (скопируйте 'allow string items', 'name', 'ensure unique name', 'duration' сюда без изменений) ...
            
            # allow string items
            if isinstance(it, str):
                it = {"phase_name": it}

            # name
            raw_name = it.get("phase_name") or it.get("name") or it.get("title") or f"Phase {i+1}"
            name = _sanitize_label(raw_name) or f"Phase {i+1}"

            # ensure unique name (append suffix when duplicate)
            base_name = name
            cnt = used_names.get(base_name, 0)
            if cnt:
                name = f"{base_name} ({cnt+1})"
            used_names[base_name] = cnt + 1

            # duration -> weeks
            dur_hours = it.get("duration_hours") or it.get("duration") or (default_week_duration * 40)
            try:
                dur_hours = float(dur_hours)
            except Exception:
                dur_hours = default_week_duration * 40

            # Конвертируем часы в дни (8ч = 1 день)
            duration_days = max(1, int(dur_hours / 8.0))
            dur_weeks = dur_hours / 40.0  # для совместимости с расчётом effort

            # 💡 ИЗМЕНЕНИЕ: Логика 'percent_complete'
            try:
                pct = float(it.get("percent_complete") or it.get("percent") or 0.0)
            except Exception:
                pct = 0.0
            
            # start/end parsing
            start_dt = _parse_date(it.get("start") or it.get("start_date"))
            end_dt = _parse_date(it.get("end") or it.get("end_date"))

            # ... (скопируйте логику расчета start/end dt сюда без изменений) ...
            if start_dt is None and end_dt is None:
                start_dt = cursor
                end_dt = start_dt + datetime.timedelta(days=duration_days)
                cursor = end_dt
            elif start_dt is not None and end_dt is None:
                end_dt = start_dt + datetime.timedelta(days=duration_days)
                cursor = end_dt
            elif start_dt is None and end_dt is not None:
                start_dt = end_dt - datetime.timedelta(days=duration_days)
                cursor = end_dt
            else:
                # both present: ensure ordering
                if start_dt >= end_dt:
                    end_dt = start_dt + datetime.timedelta(days=duration_days)
                cursor = end_dt

            # 💡 ИЗМЕНЕНИЕ: Динамический расчет 'percent_complete'
            # Рассчитываем pct, ТОЛЬКО если он не был предоставлен (т.е. равен 0)
            if pct <= 0:
                if today_dt >= end_dt:
                    pct = 100.0
                elif today_dt > start_dt and end_dt > start_dt:
                    days_passed = (today_dt - start_dt).days
                    total_days = (end_dt - start_dt).days
                    pct = (days_passed / total_days) * 100.0
                else:
                    # Задача еще не началась
                    pct = 0.0
            
            pct = max(0.0, min(100.0, pct))
            
            # ... (скопируйте 'effort_hours', 'owner', 'depends_on' сюда без изменений) ...
            # effort_hours: prefer explicit numeric; otherwise dur_weeks * hours_per_week
            effort = None
            try:
                if it.get("effort_hours") is not None:
                    effort = float(it.get("effort_hours"))
                else:
                    # FIX: Прямое использование часов, никаких недель
                    effort = float(dur_hours) 
            except Exception:
                effort = float(dur_hours)

            _possible_owner_keys = ["owner", "owner_name", "assigned_to", "resource", "responsible", "ownerName"]
            owner_raw = None
            for ok in _possible_owner_keys:
                if isinstance(it, dict) and it.get(ok):
                    owner_raw = it.get(ok)
                    break

            # prefer explicit owner (string or dict with 'name'), else guess
            if isinstance(owner_raw, dict):
                # try common nested shapes
                owner_candidate = owner_raw.get("name") or owner_raw.get("title") or next(iter(owner_raw.values()), None)
            else:
                owner_candidate = owner_raw

            owner_candidate = _sanitize_label(owner_candidate) if owner_candidate is not None else ""
            if not owner_candidate:
                owner_candidate = guess_owner(it.get("tasks") or it.get("description") or it.get("notes") or it.get("title") or it.get("phase_name") or "")

            # final fallback
            owner = owner_candidate or "Engineering"


            # depends_on normalization: accept lists or comma-separated strings
            depends_raw = it.get("depends_on") or it.get("after") or it.get("depends") or it.get("predecessors") or []
            if isinstance(depends_raw, str):
                # split by comma/semicolon or "->"
                depends = [d.strip() for d in re.split(r"[;,/]|->", depends_raw) if d.strip()]
            else:
                depends = _ensure_list(depends_raw)

            enriched.append({
                "name": name,
                "start": (start_dt.isoformat() if isinstance(start_dt, (datetime.datetime, datetime.date)) else str(start_dt)),
                "end": (end_dt.isoformat() if isinstance(end_dt, (datetime.datetime, datetime.date)) else str(end_dt)),
                "duration_days": int((end_dt - start_dt).days) if (start_dt and end_dt) else duration_days,
                "duration_weeks": int(dur_weeks),
                "percent_complete": float(pct), # pct теперь динамический
                "owner": str(owner),
                "effort_hours": float(effort),
                "depends_on": [str(d) for d in depends]
            })

        # ... (скопируйте 'post-process: resolve depends_on' сюда без изменений) ...
        # post-process: resolve depends_on to actual enriched names (fuzzy, case-insensitive)
        names = [m["name"] for m in enriched]
        lc_map = {n.lower(): n for n in names}
        for m in enriched:
            deps = _ensure_list(m.get("depends_on") or [])
            resolved = []
            for d in deps:
                ds = _sanitize_label(d)
                if not ds:
                    continue
                # exact
                if ds in names:
                    resolved.append(ds)
                    continue
                low = ds.lower()
                if low in lc_map:
                    resolved.append(lc_map[low])
                    continue
                # partial match: prefer name that contains ds or vice versa
                matched = None
                for n in names:
                    if low in n.lower() or n.lower() in low:
                        matched = n
                        break
                if matched:
                    resolved.append(matched)
            # if nothing resolved, default to previous phase sequential dependency
            if not resolved:
                idx = names.index(m["name"])
                if idx > 0:
                    resolved = [names[idx - 1]]
            # uniquify preserving order
            m["depends_on"] = list(dict.fromkeys(resolved))

        logger.info("agent_enrich_schedule: produced %d enriched phases", len(enriched))
        return enriched

    except Exception as e:
        # ... (скопируйте 'except' блок сюда без изменений) ...
        logger.exception("agent_enrich_schedule failed: %s", e)
        # fallback single phase
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
    Генерирует профессиональную диаграмму Ганта в стиле "Resource Allocation".

    💡 УЛУЧШЕНИЯ, ВДОХНОВЛЕННЫЕ ПРОФ. ПРИМЕРОМ:
    - Y-ось - это 'Owner' (Исполнитель), что создает "swimlanes" для ресурсов.
    - Цвет полос основан на 'Task' (Фазе), а не на 'Owner'.
    - Метки на полосах очищены: 'Task' и 'Effort' отображаются прямо на полосе.
    - Использует 'agent_mode=True' для получения динамического % выполнения.
    """
    try:
        # 1) Получаем и нормализуем этапы
        if agent_mode:
            ms = agent_enrich_schedule(proposal)
        else:
            # (Ваша существующая логика 'else' для обратной совместимости)
            raw = proposal.get("milestones") or proposal.get("phases_list") or []
            ms = []
            for i, it in enumerate(_ensure_list(raw)):
                name = it.get("name") or it.get("phase_name") or it.get("title") if isinstance(it, dict) else str(it)
                ms.append({
                    "name": name,
                    "start": it.get("start") if isinstance(it, dict) else None,
                    "end": it.get("end") if isinstance(it, dict) else None,
                    "duration_days": it.get("duration_days") if isinstance(it, dict) else None,
                    "duration_weeks": it.get("duration_weeks") if isinstance(it, dict) else None,
                    "percent_complete": it.get("percent_complete") if isinstance(it, dict) else (it.get("percent") if isinstance(it, dict) else 0),
                    "owner": it.get("owner") if isinstance(it, dict) else "Engineering",
                    "effort_hours": it.get("effort_hours") if isinstance(it, dict) else None,
                    "depends_on": it.get("depends_on") if isinstance(it, dict) else []
                })
            if ms:
                ms = agent_enrich_schedule({"milestones": ms})

        # 2) Конвертируем в строки (rows)
        rows = []
        for i, m in enumerate(ms):
            start_dt = _to_datetime(m.get("start"))
            end_dt = _to_datetime(m.get("end"))

            if start_dt is None or end_dt is None:
                base = _to_datetime(proposal.get("proposal_date")) or datetime.datetime.combine(datetime.date.today(), datetime.time.min)
                est_days = int(m.get("duration_days") or (int(m.get("duration_weeks") or 2) * 7))
                start_dt = base + datetime.timedelta(days=i * max(1, est_days))
                end_dt = start_dt + datetime.timedelta(days=est_days)
            if start_dt >= end_dt:
                end_dt = start_dt + datetime.timedelta(days=max(1, int(m.get("duration_days") or 7)))

            pct = float(m.get("percent_complete") or 0.0)
            effort = float(m.get("effort_hours")) if m.get("effort_hours") is not None else float(m.get("duration_weeks") or ( (end_dt - start_dt).days / 7.0 ) ) * 40.0

            rows.append({
                "Task": str(m.get("name") or f"Phase {i+1}"),
                "Start": pd.to_datetime(start_dt),
                "Finish": pd.to_datetime(end_dt),
                "Percent": max(0.0, min(100.0, pct)),
                "Owner": m.get("owner") or "Engineering",
                "Effort": max(0.0, float(effort)),
                "Depends": _ensure_list(m.get("depends_on") or [])
            })

        if not rows:
            return _placeholder_png_bytes("No milestones")

        df = pd.DataFrame(rows)

        # 3) 💡 НОВАЯ ЛОГИКА Y-ОСИ (SWIMLANES)
        # Сортируем по времени начала, чтобы получить правильный порядок на диаграмме
        df = df.sort_values("Start")
        # Создаем стабильный список уникальных исполнителей (это будут наши swimlanes)
        unique_owners = list(df["Owner"].unique())
        # Создаем карту: 'Project Manager' -> 0, 'Data Engineer' -> 1, etc.
        owner_y_map = {owner: i for i, owner in enumerate(unique_owners)}
        # Добавляем числовую Y-координату в DataFrame
        df["Y_Val"] = df["Owner"].map(owner_y_map)

        # 4) 💡 НОВАЯ ЛОГИКА ЦВЕТА (по Задаче/Фазе)
        unique_tasks = list(df["Task"].unique())
        palette = px.colors.qualitative.Plotly
        task_color_map = {task: palette[i % len(palette)] for i, task in enumerate(unique_tasks)}

        # 5) Расчеты для макета
        total_effort_hours = df["Effort"].sum()
        total_effort_str = f"{int(total_effort_hours):,}".replace(",", " ")
        est_ftes_str = f"{total_effort_hours / 40.0:.1f}"

        overall_start = df["Start"].min().to_pydatetime()
        overall_end = df["Finish"].max().to_pydatetime()
        span_days = max(1, (overall_end - overall_start).days)
        pad = max(1, int(span_days * 0.07))
        range_start = overall_start - datetime.timedelta(days=pad)
        range_end = overall_end + datetime.timedelta(days=pad)

        # 6) Создание базовой диаграммы
        # 💡 ИЗМЕНЕНИЕ: y="Owner" - это использует 'Owner' для меток Y-оси
        fig = px.timeline(df, x_start="Start", x_end="Finish", y="Owner",
                          title="AI Proposal Generator — Project Schedule",
                          hover_data=["Task", "Owner", "Effort", "Percent"])

        # 💡 ИЗМЕНЕНИЕ: Обновляем Y-ось, чтобы она использовала наши числовые Y_Val
        # и сопоставляла их с текстовыми метками 'unique_owners'
        fig.update_yaxes(
            title_text="",
            autorange="reversed",
            tickvals=list(range(len(unique_owners))),
            ticktext=unique_owners
        )

        # 7) Настройка макета
        row_height = 52
        # Высота теперь зависит от кол-ва исполнителей, а не задач
        height = max(380, row_height * len(unique_owners) + 280)
        fig.update_layout(
            margin=dict(l=120, r=30, t=100, b=100), # l=160 для имен исполнителей
            width=width,
            height=height,
            showlegend=False,
            font=dict(family="Roboto", size=12),
            plot_bgcolor="rgba(245,247,250,1)",
            hoverlabel=dict(bgcolor="white", font_size=12)
        )

        # Скрываем стандартные полосы, мы нарисуем свои
        fig.update_traces(marker=dict(line=dict(width=0), opacity=0.0))

        # 8) 💡 НОВЫЙ ЦИКЛ ОТРИСОВКИ (ПРОФЕССИОНАЛЬНЫЙ СТИЛЬ)
        for _, row in df.iterrows():
            start = row["Start"].to_pydatetime()
            finish = row["Finish"].to_pydatetime()
            pct = float(row["Percent"])
            color = task_color_map[row["Task"]] # Цвет по Задаче
            
            # Получаем Y-координату для этого исполнителя
            y_val = row["Y_Val"]
            y0, y1 = y_val - 0.36, y_val + 0.36 # Высота полосы
            
            # A. Фоновая полоса (полупрозрачная)
            fig.add_shape(type="rect", x0=start, x1=finish, y0=y0, y1=y1,
                          xref="x", yref="y", fillcolor=color, line=dict(width=1, color=color), opacity=0.7)
            
            # B. Полоса прогресса (непрозрачная)
            if pct > 0:
                prog_end = start + (finish - start) * (pct / 100.0)
                fig.add_shape(type="rect", x0=start, x1=prog_end, y0=y0, y1=y1,
                              xref="x", yref="y", fillcolor=color, line=dict(width=0), opacity=1.0)
            
            
            # D. Метка Задачи + Усилий - слева на полосе (с имитацией чёрной обводки)
            task_label = f"<b>{row['Task']}</b><br><span style='font-size:13px'>{int(row['Effort'])}h</span>"

            # offsets (в пикселях) — чёрные тени по 4 сторонам
            outline_offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            for ox, oy in outline_offsets:
                fig.add_annotation(
                    x=start, y=y_val, xref="x", yref="y",
                    text=task_label, showarrow=False,
                    font=dict(size=16, color="black"),
                    align="left", xanchor="left",
                    xshift=ox, yshift=oy,
                    ax=10  # Отступ от левого края (как было)
                )

            # основная надпись поверх (белая)
            fig.add_annotation(
                x=start, y=y_val, xref="x", yref="y",
                text=task_label, showarrow=False,
                font=dict(size=16, color="white"),
                align="left", xanchor="left",
                ax=10
            )



        # 9)Стрелки зависимости
        # Нам нужны Y-координата и время окончания для каждой задачи
        name_to_y_val = {row["Task"]: row["Y_Val"] for _, row in df.iterrows()}
        name_to_finish = {row["Task"]: row["Finish"].to_pydatetime() for _, row in df.iterrows()}

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
                                        standoff=8, # Небольшой отступ от целевой точки (начала текущей задачи)
                                        showarrow=True, arrowhead=3, arrowsize=2.0, arrowwidth=1.2,
                                        arrowcolor="rgba(60,60,60,0.9)", opacity=0.9)

        # 10) Маркер "Today"
        try:
            today_dt = pd.to_datetime(datetime.date.today()).to_pydatetime()
            if range_start <= today_dt <= range_end:
                fig.add_shape(type="line", x0=today_dt, x1=today_dt, y0=-0.5, y1=len(unique_owners)-0.5,
                              xref="x", yref="y", line=dict(color="red", dash="dash", width=1.5), opacity=0.9)
                fig.add_annotation(x=today_dt, y=len(unique_owners)-0.5 + 0.8, xref="x", yref="y",
                                   text="Today", showarrow=False, font=dict(color="red", size=11))
        except Exception:
            logger.debug("Could not draw today marker", exc_info=True)

        # 11) Футер (подвал)
        project_end_str = overall_end.strftime("%d %b %Y")
        footer = f"Project End: {project_end_str}  |  Total Effort: {total_effort_str} hours  |  Estimated FTEs: {est_ftes_str}"
        fig.add_annotation(xref="paper", yref="paper", x=0.01, y=-0.34, text=footer,
                           showarrow=False, font=dict(size=13, color="#222222", family="Roboto Light"), align="left")

        # 12) НОВАЯ ЛЕГЕНДА (Цвет = Задача/Фаза)
        legend_y = -0.2
        legend_start_x  = -0.1
        gap = 0.1

        legend_shapes = []
        legend_annotations = []

        for i, task in enumerate(unique_tasks):
            lx = legend_start_x + i * gap
            # маленький цветной квадратик
            legend_shapes.append(
                dict(type="rect", xref="paper", yref="paper",
                    x0=lx, x1=lx + 0.02, y0=legend_y - 0.01, y1=legend_y + 0.01,
                    fillcolor=task_color_map[task], line=dict(width=0))
            )
            # подпись справа от квадратика
            legend_annotations.append(
                dict(xref="paper", yref="paper", x=lx + 0.025, y=legend_y,
                    text=task, showarrow=False, font=dict(size=10), align="left",
                    xanchor="left", yanchor="middle")
            )

        # приписываем shapes и annotations к фигуре
        fig.update_layout(shapes=fig.layout.shapes + tuple(legend_shapes),
                        annotations=fig.layout.annotations + tuple(legend_annotations))

        # 13) Форматирование оси X и экспорт
        tickformat = "%d %b %Y" if span_days <= 90 else ("%b %Y" if span_days <= 730 else "%Y")
        fig.update_xaxes(range=[range_start, range_end], tickformat=tickformat, tickangle=-30, automargin=True)

        png = pio.to_image(fig, format="png", width=width, height=height, scale=2)
        if png and len(png) > 200:
            return png
        else:
            logger.warning("Gantt export produced tiny image (len=%d).", len(png) if png else 0)
            return _placeholder_png_bytes("Empty chart")

    except Exception as e:
        logger.exception("generate_gantt_image (agent) failed: %s", e)
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