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
        # try parse JSON
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            # comma-separated (but ignore "->" flows)
            if "," in s and "->" not in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s]
    return [x]


def _safe_id(s: Optional[str]) -> str:
    if s is None:
        s = "n"
    s = str(s)
    # replace non-alnum with underscore
    s = re.sub(r'[^0-9A-Za-z_]', '_', s)
    if not s:
        s = "n"
    return s


def _sanitize_label(s: Optional[str], max_len: int = 200) -> str:
    if s is None:
        return ""
    s = str(s).replace("\r", " ").replace("\n", " ").strip()
    # remove characters that break labels or record syntax
    for ch in ['{', '}', '|', '<', '>', '"', '\\', '\t']:
        s = s.replace(ch, ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > max_len:
        s = s[:max_len - 3] + "..."
    return s


def _normalize_components(raw) -> List[Dict[str, Any]]:
    items = _ensure_list(raw)
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(items):
        if isinstance(c, dict):
            depends = c.get("depends_on") or c.get("depends") or c.get("dependsOn") or []
            if isinstance(depends, str):
                depends = [p.strip() for p in depends.split(",") if p.strip()]
            normalized = {
                "id": str(c.get("id") or c.get("title") or f"comp_{i}"),
                "title": str(c.get("title") or c.get("id") or f"Component {i+1}"),
                "description": str(c.get("description") or c.get("desc") or ""),
                "type": str((c.get("type") or "service")).lower(),
                "depends_on": depends,
                "host": c.get("host") or c.get("node") or None
            }
            out.append(normalized)
            continue
        if isinstance(c, str):
            # try JSON decode string element
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
    out: List[Dict[str, Any]] = []
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
            # pattern: A->B: label
            if "->" in s:
                left, right = s.split("->", 1)
                if ":" in right:
                    dst, lbl = right.split(":", 1)
                    out.append({"from": left.strip(), "to": dst.strip(), "label": lbl.strip()})
                else:
                    out.append({"from": left.strip(), "to": right.strip(), "label": ""})
                continue
            # try json
            try:
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
    out: List[Dict[str, Any]] = []
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
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(items):
        if isinstance(m, dict):
            out.append({
                "name": str(m.get("name") or m.get("title") or f"Milestone {i+1}"),
                "start": m.get("start") or m.get("from") or m.get("begin") or None,
                "end": m.get("end") or m.get("to") or m.get("finish") or None,
                "duration_days": m.get("duration_days") or m.get("duration") or m.get("duration_weeks"),
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
                out.append({
                    "name": parts[0],
                    "start": parts[1],
                    "end": parts[2],
                    "duration_days": None,
                    "percent_complete": pct,
                    "owner": parts[3] if len(parts) > 3 else ""
                })
                continue
            out.append({"name": m, "start": None, "end": None, "duration_days": None, "percent_complete": 0.0, "owner": ""})
            continue
        out.append({"name": str(m), "start": None, "end": None, "duration_days": None, "percent_complete": 0.0, "owner": ""})
    return out


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
        fmts = ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S")
        for f in fmts:
            try:
                return datetime.datetime.strptime(s, f)
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


# ----------------- placeholder PNG -----------------

def _placeholder_png_bytes(text: str = "Diagram unavailable", width: int = 1200, height: int = 300) -> bytes:
    return b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82'


# ----------------- Diagram generators -----------------

def generate_component_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    try:
        comps_raw = proposal.get("components") if isinstance(proposal, dict) else None
        comps = _normalize_components(comps_raw)

        if not comps:
            # synthesize small set from top-level keys
            for i, k in enumerate(list(proposal.keys())[:6]):
                comps.append({"id": k, "title": k, "description": str(proposal.get(k) or ""), "type": "service", "depends_on": []})

        # build safe id mapping and ensure uniqueness
        used_ids = set()
        comp_map = {}
        for c in comps:
            orig = str(c.get("id") or c.get("title") or "")
            base = _safe_id(orig)
            sid = base
            suffix = 1
            while sid in used_ids:
                sid = f"{base}_{suffix}"
                suffix += 1
            used_ids.add(sid)
            comp_map[orig] = sid
            # also map by title
            comp_map[str(c.get("title") or "")] = sid

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", shape="box", style="rounded,filled", fontname="Arial", fontsize="10")
        dot.attr("edge", fontname="Arial", fontsize="9", color="#333333")

        colors = {"service": "#e8f4ff", "db": "#fff4e6", "ui": "#f3fff0", "external": "#f7f7f7", "default": "#ffffff"}

        # create nodes (use safe ids)
        for c in comps:
            orig = str(c.get("id") or c.get("title") or "")
            sid = comp_map.get(orig) or _safe_id(orig)
            title = _sanitize_label(c.get("title") or orig, max_len=80)
            desc = _sanitize_label(c.get("description") or "", max_len=140)
            label = title if not desc else f"{title}\\n{desc}"
            t = (c.get("type") or "default").lower()
            fill = colors.get(t, colors["default"])
            dot.node(sid, label=label, fillcolor=fill)

        # add edges (resolve depends_on)
        for c in comps:
            dest_orig = str(c.get("id") or c.get("title") or "")
            dest_sid = comp_map.get(dest_orig) or _safe_id(dest_orig)
            deps = c.get("depends_on") or []
            if isinstance(deps, str):
                deps = [d.strip() for d in deps.split(",") if d.strip()]
            for d in deps:
                if not d:
                    continue
                # try direct mapping, fall back to title mapping
                src_sid = comp_map.get(str(d))
                if not src_sid:
                    # try find by title case-insensitive
                    for cc in comps:
                        if str(cc.get("title") or "").strip().lower() == str(d).strip().lower():
                            src_sid = comp_map.get(str(cc.get("id") or cc.get("title")))
                            break
                if not src_sid:
                    # create a minimal node for unknown id
                    src_sid = _safe_id(d)
                    if src_sid not in used_ids:
                        used_ids.add(src_sid)
                        dot.node(src_sid, label=_sanitize_label(str(d), max_len=80), fillcolor="#f7f7f7")
                dot.edge(src_sid, dest_sid, arrowhead="vee", color="#2b7cff")

        try:
            png = dot.pipe(format="png")
            if png:
                return png
        except Exception as e:
            logger.exception("Graphviz pipe failed for component diagram: %s", e)
    except Exception as e:
        logger.exception("Component diagram generation failed: %s", e)

    return _placeholder_png_bytes("Component diagram failed")


def generate_dataflow_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    try:
        comps = _normalize_components(proposal.get("components") if isinstance(proposal, dict) else None)
        flows = _normalize_flows(proposal.get("data_flows") if isinstance(proposal, dict) else None)

        # Node registry
        nodes = {}
        used_ids = set()

        # create nodes from components
        for c in comps:
            orig = str(c.get("id") or c.get("title") or "")
            sid = _safe_id(orig)
            # ensure unique
            sbase = sid
            suffix = 1
            while sid in used_ids:
                sid = f"{sbase}_{suffix}"
                suffix += 1
            used_ids.add(sid)
            nodes[sid] = {"title": _sanitize_label(c.get("title") or orig, max_len=120), "type": c.get("type", "service"), "desc": _sanitize_label(c.get("description") or "")}

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", fontname="Arial")

        # create nodes
        for nid, meta in nodes.items():
            label = meta["title"]
            if meta["desc"]:
                label = f"{label}\\n{meta['desc']}"
            # choose shape by detection
            t = (meta.get("type") or "").lower()
            if "db" in t or "store" in label.lower():
                shape = "cylinder"
            elif "external" in t or "actor" in t or "client" in label.lower():
                shape = "oval"
            elif "ui" in t or "frontend" in label.lower():
                shape = "component"
            else:
                shape = "box"
            dot.node(nid, label=label, shape=shape, style="rounded,filled", fillcolor="#ffffff")

        # ensure helper: resolve a name to an existing node id by title or id
        def resolve_node(name: str) -> Optional[str]:
            if not name:
                return None
            sid = _safe_id(name)
            if sid in nodes:
                return sid
            # try matching by title (case-insensitive)
            for nid, meta in nodes.items():
                if meta["title"].strip().lower() == str(name).strip().lower():
                    return nid
            # not found
            return None

        # add flows (create nodes if referenced but not present)
        for f in flows:
            src_raw = f.get("from") or ""
            dst_raw = f.get("to") or ""
            lbl = _sanitize_label(f.get("label") or "", max_len=80)

            src = resolve_node(src_raw) or _safe_id(src_raw)
            dst = resolve_node(dst_raw) or _safe_id(dst_raw)

            # create node if missing
            if src not in nodes:
                nodes[src] = {"title": _sanitize_label(src_raw), "type": "process", "desc": ""}
                dot.node(src, label=_sanitize_label(src_raw), shape="box", style="rounded,filled", fillcolor="#ffffff")
            if dst not in nodes:
                nodes[dst] = {"title": _sanitize_label(dst_raw), "type": "process", "desc": ""}
                dot.node(dst, label=_sanitize_label(dst_raw), shape="box", style="rounded,filled", fillcolor="#ffffff")

            if lbl:
                dot.edge(src, dst, label=lbl, fontsize="9", color="#2b7cff", fontname="Arial")
            else:
                dot.edge(src, dst, color="#2b7cff")

        try:
            png = dot.pipe(format="png")
            if png:
                return png
        except Exception as e:
            logger.exception("Graphviz pipe failed for dataflow diagram: %s", e)
    except Exception as e:
        logger.exception("Dataflow diagram generation failed: %s", e)

    return _placeholder_png_bytes("Dataflow diagram failed")


def generate_deployment_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    """
    Deployment diagram with infra clusters and components deployed into them.
    """
    try:
        infra = _normalize_infra(proposal.get("infrastructure") if isinstance(proposal, dict) else None)
        comps = _normalize_components(proposal.get("components") if isinstance(proposal, dict) else None)
        connections = _ensure_list(proposal.get("connections") or proposal.get("net") or [])

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", fontname="Arial")

        infra_map = {}  # host_safe_id -> original infra dict + label
        used_ids = set()

        # create infra clusters and representative infra nodes
        for i, inf in enumerate(infra):
            orig_node = str(inf.get("node") or f"infra_{i}")
            node_safe = _safe_id(orig_node)
            base = node_safe
            suffix = 1
            while node_safe in used_ids:
                node_safe = f"{base}_{suffix}"
                suffix += 1
            used_ids.add(node_safe)
            infra_label = _sanitize_label(inf.get("label") or orig_node, max_len=120)
            infra_map[node_safe] = {"orig": inf, "label": infra_label}
            # create a cluster and an infra node inside it
            with dot.subgraph(name=f"cluster_{node_safe}") as c:
                c.attr(label=infra_label, style="rounded", color="#cfe3ff", fontsize="10")
                infra_node_id = f"infra_node_{node_safe}"
                c.node(infra_node_id, label=infra_label, shape="oval", style="filled", fillcolor="#e6f0ff", fontsize="10")
                # we keep infra_node_id placeholder for edges

        # helper: find infra cluster id by host string
        def find_infra_host(host_value: str) -> Optional[str]:
            if not host_value:
                return None
            maybe = _safe_id(host_value)
            if maybe in infra_map:
                return maybe
            # try match by label
            for k, v in infra_map.items():
                if v["label"].strip().lower() == str(host_value).strip().lower():
                    return k
            return None

        # add components, placing them inside their host cluster if possible
        comp_nodes_created = set()
        for c in comps:
            orig = str(c.get("id") or c.get("title") or "")
            comp_safe = _safe_id(orig)
            base = comp_safe
            suffix = 1
            while comp_safe in used_ids:
                comp_safe = f"{base}_{suffix}"
                suffix += 1
            used_ids.add(comp_safe)

            title = _sanitize_label(c.get("title") or orig, max_len=140)
            desc = _sanitize_label(c.get("description") or "", max_len=140)
            label = title if not desc else f"{title}\\n{desc}"
            host = c.get("host") or c.get("node") or None
            host_id = find_infra_host(host) if host else None

            if host_id:
                # create node inside cluster
                with dot.subgraph(name=f"cluster_{host_id}") as sc:
                    sc.node(comp_safe, label=label, shape="box", style="filled", fillcolor="#ffffff", fontsize="9")
            else:
                dot.node(comp_safe, label=label, shape="box", style="filled", fillcolor="#ffffff", fontsize="9")

            comp_nodes_created.add(comp_safe)
            # if host exists, draw dashed 'deployed' edge between infra_node and comp
            if host_id:
                left = f"infra_node_{host_id}"
                dot.edge(left, comp_safe, style="dashed", label="deployed", color="#666666", fontsize="8")

        # add network / infra connections
        for conn in connections:
            try:
                if isinstance(conn, dict):
                    left_raw = conn.get("from") or conn.get("src") or ""
                    right_raw = conn.get("to") or conn.get("dst") or ""
                    lbl = _sanitize_label(conn.get("label") or "", max_len=80)
                elif isinstance(conn, str):
                    s = conn
                    if "->" in s:
                        a, b = s.split("->", 1)
                        if ":" in b:
                            to_part, lbl = b.split(":", 1)
                            left_raw = a.strip()
                            right_raw = to_part.strip()
                            lbl = _sanitize_label(lbl)
                        else:
                            left_raw = a.strip()
                            right_raw = b.strip()
                            lbl = ""
                    else:
                        continue
                else:
                    continue
                left_id = find_infra_host(left_raw) or _safe_id(left_raw)
                right_id = find_infra_host(right_raw) or _safe_id(right_raw)
                left_node = f"infra_node_{left_id}"
                right_node = f"infra_node_{right_id}"
                if lbl:
                    dot.edge(left_node, right_node, label=lbl, color="#2b7cff")
                else:
                    dot.edge(left_node, right_node, color="#2b7cff")
            except Exception:
                continue

        try:
            png = dot.pipe(format="png")
            if png:
                return png
        except Exception as e:
            logger.exception("Graphviz pipe failed for deployment diagram: %s", e)
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

            rows.append({
                "Task": name,
                "Start": pd.to_datetime(start_dt),
                "Finish": pd.to_datetime(end_dt),
                "Percent": max(0.0, min(100.0, pct)),
                "Owner": m.get("owner", "")
            })

        if not rows:
            for i in range(3):
                s = datetime.datetime.combine(today + datetime.timedelta(days=i * 14), datetime.time.min)
                f = s + datetime.timedelta(days=14)
                rows.append({"Task": f"Phase {i+1}", "Start": pd.to_datetime(s), "Finish": pd.to_datetime(f), "Percent": 0.0, "Owner": ""})

        df = pd.DataFrame(rows).sort_values("Start")
        df["Start"] = pd.to_datetime(df["Start"])
        df["Finish"] = pd.to_datetime(df["Finish"])

        fig = px.timeline(df, x_start="Start", x_end="Finish", y="Task", title="Project Timeline")
        fig.update_yaxes(autorange="reversed")
        fig.update_traces(marker=dict(line=dict(width=0)))
        fig.update_layout(
            margin=dict(l=160, r=30, t=60, b=30),
            width=width,
            height=max(320, 60 * len(df) + 80),
            showlegend=False,
            font=dict(family="Arial")
        )

        # colored bars by percent: use shapes to avoid modifying trace internals
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
            fig.add_shape(type="rect",
                          x0=start, x1=finish,
                          y0=idx - 0.4, y1=idx + 0.4,
                          xref="x", yref="y",
                          fillcolor=color, line=dict(width=0))
            mid = start + (finish - start) / 2
            fig.add_annotation(x=mid, y=idx, xref="x", yref="y",
                               text=f"{int(pct)}%", showarrow=False, font=dict(size=10, color="black"))

        try:
            today_dt = pd.to_datetime(datetime.date.today()).to_pydatetime()
            fig.add_shape(type="line", x0=today_dt, x1=today_dt, y0=0, y1=1, xref="x", yref="paper",
                          line=dict(color="red", dash="dash"))
            fig.add_annotation(x=today_dt, y=1.02, xref="x", yref="paper", text="Today", showarrow=False,
                               font=dict(color="red", size=10))
        except Exception as e:
            logger.debug("Could not add Today marker: %s", e)

        fig.add_annotation(xref="paper", yref="paper", x=0.99, y=0.01,
                           text="Legend: color = % complete", showarrow=False, align="right", font=dict(size=9))

        try:
            png = pio.to_image(fig, format="png", width=width, height=max(320, 60 * len(df) + 80), scale=2)
            if png:
                return png
        except Exception as e:
            logger.exception("Gantt export failed: %s", e)
    except Exception as e:
        logger.exception("Gantt generation failed: %s", e)

    return _placeholder_png_bytes("Gantt generation failed")
