# backend/app/services/visualization_service.py
"""
Visualization service — generates diagrams as PNG bytes.

Provides:
- generate_component_diagram(proposal) -> bytes
- generate_dataflow_diagram(proposal) -> bytes
- generate_deployment_diagram(proposal) -> bytes
- generate_gantt_image(proposal) -> bytes

Input `proposal` is flexible: keys may be dicts/lists/strings/JSON-encoded strings.
Expected canonical keys:
- components: list[dict{id,title,description,type,depends_on,host}]
- data_flows: list[dict{from,to,label}] or list[str] "a->b: label"
- infrastructure: list[dict{node,label,type}]
- milestones / phases_list: list[dict{name,start,end,duration_days,percent_complete,owner}]
"""
import io
import json
import logging
import datetime
from typing import Dict, Any, List, Optional
import re
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
    # if JSON string or JSON object in string
    if isinstance(x, str):
        s = x.strip()
        # try parse JSON list/object
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            # treat as comma-separated identifiers
            if "," in s and "->" not in s:
                return [p.strip() for p in s.split(",") if p.strip()]
            return [s]
    # anything else -> single element list
    return [x]


def _normalize_components(raw) -> List[Dict[str, Any]]:
    items = _ensure_list(raw)
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(items):
        if isinstance(c, dict):
            # ensure keys exist
            normalized = {
                "id": str(c.get("id") or c.get("title") or f"comp_{i}"),
                "title": str(c.get("title") or c.get("id") or f"Component {i+1}"),
                "description": str(c.get("description") or ""),
                "type": str(c.get("type") or "service").lower(),
                "depends_on": c.get("depends_on") or c.get("depends") or [],
                "host": c.get("host") or c.get("node") or None
            }
            # ensure depends_on is list of ids
            if isinstance(normalized["depends_on"], str):
                normalized["depends_on"] = [s.strip() for s in normalized["depends_on"].split(",") if s.strip()]
            out.append(normalized)
            continue
        if isinstance(c, str):
            # try parse JSON object encoded in string
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
        # fallback coercion
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
            # pattern: a->b: label
            try:
                if "->" in s:
                    left, right = s.split("->", 1)
                    if ":" in right:
                        dst, lbl = right.split(":", 1)
                        out.append({"from": left.strip(), "to": dst.strip(), "label": lbl.strip()})
                    else:
                        out.append({"from": left.strip(), "to": right.strip(), "label": ""})
                    continue
                # maybe json
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
            # fallback: treat as a node -> a (self flow)
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
            # accept many possible names
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
            s = m.strip()
            # try JSON
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    out.extend(_normalize_milestones([parsed]))
                    continue
            except Exception:
                pass
            # try pipe-separated: name|start|end|owner|percent
            parts = [p.strip() for p in s.split("|")]
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
            # fallback: name-only
            out.append({"name": s, "start": None, "end": None, "duration_days": None, "percent_complete": 0.0, "owner": ""})
            continue
        out.append({"name": str(m), "start": None, "end": None, "duration_days": None, "percent_complete": 0.0, "owner": ""})
    return out


def _to_datetime(obj) -> Optional[datetime.datetime]:
    """Convert ISO/date/datetime-like to datetime or return None."""
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
        # try several formats
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
def _sanitize_label(s: Optional[str], max_len: int = 200) -> str:
    """Make a safe label string for Graphviz: remove braces/pipe/angle quotes, shorten, remove newlines."""
    if s is None:
        return ""
    s = str(s)
    # replace newlines with space
    s = s.replace("\r", " ").replace("\n", " ").strip()
    # remove characters that break simple labels or record syntax
    for ch in ['{', '}', '|', '<', '>', '"', '\\', '\t']:
        s = s.replace(ch, ' ')
    # collapse multiple spaces
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > max_len:
        s = s[:max_len-3] + "..."
    return s

def _safe_id(s: str) -> str:
    """Create a safe node id: only letters, digits, underscore."""
    if s is None:
        return ""
    s = str(s)
    s = re.sub(r'[^0-9A-Za-z_]', '_', s)
    if not s:
        s = "n"
    return s


# ----------------- placeholder PNG -----------------

def _placeholder_png_bytes(text: str = "Diagram unavailable", width: int = 1200, height: int = 300) -> bytes:
    # Very small white PNG (static) to avoid PIL dependency; better than failing
    # If you prefer, replace with PIL-generated image.
    return b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0bIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82'


# ----------------- Diagram generators -----------------

def generate_component_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    """
    Component / simple UML-like diagram.
    Nodes: components with title + short description.
    Edges: depends_on relationships.
    Types: service, db, ui, external -> color-coding.
    """
    try:
        comps = _normalize_components(proposal.get("components"))
        if not comps:
            # try synthesize from keys
            for k in list(proposal.keys())[:6]:
                comps.append({"id": k, "title": k, "description": str(proposal.get(k) or ""), "type": "service", "depends_on": []})

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", shape="record", style="rounded,filled", fontname="Arial", fontsize="10")
        dot.attr("edge", fontname="Arial", fontsize="9", color="#333333")

        colors = {"service": "#e8f4ff", "db": "#fff4e6", "ui": "#f3fff0", "external": "#f7f7f7", "default": "#ffffff"}

        # add nodes
        for c in comps:
            cid = str(c.get("id"))
            title = str(c.get("title") or cid)
            desc = str(c.get("description") or "")
            if len(desc) > 120:
                desc = desc[:117] + "..."
            label = f'{{{title}|{desc}}}'
            t = (c.get("type") or "default").lower()
            fill = colors.get(t, colors["default"])
            dot.node(cid, label=label, fillcolor=fill)

        # add edges (depends_on)
        for c in comps:
            cid = str(c.get("id"))
            deps = c.get("depends_on") or []
            if isinstance(deps, str):
                deps = [s.strip() for s in deps.split(",") if s.strip()]
            for d in deps:
                if not d:
                    continue
                dot.edge(str(d), cid, arrowhead="vee")

        # add legend as separate small nodes
        # optional: keep simple legend node
        legend_html = "{{Legend|Service|DB|UI|External}}"
        # don't force legend if graphviz breaks; skip adding

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
    """
    Professional Data Flow Diagram (DFD) generator.

    - Processes: boxes (label = title + short desc)
    - Data stores: cylinders
    - External entities: ovals
    - Flows: labeled directed edges
    - Layout: left-to-right (rankdir=LR)
    """
    try:
        comps_list = _normalize_components(proposal.get("components") if isinstance(proposal, dict) else None)
        flows = _normalize_flows(proposal.get("data_flows") if isinstance(proposal, dict) else None)

        # Map ids -> component dict, also map titles to ids
        id_map = {}
        for c in comps_list:
            cid = _safe_id(c.get("id") or c.get("title") or f"comp_{len(id_map)}")
            id_map[cid] = c
            # also keep reverse lookup by title
            title_key = str(c.get("title") or "").strip()
            if title_key:
                id_map[_safe_id(title_key)] = c

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", fontname="Arial")

        # create nodes: decide shape by type detection
        created = set()
        for cid, c in list(id_map.items()):
            # We want unique node per canonical id (not duplicate title keys)
            canonical_id = _safe_id(c.get("id") or c.get("title") or cid)
            if canonical_id in created:
                continue
            created.add(canonical_id)

            title = _sanitize_label(c.get("title") or canonical_id, max_len=120)
            desc = _sanitize_label(c.get("description") or "", max_len=160)
            ttype = (c.get("type") or "").lower()

            # choose shape
            if ttype in ("db", "database", "datastore") or "db" in title.lower() or "store" in title.lower():
                shape = "cylinder"
            elif ttype in ("external", "actor") or "external" in title.lower() or "client" in title.lower():
                shape = "oval"
            elif ttype in ("ui", "frontend") or "ui" in title.lower():
                shape = "component"
            else:
                # processes are boxes
                shape = "box"

            label = title
            if desc:
                # multi-line label "Title\n(desc...)"
                label = f"{title}\\n{desc}"
            # ensure label safe (no braces/|)
            label = _sanitize_label(label, max_len=220)
            dot.node(canonical_id, label=label, shape=shape, style="rounded,filled", fillcolor="#ffffff")

        # Add flows. Resolve names to node ids if possible.
        for f in flows:
            src_raw = f.get("from") or ""
            dst_raw = f.get("to") or ""
            lbl = _sanitize_label(f.get("label") or "", max_len=80)

            src_id = _safe_id(src_raw)
            dst_id = _safe_id(dst_raw)
            # try to pick canonical id from actual components list if titles used
            if src_id not in created:
                # maybe src_raw equals a component title
                for c in comps_list:
                    if _sanitize_label(c.get("title") or "").lower() == src_raw.strip().lower():
                        src_id = _safe_id(c.get("id") or c.get("title"))
                        break
            if dst_id not in created:
                for c in comps_list:
                    if _sanitize_label(c.get("title") or "").lower() == dst_raw.strip().lower():
                        dst_id = _safe_id(c.get("id") or c.get("title"))
                        break

            if not src_id or not dst_id:
                continue
            # final safe ids
            src_id = _safe_id(src_id)
            dst_id = _safe_id(dst_id)

            # Add edge with label (if provided)
            if lbl:
                dot.edge(src_id, dst_id, label=lbl, fontsize="9", color="#2b7cff", fontname="Arial")
            else:
                dot.edge(src_id, dst_id, color="#2b7cff")

        try:
            png = dot.pipe(format="png")
            return png
        except Exception as e:
            logger.exception("Graphviz pipe failed for dataflow diagram: %s", e)
    except Exception as e:
        logger.exception("Dataflow diagram generation failed: %s", e)

    return _placeholder_png_bytes("Dataflow diagram failed")


def generate_deployment_diagram(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    """
    Professional Deployment Diagram.
    Style based on standard UML deployment diagrams (Visual Paradigm):
    - physical nodes / environments are clusters (boxes with label)
    - components/artifacts appear inside the cluster
    - dotted lines indicate 'deployed on' relationships
    - arrows between nodes show network / communication

    Input:
      proposal['infrastructure'] = list of infra nodes (node/id, label, type)
      proposal['components'] = list of components with optional 'host' field pointing to infra node id/label
      proposal['connections'] = list of dicts {from: infra_id/label, to: infra_id/label, label: 'https/api'}
    """
    try:
        infra = _normalize_infra(proposal.get("infrastructure") if isinstance(proposal, dict) else None)
        comps = _normalize_components(proposal.get("components") if isinstance(proposal, dict) else None)
        connections = _ensure_list(proposal.get("connections") or proposal.get("net") or [])

        dot = Digraph(format="png")
        dot.attr("graph", rankdir="LR", fontsize="10", fontname="Arial")
        dot.attr("node", fontname="Arial")

        # create infra clusters
        infra_map = {}
        for i, inf in enumerate(infra):
            node_key = str(inf.get("node") or f"infra_{i}")
            node_id = _safe_id(node_key)
            infra_label = _sanitize_label(inf.get("label") or node_key, max_len=120)
            infra_map[node_id] = {"orig": inf, "label": infra_label}
            with dot.subgraph(name=f"cluster_{node_id}") as c:
                c.attr(label=infra_label)
                c.attr(style="rounded", color="#cfe3ff", fontsize="10")
                # add a placeholder node representing the node box (invisible border)
                # components will be placed into cluster by giving them the same name
                # but Graphviz places nodes inside subgraph automatically
                # nothing to add here yet

        # add components, placing them under their host cluster (Graphviz will visually place nodes inside cluster)
        for comp in comps:
            cid = _safe_id(comp.get("id") or comp.get("title") or f"comp_{len(comp)}")
            title = _sanitize_label(comp.get("title") or cid, max_len=140)
            desc = _sanitize_label(comp.get("description") or "", max_len=160)
            label = title if not desc else f"{title}\\n{desc}"
            host = comp.get("host") or comp.get("node") or None
            host_id = None
            if host:
                # try to match host to infra
                hid = _safe_id(host)
                if hid in infra_map:
                    host_id = hid
                else:
                    # try match by label text
                    for k, v in infra_map.items():
                        if v['label'].lower() == str(host).strip().lower():
                            host_id = k
                            break

            # create node (Graphviz will render it inside the cluster if we used subgraph earlier)
            dot.node(cid, label=label, shape="box", style="filled", fillcolor="#ffffff", fontsize="9")
            # if host found, draw dashed line from infra box to component with label 'deployed'
            if host_id:
                dot.edge(f"infra_{host_id}", cid, style="dashed", label="deployed", color="#666666", fontsize="8")
            else:
                # no explicit host - leave as floating or create edge to a generic infra cluster
                pass

        # create network / infra connections
        for conn in _ensure_list(connections):
            try:
                if isinstance(conn, dict):
                    left = _safe_id(conn.get("from") or conn.get("src") or "")
                    right = _safe_id(conn.get("to") or conn.get("dst") or "")
                    lbl = _sanitize_label(conn.get("label") or "", max_len=80)
                elif isinstance(conn, str):
                    # parse simple "A->B: label"
                    s = conn
                    if "->" in s:
                        a, b = s.split("->", 1)
                        if ":" in b:
                            to_part, lbl = b.split(":", 1)
                            left = _safe_id(a)
                            right = _safe_id(to_part)
                            lbl = _sanitize_label(lbl)
                        else:
                            left = _safe_id(a)
                            right = _safe_id(b)
                            lbl = ""
                    else:
                        continue
                else:
                    continue
                if left and right:
                    if lbl:
                        dot.edge(left, right, label=lbl, color="#2b7cff")
                    else:
                        dot.edge(left, right, color="#2b7cff")
            except Exception:
                continue

        # If no infra clusters existed, add a top-level "Deployment" node grouping
        if not infra_map:
            # simply create a dotted box grouping all components (visual hint)
            # but Graphviz doesn't have a native group box outside of clusters; skip
            pass

        try:
            png = dot.pipe(format="png")
            return png
        except Exception as e:
            logger.exception("Graphviz pipe failed for deployment diagram: %s", e)
    except Exception as e:
        logger.exception("Deployment diagram generation failed: %s", e)

    return _placeholder_png_bytes("Deployment diagram failed")
def generate_gantt_image(proposal: Dict[str, Any], width: int = 1400) -> bytes:
    """
    Professional Gantt chart via Plotly.
    Expects proposal['milestones'] or proposal['phases_list'] as list of dicts:
      {name, start (ISO), end (ISO) or duration_days, percent_complete, owner}
    """
    try:
        ms = _normalize_milestones(proposal.get("milestones") or proposal.get("phases_list") or [])
        rows = []
        today = datetime.date.today()

        # build rows with Start/Finish datetimes
        for i, m in enumerate(ms):
            name = m.get("name") or f"Phase {i+1}"
            # parse start/end into datetimes
            start_dt = _to_datetime(m.get("start"))
            end_dt = _to_datetime(m.get("end"))
            dur = None
            if m.get("duration_days") is not None:
                try:
                    dur = int(m.get("duration_days"))
                except Exception:
                    dur = None
            # fallback, try duration_weeks
            if dur is None and m.get("duration_weeks") is not None:
                try:
                    dur = int(m.get("duration_weeks")) * 7
                except Exception:
                    dur = None

            if start_dt is None and end_dt is None:
                # place sequentially starting from today
                start_dt = datetime.datetime.combine(today + datetime.timedelta(days=i * 14), datetime.time.min)
                end_dt = start_dt + datetime.timedelta(days=dur or 14)
            elif start_dt is None and end_dt is not None:
                end_dt = end_dt
                start_dt = end_dt - datetime.timedelta(days=dur or 14)
            elif start_dt is not None and end_dt is None:
                end_dt = start_dt + datetime.timedelta(days=dur or 14)

            # ensure datetimes
            if not isinstance(start_dt, datetime.datetime):
                start_dt = _to_datetime(start_dt)
            if not isinstance(end_dt, datetime.datetime):
                end_dt = _to_datetime(end_dt)
            if start_dt is None or end_dt is None:
                # fallback short window
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
            # synthesize small default timeline
            for i in range(3):
                s = datetime.datetime.combine(today + datetime.timedelta(days=i * 14), datetime.time.min)
                f = s + datetime.timedelta(days=14)
                rows.append({"Task": f"Phase {i+1}", "Start": pd.to_datetime(s), "Finish": pd.to_datetime(f), "Percent": 0.0, "Owner": ""})

        df = pd.DataFrame(rows).sort_values("Start")
        df["Start"] = pd.to_datetime(df["Start"])
        df["Finish"] = pd.to_datetime(df["Finish"])

        # prepare figure
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
        # color bars by Percent manually: draw rect shapes per row
        for idx, row in df.reset_index(drop=True).iterrows():
            start = row["Start"].to_pydatetime()
            finish = row["Finish"].to_pydatetime()
            pct = float(row["Percent"])
            # color interpolation: red->yellow->green
            if pct < 50.0:
                r = 255
                g = int(255 * (pct / 50.0))
            else:
                g = 255
                r = int(255 * (1 - ((pct - 50.0) / 50.0)))
            color = f"rgba({r},{g},0,0.9)"
            # y as index position
            fig.add_shape(type="rect",
                          x0=start, x1=finish,
                          y0=idx - 0.4, y1=idx + 0.4,
                          xref="x", yref="y",
                          fillcolor=color, line=dict(width=0))
            # percent label centered
            mid = start + (finish - start) / 2
            fig.add_annotation(x=mid, y=idx, xref="x", yref="y",
                               text=f"{int(pct)}%", showarrow=False, font=dict(size=10, color="black"))

        # Today marker (paper coordinates to avoid axis type issues)
        try:
            today_dt = pd.to_datetime(datetime.date.today()).to_pydatetime()
            fig.add_shape(type="line", x0=today_dt, x1=today_dt, y0=0, y1=1, xref="x", yref="paper",
                          line=dict(color="red", dash="dash"))
            fig.add_annotation(x=today_dt, y=1.02, xref="x", yref="paper", text="Today", showarrow=False,
                               font=dict(color="red", size=10))
        except Exception as e:
            logger.debug("Could not add Today marker: %s", e)

        # small legend as annotation
        fig.add_annotation(xref="paper", yref="paper", x=0.99, y=0.01,
                           text="Legend: color = % complete", showarrow=False, align="right", font=dict(size=9))

        # export PNG via kaleido
        try:
            png = pio.to_image(fig, format="png", width=width, height=max(320, 60 * len(df) + 80), scale=2)
            if png:
                return png
        except Exception as e:
            logger.exception("Gantt export failed: %s", e)
    except Exception as e:
        logger.exception("Gantt generation failed: %s", e)

    return _placeholder_png_bytes("Gantt generation failed")
