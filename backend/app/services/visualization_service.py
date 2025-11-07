# backend/app/services/visualization_service.py
"""
Visualization service — generates diagrams as PNG bytes.

Provides:
- generate_component_diagram(proposal) -> bytes
- generate_dataflow_diagram(proposal) -> bytes
- generate_deployment_diagram(proposal) -> bytes
- generate_gantt_image(proposal) -> bytes

This version is robust against string/json inputs, unsafe characters in labels,
and Graphviz label formatting issues.
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


# ----------------- helpers / normalizers -----------------

def _ensure_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, (tuple, set)):
        return list(x)
    if isinstance(x, str):
        s = x.strip()
        # пробуем распарсить JSON
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            # comma separated fallback
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
    # ensure doesn't start with digit (graphviz allows, but keep tidy)
    if re.match(r'^\d', s):
        s = "n" + s
    return s

def _sanitize_label(s: Optional[str], max_len: int = 200) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    # remove dangerous characters for graphviz simple labels
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
            normalized = {
                "id": str(c.get("id") or c.get("title") or f"comp_{i}"),
                "title": str(c.get("title") or c.get("id") or f"Component {i+1}"),
                "description": str(c.get("description") or ""),
                "type": str(c.get("type") or "service").lower(),
                "depends_on": c.get("depends_on") or c.get("depends") or [],
                "host": c.get("host") or c.get("node") or None
            }
            if isinstance(normalized["depends_on"], str):
                normalized["depends_on"] = [p.strip() for p in normalized["depends_on"].split(",") if p.strip()]
            out.append(normalized)
            continue
        if isinstance(c, str):
            # try parse JSON string
            try:
                parsed = json.loads(c)
                if isinstance(parsed, dict):
                    out.extend(_normalize_components([parsed]))
                    continue
            except Exception:
                pass
            out.append({
                "id": f"comp_{i}",
                "title": c,
                "description": "",
                "type": "service",
                "depends_on": [],
                "host": None
            })
            continue
        out.append({
            "id": f"comp_{i}",
            "title": str(c),
            "description": "",
            "type": "service",
            "depends_on": [],
            "host": None
        })
    return out



def _normalize_flows(raw) -> List[Dict[str, Any]]:
    items = _ensure_list(raw)
    out = []
    for f in items:
        if isinstance(f, dict):
            out.append({
                "from": str(f.get("from") or f.get("src") or f.get("source") or ""),
                "to": str(f.get("to") or f.get("dst") or f.get("target") or ""),
                "label": str(f.get("label") or f.get("type") or "")
            })
            continue
        if isinstance(f, str):
            s = f.strip()
            try:
                if "->" in s:
                    left, right = s.split("->", 1)
                    if ":" in right:
                        dst, lbl = right.split(":", 1)
                        out.append({"from": left.strip(), "to": dst.strip(), "label": lbl.strip()})
                    else:
                        out.append({"from": left.strip(), "to": right.strip(), "label": ""})
                    continue
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    out.append({
                        "from": str(parsed.get("from") or ""),
                        "to": str(parsed.get("to") or ""),
                        "label": str(parsed.get("label") or "")
                    })
                    continue
            except Exception:
                pass
            out.append({"from": s, "to": s, "label": ""})
            continue
        out.append({"from": str(f), "to": str(f), "label": ""})
    return out


def _normalize_infra(raw) -> List[Dict[str, Any]]:
    items = _ensure_list(raw)
    out = []
    for i, n in enumerate(items):
        if isinstance(n, dict):
            out.append({
                "node": str(n.get("node") or n.get("id") or f"infra_{i}"),
                "label": str(n.get("label") or n.get("name") or n.get("node") or ""),
                "type": str(n.get("type") or "environment")
            })
            continue
        if isinstance(n, str):
            out.append({"node": f"infra_{i}", "label": n, "type": "environment"})
            continue
        out.append({"node": f"infra_{i}", "label": str(n), "type": "environment"})
    return out

def _normalize_milestones(raw) -> List[Dict[str, Any]]:
    items = _ensure_list(raw)
    out = []
    for i, m in enumerate(items):
        if isinstance(m, dict):
            out.append({
                "name": str(m.get("name") or m.get("title") or f"Milestone {i+1}"),
                "start": m.get("start") or None,
                "end": m.get("end") or None,
                "duration_days": m.get("duration_days") or m.get("duration") or None,
                "percent_complete": float(m.get("percent_complete") or m.get("percent") or 0.0),
                "owner": str(m.get("owner") or m.get("responsible") or "")
            })
            continue
        if isinstance(m, str):
            try:
                parsed = json.loads(m)
                if isinstance(parsed, dict):
                    out.extend(_normalize_milestones([parsed]))
                    continue
            except Exception:
                pass
            parts = [p.strip() for p in m.split("|")]
            if len(parts) >= 3:
                pct = 0.0
                try:
                    pct = float(parts[4]) if len(parts) > 4 and parts[4] else 0.0
                except Exception:
                    pct = 0.0
                out.append({"name": parts[0], "start": parts[1], "end": parts[2], "duration_days": None, "percent_complete": pct, "owner": parts[3] if len(parts) > 3 else ""})
                continue
            out.append({"name": m, "start": None, "end": None, "duration_days": None, "percent_complete": 0.0, "owner": ""})
            continue
        out.append({"name": str(m), "start": None, "end": None, "duration_days": None, "percent_complete": 0.0, "owner": ""})
    return out



# ----------------- placeholder PNG -----------------

def _placeholder_png_bytes(text: str = "Diagram unavailable", width: int = 1200, height: int = 300) -> bytes:
    return b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82'


# ----------------- Diagram generators -----------------

def generate_component_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    try:
        comps = _normalize_components(proposal.get("components"))
        if not comps:
            for i, k in enumerate(list(proposal.keys())[:6]):
                comps.append({"id": k, "title": k, "description": str(proposal.get(k) or ""), "type": "service", "depends_on": []})

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", shape="box", style="rounded,filled", fontname="Arial", fontsize="10")
        dot.attr("edge", fontname="Arial", fontsize="9", color="#333333")

        colors = {"service": "#e8f4ff", "db": "#fff4e6", "ui": "#f3fff0", "external": "#f7f7f7", "default": "#ffffff"}

        for c in comps:
            cid = _safe_id(c.get("id"))
            title = _sanitize_label(c.get("title"))
            desc = _sanitize_label(c.get("description"))
            label = title if not desc else f"{title}\\n{desc}"
            fill = colors.get((c.get("type") or "default").lower(), colors["default"])
            dot.node(cid, label=label, fillcolor=fill)

        for c in comps:
            cid = _safe_id(c.get("id"))
            deps = c.get("depends_on") or []
            if isinstance(deps, str):
                deps = [p.strip() for p in deps.split(",") if p.strip()]
            for d in deps:
                if not d:
                    continue
                dot.edge(_safe_id(d), cid, arrowhead="vee")

        png = dot.pipe(format="png")
        if png:
            return png
    except Exception as e:
        logger.exception("Component diagram generation failed: %s", e)
    return _placeholder_png_bytes("Component diagram failed")


def generate_dataflow_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    try:
        comps = _normalize_components(proposal.get("components"))
        flows = _normalize_flows(proposal.get("data_flows"))

        # Build nodes map, canonical ids
        node_map = {}
        for c in comps:
            cid = _safe_id(c.get("id") or c.get("title"))
            node_map[cid] = c

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", fontname="Arial")

        # create nodes
        created = set()
        for cid, c in node_map.items():
            canonical = _safe_id(c.get("id") or c.get("title") or cid)
            if canonical in created:
                continue
            created.add(canonical)
            title = _sanitize_label(c.get("title") or canonical)
            desc = _sanitize_label(c.get("description") or "")
            label = title if not desc else f"{title}\\n{desc}"
            # choose shape: process(box), data store(cylinder), external(oval)
            t = (c.get("type") or "").lower()
            if t in ("db", "database", "datastore") or "db" in title.lower() or "store" in title.lower():
                shape = "cylinder"
            elif t in ("external", "actor") or "external" in title.lower() or "client" in title.lower():
                shape = "oval"
            else:
                shape = "box"
            dot.node(canonical, label=label, shape=shape, style="rounded,filled", fillcolor="#ffffff")

        # add flows
        for f in flows:
            src = _safe_id(f.get("from"))
            dst = _safe_id(f.get("to"))
            lbl = _sanitize_label(f.get("label"))
            if not src or not dst:
                continue
            dot.edge(src, dst, label=lbl if lbl else None, color="#2b7cff", fontsize="9")
        png = dot.pipe(format="png")
        if png:
            return png
    except Exception as e:
        logger.exception("Dataflow diagram generation failed: %s", e)
    return _placeholder_png_bytes("Dataflow diagram failed")


def generate_deployment_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    try:
        infra = _normalize_infra(proposal.get("infrastructure"))
        comps = _normalize_components(proposal.get("components"))
        connections = _ensure_list(proposal.get("connections") or [])

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", fontname="Arial")

        infra_map = {}
        for i, inf in enumerate(infra):
            node_key = str(inf.get("node") or f"infra_{i}")
            node_id = _safe_id(node_key)
            infra_map[node_id] = {"orig": inf, "label": _sanitize_label(inf.get("label") or node_key)}

        # create clusters for infra
        for node_id, meta in infra_map.items():
            lbl = meta["label"]
            with dot.subgraph(name=f"cluster_{node_id}") as c:
                c.attr(label=lbl)
                c.attr(style="rounded", color="#cfe3ff", fontsize="10")
                # nothing else here; components will be drawn after

        # add components and (optionally) attach dashed edge from infra->component
        for comp in comps:
            cid = _safe_id(comp.get("id"))
            title = _sanitize_label(comp.get("title"))
            desc = _sanitize_label(comp.get("description"))
            label = title if not desc else f"{title}\\n{desc}"
            dot.node(cid, label=label, shape="component", style="filled", fillcolor="#ffffff")
            host = comp.get("host")
            if host:
                hid = _safe_id(host)
                # if matching infra cluster id
                if hid in infra_map:
                    # draw dashed 'deployed' edge (use infra cluster node name)
                    # Note: to indicate cluster -> node, we reference the node id directly
                    dot.edge(hid, cid, style="dashed", label="deployed", color="#666666", fontsize="8")

        # connections between infra nodes (network)
        for conn in connections:
            try:
                left = _safe_id(conn.get("from") or conn.get("src") or "")
                right = _safe_id(conn.get("to") or conn.get("dst") or "")
                lbl = _sanitize_label(conn.get("label") or "")
                if left and right:
                    dot.edge(left, right, label=lbl if lbl else None, color="#2b7cff")
            except Exception:
                continue

        png = dot.pipe(format="png")
        if png:
            return png
    except Exception as e:
        logger.exception("Deployment diagram generation failed: %s", e)
    return _placeholder_png_bytes("Deployment diagram failed")


def generate_gantt_image(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    try:
        ms = _normalize_milestones(proposal.get("milestones") or proposal.get("phases_list") or [])
        rows = []
        today = datetime.date.today()

        for i, m in enumerate(ms):
            name = m.get("name") or f"Phase {i+1}"
            start_dt = _to_datetime(m.get("start"))
            end_dt = _to_datetime(m.get("end"))
            dur = None
            if m.get("duration_days") is not None:
                try:
                    dur = int(m.get("duration_days"))
                except Exception:
                    dur = None
            if dur is None and m.get("duration_weeks") is not None:
                try:
                    dur = int(m.get("duration_weeks")) * 7
                except Exception:
                    dur = None

            if start_dt is None and end_dt is None:
                start_dt = datetime.datetime.combine(today + datetime.timedelta(days=i * 14), datetime.time.min)
                end_dt = start_dt + datetime.timedelta(days=dur or 14)
            elif start_dt is None and end_dt is not None:
                end_dt = end_dt
                start_dt = end_dt - datetime.timedelta(days=dur or 14)
            elif start_dt is not None and end_dt is None:
                end_dt = start_dt + datetime.timedelta(days=dur or 14)

            if not isinstance(start_dt, datetime.datetime):
                start_dt = _to_datetime(start_dt)
            if not isinstance(end_dt, datetime.datetime):
                end_dt = _to_datetime(end_dt)
            if start_dt is None or end_dt is None:
                start_dt = datetime.datetime.combine(today + datetime.timedelta(days=i * 14), datetime.time.min)
                end_dt = start_dt + datetime.timedelta(days=14)

            pct = 0.0
            try:
                pct = float(m.get("percent_complete") or m.get("percent") or 0.0)
            except Exception:
                pct = 0.0

            rows.append({"Task": name, "Start": pd.to_datetime(start_dt), "Finish": pd.to_datetime(end_dt), "Percent": max(0.0, min(100.0, pct)), "Owner": m.get("owner", "")})

        if not rows:
            for i in range(3):
                s = datetime.datetime.combine(today + datetime.timedelta(days=i * 14), datetime.time.min)
                f = s + datetime.timedelta(days=14)
                rows.append({"Task": f"Phase {i+1}", "Start": pd.to_datetime(s), "Finish": pd.to_datetime(f), "Percent": 0.0, "Owner": ""})

        df = pd.DataFrame(rows).sort_values("Start")
        fig = px.timeline(df, x_start="Start", x_end="Finish", y="Task", title="Project Timeline")
        fig.update_yaxes(autorange="reversed")
        fig.update_traces(marker=dict(line=dict(width=0)))
        fig.update_layout(margin=dict(l=160, r=30, t=60, b=30), width=width, height=max(320, 60 * len(df) + 80), showlegend=False, font=dict(family="Arial"))
        # draw percent colouring
        for idx, row in df.reset_index(drop=True).iterrows():
            start = row["Start"].to_pydatetime()
            finish = row["Finish"].to_pydatetime()
            pct = float(row["Percent"])
            if pct < 50.0:
                r = 255
                g = int(255 * (pct / 50.0))
            else:
                g = 255
                r = int(255 * (1 - ((pct - 50.0) / 50.0)))
            color = f"rgba({r},{g},0,0.9)"
            fig.add_shape(type="rect", x0=start, x1=finish, y0=idx - 0.4, y1=idx + 0.4, xref="x", yref="y", fillcolor=color, line=dict(width=0))
            mid = start + (finish - start) / 2
            fig.add_annotation(x=mid, y=idx, xref="x", yref="y", text=f"{int(pct)}%", showarrow=False, font=dict(size=10, color="black"))
        # Today marker
        try:
            today_dt = pd.to_datetime(datetime.date.today()).to_pydatetime()
            fig.add_shape(type="line", x0=today_dt, x1=today_dt, y0=0, y1=1, xref="x", yref="paper", line=dict(color="red", dash="dash"))
            fig.add_annotation(x=today_dt, y=1.02, xref="x", yref="paper", text="Today", showarrow=False, font=dict(color="red", size=10))
        except Exception as e:
            logger.debug("Could not add Today marker: %s", e)
        fig.add_annotation(xref="paper", yref="paper", x=0.99, y=0.01, text="Legend: color = % complete", showarrow=False, align="right", font=dict(size=9))
        png = pio.to_image(fig, format="png", width=width, height=max(320, 60 * len(df) + 80), scale=2)
        if png:
            return png
    except Exception as e:
        logger.exception("Gantt generation failed: %s", e)
    return _placeholder_png_bytes("Gantt generation failed")
