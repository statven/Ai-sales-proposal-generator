# backend/app/services/visualization_service.py
import io
import datetime
import logging
from typing import Dict, Any, Optional, List

from graphviz import Digraph
import pandas as pd
import plotly.express as px
import plotly.io as pio

logger = logging.getLogger("uvicorn.error")

def _safe_date_parse(s: Optional[str]) -> Optional[datetime.date]:
    if s is None:
        return None
    if isinstance(s, (datetime.date, datetime.datetime)):
        return s.date() if isinstance(s, datetime.datetime) else s
    if not isinstance(s, str):
        return None
    s = s.strip()
    # try common formats
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None

def _escape_label(s: str) -> str:
    if s is None:
        return ""
    # shorten, replace newlines, escape braces for graphviz record
    s = str(s).replace("\n", " ").strip()
    if len(s) > 180:
        s = s[:177] + "..."
    return s.replace("{", "\\{").replace("}", "\\}")

def generate_uml_image(proposal: Dict[str, Any], width: int = 1200) -> bytes:
    """
    Professional UML/Component diagram generator.
    Expects proposal['components'] = list of {id, title, description, depends_on:[ids], type: 'service/db/ui'}
    Returns PNG bytes (high quality).
    """
    comps = proposal.get("components") or []
    # fallback: synthesize from top-level keys
    if not isinstance(comps, list) or len(comps) == 0:
        comps = []
        for i, k in enumerate(list(proposal.keys())[:6]):
            comps.append({"id": f"k{i}", "title": k, "description": str(proposal.get(k,""))[:200], "depends_on": []})

    dot = Digraph(format="png")
    # global attributes for consistent style
    dot.attr("node", shape="record", style="filled", fillcolor="#FFFFFF", fontname="Arial")
    dot.attr("graph", rankdir="TB", fontsize="10", fontname="Arial")
    dot.attr("edge", color="#444444", fontname="Arial")

    # color mapping by type
    type_colors = {
        "service": "#e6f2ff",
        "db": "#fff2e6",
        "ui": "#f2ffe6",
        "default": "#f7f7f7"
    }

    # add nodes
    for c in comps:
        cid = str(c.get("id") or c.get("title"))
        title = _escape_label(c.get("title") or cid)
        desc = _escape_label(c.get("description") or "")
        node_label = f"{{{title}|{desc}}}"
        t = (c.get("type") or "default").lower()
        color = type_colors.get(t, type_colors["default"])
        dot.node(cid, label=node_label, fillcolor=color)

    # add edges with optional labels
    for c in comps:
        cid = str(c.get("id") or c.get("title"))
        deps = c.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            dep = str(dep)
            # optionally include relationship type
            rel = None
            if isinstance(deps, dict):
                rel = deps.get("type")
            dot.edge(cid, dep, arrowhead="vee")

    # layout and render
    try:
        png = dot.pipe(format="png")
        return png
    except Exception as e:
        logger.exception("UML generation failed: %s", e)
        # return small placeholder if needed
        return _placeholder_png_bytes("UML generation failed")

# placeholder util (PIL-free simple white image via plotly)
def _placeholder_png_bytes(text="Diagram unavailable", width=1200, height=300) -> bytes:
    fig = px.imshow([[255]], binary_string=True)
    fig.update_layout(width=width, height=height, margin=dict(l=10,r=10,t=10,b=10))
    buf = io.BytesIO()
    # create a minimal blank PNG
    img = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82'
    return img

def generate_gantt_image(proposal: Dict[str, Any], width: int = 1200) -> bytes:
    """
    Professional Gantt chart generator.
    Expects proposal['milestones'] = list of {name, start (ISO), end (ISO) or duration_days}
    """
    ms = proposal.get("milestones") or []
    # --- в generate_gantt_image --

    rows = []
    today = datetime.date.today()
    for i, m in enumerate(ms):
        name = m.get("name") or m.get("title") or f"Phase {i+1}"
        start = _safe_date_parse(m.get("start"))
        end = _safe_date_parse(m.get("end"))
        dur = m.get("duration_days") or m.get("duration") or m.get("duration_weeks")

        # Normalize to python date/datetime
        if start is None and end is None:
            start_date = today + datetime.timedelta(days=i * 7)
            if dur:
                try:
                    end_date = start_date + datetime.timedelta(days=int(dur))
                except Exception:
                    end_date = start_date + datetime.timedelta(days=7)
            else:
                end_date = start_date + datetime.timedelta(days=7)
        elif start is None and end is not None:
            end_date = end
            if isinstance(end_date, datetime.date) and not isinstance(end_date, datetime.datetime):
                end_date = datetime.datetime.combine(end_date, datetime.time.min)
            if dur:
                start_date = end_date - datetime.timedelta(days=int(dur))
            else:
                start_date = end_date - datetime.timedelta(days=7)
        elif start is not None and end is None:
            start_date = start
            if isinstance(start_date, datetime.date) and not isinstance(start_date, datetime.datetime):
                start_date = datetime.datetime.combine(start_date, datetime.time.min)
            if dur:
                end_date = start_date + datetime.timedelta(days=int(dur))
            else:
                end_date = start_date + datetime.timedelta(days=7)
        else:
            # both provided
            start_date = start
            end_date = end
            if isinstance(start_date, datetime.date) and not isinstance(start_date, datetime.datetime):
                start_date = datetime.datetime.combine(start_date, datetime.time.min)
            if isinstance(end_date, datetime.date) and not isinstance(end_date, datetime.datetime):
                end_date = datetime.datetime.combine(end_date, datetime.time.min)

        # ensure we have datetimes
        if not isinstance(start_date, datetime.datetime):
            start_date = pd.to_datetime(start_date).to_pydatetime()
        if not isinstance(end_date, datetime.datetime):
            end_date = pd.to_datetime(end_date).to_pydatetime()

        rows.append({"Task": name, "Start": start_date, "Finish": end_date})

    # fallback when rows empty
    if not rows:
        for i in range(3):
            s = today + datetime.timedelta(days=i * 14)
            f = s + datetime.timedelta(days=14)
            rows.append({"Task": f"Phase {i+1}", "Start": datetime.datetime.combine(s, datetime.time.min), "Finish": datetime.datetime.combine(f, datetime.time.min)})

    df = pd.DataFrame(rows)
    if not df.empty:
        df["Start"] = pd.to_datetime(df["Start"])
        df["Finish"] = pd.to_datetime(df["Finish"])
    else:
        # fallback: synthesize 3 phases
        today = datetime.date.today()
        rows = []
        for i in range(3):
            s = today + datetime.timedelta(days=i * 14)
            f = s + datetime.timedelta(days=14)
            rows.append({"Task": f"Phase {i+1}", "Start": pd.to_datetime(s), "Finish": pd.to_datetime(f)})
        df = pd.DataFrame(rows)

    df = df.sort_values("Start")

    # Build timeline
        # --- Построение DataFrame как pandas.Timestamp (последовательно) ---
    df = pd.DataFrame(rows)
    if df.empty:
        # fallback: synthesize 3 phases
        today = datetime.date.today()
        rows = []
        for i in range(3):
            s = today + datetime.timedelta(days=i * 14)
            f = s + datetime.timedelta(days=14)
            rows.append({"Task": f"Phase {i+1}", "Start": pd.to_datetime(s), "Finish": pd.to_datetime(f)})
        df = pd.DataFrame(rows)

    # Приводим колонки к единообразным типам pandas.Timestamp
    df["Start"] = pd.to_datetime(df["Start"])
    df["Finish"] = pd.to_datetime(df["Finish"])
    df = df.sort_values("Start")

    # Build timeline
    fig = px.timeline(df, x_start="Start", x_end="Finish", y="Task", title="Project Timeline")
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(
        margin=dict(l=120, r=30, t=50, b=30),
        width=width,
        height=max(300, 60 * len(df) + 50),
        showlegend=False,
        font=dict(family="Arial")   # force an available font to reduce pango warnings
    )
    # формат оси времени (подписи)
    try:
        fig.update_xaxes(tickformat="%b %d, %Y", ticklabelmode="period")
    except Exception:
        # некоторые версии plotly могут игнорировать нестандартные параметры — игнорируем
        pass

    # Надёжный Today marker: используем shape с yref='paper' (0..1) чтобы не смешивать типы
    try:
        today_dt = pd.to_datetime(datetime.date.today()).to_pydatetime()
        fig.add_shape(
            type="line",
            x0=today_dt, x1=today_dt,
            y0=0, y1=1,
            xref="x", yref="paper",
            line=dict(color="red", dash="dash"),
        )
        fig.add_annotation(
            x=today_dt,
            y=1.01,
            xref="x",
            yref="paper",
            text="Today",
            showarrow=False,
            font=dict(color="red", size=10),
            align="center"
        )
    except Exception as e:
        logger.debug("Could not add Today marker to Gantt: %s", e)

    # Export PNG with kaleido (high-res). Ensure kaleido is installed (pip install -U kaleido)
    try:
        png = pio.to_image(fig, format="png", width=width, height=max(300, 60 * len(df) + 50), scale=2)
        return png
    except Exception as e:
        logger.exception("Gantt export failed: %s", e)
        return _placeholder_png_bytes("Gantt generation failed")
