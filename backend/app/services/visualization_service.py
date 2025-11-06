# backend/app/services/visualization_service.py
import datetime
from typing import Dict, Any, Optional
import logging

from graphviz import Digraph
import plotly.express as px
import plotly.io as pio
import pandas as pd

logger = logging.getLogger("uvicorn.error")

def _safe_date_parse(s: Optional[str]) -> Optional[datetime.date]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None

def generate_uml_image(proposal: Dict[str, Any]) -> bytes:
    dot = Digraph(format="png")
    dot.attr("node", shape="record", fontsize="10")

    comps = proposal.get("components")
    if not comps or not isinstance(comps, list):
        # fallback nodes from high-level keys
        comps = []
        for k in list(proposal.keys())[:8]:
            comps.append({"id": k, "title": k, "description": str(proposal.get(k, "")), "depends_on": []})

    for c in comps:
        cid = str(c.get("id") or c.get("title"))
        title = c.get("title") or cid
        desc = (c.get("description") or "")[:200].replace("\n", " ")
        label = f"{{{title}|{desc}}}"
        dot.node(cid, label)

    has_edges = False
    for c in comps:
        cid = str(c.get("id") or c.get("title"))
        deps = c.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        for dep in deps:
            has_edges = True
            dot.edge(cid, str(dep))

    if not has_edges and len(comps) > 1:
        for i in range(len(comps) - 1):
            a = str(comps[i].get("id") or comps[i].get("title"))
            b = str(comps[i + 1].get("id") or comps[i + 1].get("title"))
            dot.edge(a, b, style="dashed")

    png = dot.pipe(format="png")
    return png

def generate_gantt_image(proposal: Dict[str, Any]) -> bytes:
    milestones = proposal.get("milestones")
    rows = []
    if isinstance(milestones, list) and milestones:
        for m in milestones:
            name = m.get("name") or m.get("title") or "Phase"
            start = _safe_date_parse(m.get("start"))
            end = _safe_date_parse(m.get("end"))
            if start is None and end is None:
                continue
            if start is None:
                start = end - datetime.timedelta(days=int(m.get("duration_days", 14)))
            if end is None:
                end = start + datetime.timedelta(days=int(m.get("duration_days", 14)))
            rows.append({"Task": name, "Start": start, "Finish": end})

    if not rows:
        today = datetime.date.today()
        keys = list(proposal.keys())[:6]
        for i, k in enumerate(keys):
            s = today + datetime.timedelta(days=i * 7)
            f = s + datetime.timedelta(days=7)
            rows.append({"Task": k, "Start": s, "Finish": f})

    df = pd.DataFrame(rows)
    df["Start"] = pd.to_datetime(df["Start"])
    df["Finish"] = pd.to_datetime(df["Finish"])

    fig = px.timeline(df, x_start="Start", x_end="Finish", y="Task")
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), height=400)
    png = pio.to_image(fig, format="png")
    return png
