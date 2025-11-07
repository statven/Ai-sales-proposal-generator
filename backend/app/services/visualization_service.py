"""
Visualization service — generates UML component and Gantt diagrams as PNG bytes.

Provides:
- generate_uml_diagram(proposal) -> bytes
- generate_gantt_image(proposal) -> bytes
"""

import io
import json
import logging
import datetime
import re
from typing import Dict, Any, List, Optional

import pandas as pd
import plotly.express as px
import plotly.io as pio
from graphviz import Digraph

logger = logging.getLogger("uvicorn.error")

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------

def _ensure_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, (tuple, set)):
        return list(x)
    if isinstance(x, str):
        s = x.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            if "," in s and "->" not in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s]
    return [x]


def _safe_id(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = str(s)
    s = re.sub(r'[^0-9A-Za-z_]', '_', s)
    if not s:
        return "n"
    if re.match(r'^\d', s):
        s = "n" + s
    return s


def _sanitize_label(s: Optional[str], max_len: int = 200) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    for ch in ['{', '}', '|', '<', '>', '"', '\\', '\t']:
        s = s.replace(ch, ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > max_len:
        s = s[:max_len-3] + "..."
    return s


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
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.datetime.strptime(s, fmt)
            except Exception:
                continue
        try:
            return pd.to_datetime(s).to_pydatetime()
        except Exception:
            return None
    try:
        return pd.to_datetime(obj).to_pydatetime()
    except Exception:
        return None


def _normalize_components(raw) -> List[Dict[str, Any]]:
    items = _ensure_list(raw)
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(items):
        if isinstance(c, dict):
            out.append({
                "id": str(c.get("id") or c.get("title") or f"comp_{i}"),
                "title": str(c.get("title") or f"Component {i+1}"),
                "description": str(c.get("description") or ""),
                "type": str(c.get("type") or "service").lower(),
                "depends_on": c.get("depends_on") or [],
            })
        elif isinstance(c, str):
            out.append({
                "id": f"comp_{i}",
                "title": c,
                "description": "",
                "type": "service",
                "depends_on": [],
            })
    return out


def _normalize_milestones(raw) -> List[Dict[str, Any]]:
    items = _ensure_list(raw)
    out = []
    for i, m in enumerate(items):
        if isinstance(m, dict):
            out.append({
                "name": str(m.get("name") or m.get("title") or f"Milestone {i+1}"),
                "start": m.get("start"),
                "end": m.get("end"),
                "duration_days": m.get("duration_days") or m.get("duration"),
                "percent_complete": float(m.get("percent_complete") or 0.0),
                "owner": str(m.get("owner") or ""),
            })
        elif isinstance(m, str):
            out.append({"name": m, "start": None, "end": None, "duration_days": None, "percent_complete": 0.0, "owner": ""})
    return out


def _placeholder_png_bytes(text: str = "Diagram unavailable") -> bytes:
    return b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82'


# -------------------------------------------------------
# UML COMPONENT DIAGRAM
# -------------------------------------------------------

def generate_uml_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    """
    UML Component Diagram (safe version) — system architecture view
    """
    try:
        comps = _normalize_components(proposal.get("components"))
        logger.debug("generate_uml_diagram: %d components", len(comps))

        if not comps:
            return _placeholder_png_bytes("No components data")

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial", compound="true")
        dot.attr("node", fontname="Arial", fontsize="10", style="filled,rounded")
        dot.attr("edge", fontname="Arial", fontsize="9")

        type_styles = {
            "service": ("#e8f4ff", "component"),
            "db": ("#fff4e6", "cylinder"),
            "ui": ("#f3fff0", "box"),
            "external": ("#f7f7f7", "oval"),
        }

        def html_escape(text: str) -> str:
            """Безопасная замена спецсимволов для Graphviz HTML label"""
            return (text
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;")
                    .replace("'", "&#39;"))

        # --- Кластеризация по типу
        clusters = {}
        for c in comps:
            t = (c.get("type") or "service").lower()
            clusters.setdefault(t, []).append(c)

        for t, items in clusters.items():
            color, shape = type_styles.get(t, ("#ffffff", "box"))
            with dot.subgraph(name=f"cluster_{t}") as sub:
                sub.attr(label=t.upper(), color="#999999")
                for comp in items:
                    cid = _safe_id(comp.get("id"))
                    title = html_escape(_sanitize_label(comp.get("title")))
                    desc = html_escape(_sanitize_label(comp.get("description")))
                    # безопасный label без недопустимых HTML
                    if desc:
                        label = f"{title}\\n{desc}"
                    else:
                        label = title
                    sub.node(cid, label=label, shape=shape, fillcolor=color)

        # --- Связи между компонентами
        for c in comps:
            src = _safe_id(c.get("id"))
            deps = c.get("depends_on") or []
            if isinstance(deps, str):
                deps = [d.strip() for d in deps.split(",") if d.strip()]
            for d in deps:
                if not d:
                    continue
                dst = _safe_id(d)
                dot.edge(dst, src, arrowhead="vee", color="#2b7cff", label="depends on")

        dot.attr(label="UML Component Diagram", labelloc="t", fontsize="12")
        png = dot.pipe(format="png")
        if png:
            return png

    except Exception as e:
        logger.exception("UML diagram generation failed: %s", e)

    return _placeholder_png_bytes("UML diagram failed")


# -------------------------------------------------------
# GANTT CHART
# -------------------------------------------------------

def generate_gantt_image(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    """
    Professional Gantt chart showing project phases & completion percent
    """
    try:
        ms = _normalize_milestones(proposal.get("milestones") or proposal.get("phases_list") or [])
        rows = []
        today = datetime.date.today()

        for i, m in enumerate(ms):
            name = m.get("name") or f"Phase {i+1}"
            start = _to_datetime(m.get("start")) or datetime.datetime.combine(today + datetime.timedelta(days=i*14), datetime.time.min)
            end = _to_datetime(m.get("end")) or (start + datetime.timedelta(days=int(m.get("duration_days") or 14)))
            pct = float(m.get("percent_complete") or 0.0)
            rows.append({"Task": name, "Start": pd.to_datetime(start), "Finish": pd.to_datetime(end), "Percent": pct, "Owner": m.get("owner", "")})

        if not rows:
            return _placeholder_png_bytes("No milestones")

        df = pd.DataFrame(rows).sort_values("Start")
        fig = px.timeline(df, x_start="Start", x_end="Finish", y="Task", title="Project Timeline")
        fig.update_yaxes(autorange="reversed")
        fig.update_layout(
            margin=dict(l=160, r=30, t=60, b=30),
            width=width,
            height=max(320, 60 * len(df) + 80),
            showlegend=False,
            font=dict(family="Arial")
        )

        # Percent shading
        for idx, row in df.reset_index(drop=True).iterrows():
            color = "rgba(0,150,0,0.8)" if row["Percent"] >= 80 else "rgba(255,200,0,0.8)"
            fig.add_shape(type="rect", x0=row["Start"], x1=row["Finish"], y0=idx-0.4, y1=idx+0.4, fillcolor=color, line=dict(width=0))
            mid = row["Start"] + (row["Finish"] - row["Start"]) / 2
            fig.add_annotation(x=mid, y=idx, text=f"{int(row['Percent'])}%", showarrow=False, font=dict(size=10))

        today_dt = pd.to_datetime(datetime.date.today())
        fig.add_shape(type="line", x0=today_dt, x1=today_dt, y0=0, y1=1, xref="x", yref="paper", line=dict(color="red", dash="dash"))
        fig.add_annotation(x=today_dt, y=1.02, xref="x", yref="paper", text="Today", showarrow=False, font=dict(color="red", size=10))

        png = pio.to_image(fig, format="png", width=width, scale=2)
        if png:
            return png
    except Exception as e:
        logger.exception("Gantt chart generation failed: %s", e)
    return _placeholder_png_bytes("Gantt chart failed")
