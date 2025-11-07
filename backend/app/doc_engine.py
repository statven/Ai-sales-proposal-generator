import os
import re
import json
import shutil
import tempfile
import logging
import locale
import datetime
from io import BytesIO
from typing import Dict, Any, List, Optional
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt, Inches
from docx.table import Table
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from PIL import Image, ImageDraw, ImageFont


# --- Импорт безопасных генераторов диаграмм ---
from backend.app.services.visualization_service import (
    generate_uml_diagram,
    generate_gantt_image,
)

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# --- Валютное форматирование ---
try:
    locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_ALL, 'Russian_Russia')
    except locale.Error:
        logger.warning("Could not set locale for Russian formatting. Using default string conversion.")


def _format_currency(value) -> str:
    if value is None or value == "":
        return ""
    try:
        amount = float(value)
        formatted_value = locale.format_string("%.2f", amount, grouping=True)
        return f"{formatted_value}"
    except Exception:
        try:
            return f"{float(value):,.2f}".replace(',', 'TEMP').replace('.', ',').replace('TEMP', ' ')
        except Exception:
            return str(value)


# --- Вспомогательные функции замены текста ---
def _replace_in_paragraph(paragraph, mapping: Dict[str, str]) -> None:
    full_text = "".join(run.text for run in paragraph.runs)
    if not full_text:
        return

    new_text = full_text
    replaced = False

    for k, v in mapping.items():
        ph = f"{{{{{k}}}}}"
        if ph in new_text:
            new_text = new_text.replace(ph, v or "")
            replaced = True

    if replaced and new_text != full_text:
        for run in paragraph.runs:
            run.text = ""
        paragraph.add_run(new_text)


def _replace_in_table(table: Table, mapping: Dict[str, str]) -> None:
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                _replace_in_paragraph(para, mapping)


def _replace_in_header_footer(container, mapping: Dict[str, str]) -> None:
    if not container:
        return
    for para in container.paragraphs:
        _replace_in_paragraph(para, mapping)
    if hasattr(container, "tables"):
        for t in container.tables:
            _replace_in_table(t, mapping)


# --- Поиск и вставка изображений ---
def _find_and_replace_placeholder_with_image(doc: Document, placeholder: str, image_bytes: bytes, width_inches: float = 6.0) -> bool:
    """Находит {{placeholder}} и заменяет его изображением"""
    for p in doc.paragraphs:
        if placeholder in p.text:
            for r in list(p.runs):
                r.text = ""
            run = p.add_run()
            try:
                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
            except Exception:
                p.add_run("[Image could not be embedded]")
            p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            return True
    # искать в таблицах
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if placeholder in p.text:
                        for r in list(p.runs):
                            r.text = ""
                        run = p.add_run()
                        try:
                            run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
                        except Exception:
                            p.add_run("[Image could not be embedded]")
                        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                        return True
    return False


def _run_has_picture(run) -> bool:
    try:
        for _ in run._element.xpath('.//pic:pic', {'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture'}):
            return True
    except Exception:
        if run._element.xpath('.//w:drawing'):
            return True
    return False


def _insert_image_with_caption(doc: Document, image_bytes: bytes, placeholder: str, caption_text: str, width_inches: float = 6.5):
    """Вставляет картинку и подпись под ней"""
    inserted = _find_and_replace_placeholder_with_image(doc, placeholder, image_bytes, width_inches)
    if not inserted:
        # если плейсхолдер не найден — добавляем в конец
        p = doc.add_paragraph()
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = p.add_run()
        try:
            run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
        except Exception:
            p.add_run("[Image could not be embedded]")
    # подпись
    cap_para = doc.add_paragraph(caption_text)
    cap_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    run = cap_para.add_run(caption_text)
    run.font.size = Pt(9)
    run.italic = True


# --- Таблицы ---
def _find_table_by_headers(doc: Document, headers: List[str]) -> Optional[Table]:
    headers_lower = [h.lower() for h in headers]
    for table in doc.tables:
        if not table.rows:
            continue
        first_row = [c.text.strip().lower() for c in table.rows[0].cells]
        if all(any(h in ct for ct in first_row) for h in headers_lower):
            return table
    return None


def _append_deliverables(table: Table, deliverables: List[Dict[str, str]], max_rows: int = 200):
    for d in deliverables[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            title = d.get("title", "")
            desc = d.get("description", "")
            acc = d.get("acceptance_criteria", d.get("acceptance", ""))
            if len(cells) >= 3:
                cells[0].text = title
                cells[1].text = desc
                cells[2].text = acc
            else:
                cells[0].text = f"{title} / {desc} / {acc}"
        except Exception:
            logger.exception("Failed to add deliverable row")


def _append_timeline(table: Table, phases: List[Dict[str, str]], max_rows: int = 200):
    for p in phases[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            name = p.get("phase_name", "")
            dur = p.get("duration", p.get("duration_weeks", ""))
            tasks = p.get("tasks", "")
            if len(cells) >= 3:
                cells[0].text = str(name)
                cells[1].text = str(dur)
                cells[2].text = str(tasks)
            else:
                cells[0].text = f"{name} / {dur} / {tasks}"
        except Exception:
            logger.exception("Failed to add timeline row")


# --- Нормализация данных визуализации ---
def _normalize_visualization(context: Dict[str, Any]) -> Dict[str, Any]:
    """Создает корректный payload для визуализации"""
    raw = context.get("visualization")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                raw = parsed
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # fallback из context
    vis = {
        "components": raw.get("components") or context.get("components") or [],
        "data_flows": raw.get("data_flows") or context.get("data_flows") or [],
        "infrastructure": raw.get("infrastructure") or context.get("infrastructure") or [],
        "connections": raw.get("connections") or context.get("connections") or [],
        "milestones": raw.get("milestones") or context.get("phases_list") or context.get("milestones") or []
    }
    return vis


# --- Основная функция генерации ---
# вставь этот блок внутри backend/app/doc_engine.py, заменив текущую render_docx_from_template
def _placeholder_png_bytes(text: str = "UML unavailable", width: int = 800, height: int = 400) -> bytes:
    # Простая заглушка — белое изображение с текстом
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        # Попробуем системный шрифт
        font = ImageFont.truetype("arial.ttf", 20)
    except Exception:
        font = ImageFont.load_default()
    # center text
    w, h = draw.textsize(text, font=font)
    draw.text(((width-w)/2, (height-h)/2), text, fill=(50, 50, 50), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def render_docx_from_template(template_path: str, context: Dict[str, Any]) -> BytesIO:
    """
    Improved rendering with heavy diagnostics for visualization generation.
    - Logs full 'visualization' payload
    - Saves viz JSON and intermediate PNGs to temp dir for inspection
    - Synthesizes components/milestones from deliverables/phases as fallback
    """
    doc = Document(template_path)

    # 1. Prepare mapping (currency formatting preserved)
    mapping = {}
    for k, v in context.items():
        if k in ("development_cost", "licenses_cost", "support_cost", "total_investment_cost"):
            try:
                mapping[k] = _format_currency(v)
            except Exception:
                mapping[k] = str(v or "")
        else:
            mapping[k] = "" if v is None else str(v)

    # ensure aliases exist for template compatibility
    if "client_company_name" in mapping and "client_name" not in mapping:
        mapping["client_name"] = mapping["client_company_name"]
    if "provider_company_name" in mapping and "provider_name" not in mapping:
        mapping["provider_name"] = mapping["provider_company_name"]

    # 2. Replace placeholders in paragraphs and tables
    for para in doc.paragraphs:
        try:
            _replace_in_paragraph(para, mapping)
        except Exception:
            logger.exception("Paragraph replace failed.")

    for table in doc.tables:
        try:
            _replace_in_table(table, mapping)
        except Exception:
            logger.exception("Table replace failed.")

    # 3. Headers/footers
    for section in doc.sections:
        try:
            _replace_in_header_footer(section.first_page_header, mapping)
            _replace_in_header_footer(section.first_page_footer, mapping)
            _replace_in_header_footer(section.header, mapping)
            _replace_in_header_footer(section.footer, mapping)
        except Exception:
            logger.exception("Header/footer replacement failed.")

    # 4. Append deliverables & timeline if present (keeps old behavior)
    try:
        deliverables_table = _find_table_by_headers(doc, ["Deliverable", "Description", "Acceptance"])
        if deliverables_table and context.get("deliverables_list"):
            _append_deliverables(deliverables_table, context["deliverables_list"])
    except Exception:
        logger.exception("Appending deliverables failed.")

    try:
        timeline_table = _find_table_by_headers(doc, ["Phase", "Duration", "Key Tasks"])
        if timeline_table and context.get("phases_list"):
            _append_timeline(timeline_table, context["phases_list"])
    except Exception:
        logger.exception("Appending timeline failed.")

    # 5. Prepare visualization payload robustly and log it
    def _normalize_visualization_local(ctx: Dict[str, Any]) -> Dict[str, Any]:
        # Accept dict or JSON-string in ctx["visualization"]
        raw = ctx.get("visualization")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    raw = parsed
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}

        # fallback keys and permissive mapping for LLM variations
        def pick(d, *keys):
            for k in keys:
                if k in d and d[k]:
                    return d[k]
            return []

        vis = {}
        vis["components"] = pick(raw, "components", "nodes", "elements", "services") or ctx.get("components") or ctx.get("deliverables_list") or []
        vis["data_flows"] = pick(raw, "data_flows", "flows", "edges") or ctx.get("data_flows") or []
        vis["infrastructure"] = pick(raw, "infrastructure", "infra", "servers", "hosts") or ctx.get("infrastructure") or []
        vis["connections"] = pick(raw, "connections", "links", "network") or ctx.get("connections") or []
        vis["milestones"] = pick(raw, "milestones", "timeline", "phases") or ctx.get("phases_list") or ctx.get("milestones") or []
        return vis

    viz = _normalize_visualization_local(context)

    # Create diagnostics temp directory (unique per render)
    try:
        tmpdir = tempfile.mkdtemp(prefix="proposal_viz_")
        # Dump visualization payload for debugging
        try:
            with open(os.path.join(tmpdir, "viz_debug.json"), "w", encoding="utf-8") as fh:
                json.dump(viz, fh, ensure_ascii=False, indent=2, default=str)
            logger.info("Visualization payload dumped to %s", os.path.join(tmpdir, "viz_debug.json"))
        except Exception:
            logger.exception("Failed to write viz_debug.json")
    except Exception:
        tmpdir = None
        logger.exception("Failed to create temp dir for viz debugging")

    logger.debug("[DOC_ENGINE] Normalized visualization payload: %s", json.dumps(viz, ensure_ascii=False))

    # If visualization is empty, attempt to synthesize minimal components/milestones
    try:
        if not viz.get("components"):
            synth = []
            # prefer deliverables_list (already normalized by template code)
            for i, d in enumerate(context.get("deliverables_list", []) or []):
                synth.append({
                    "id": d.get("title")[:40] if isinstance(d.get("title"), str) else f"deliv_{i}",
                    "title": d.get("title") or f"Deliverable {i+1}",
                    "description": (d.get("description") or "")[:200],
                    "type": "service",
                    "depends_on": []
                })
            # if still empty, use top-level keys as nodes
            if not synth:
                for i, k in enumerate(list(context.keys())[:6]):
                    synth.append({"id": k, "title": k, "description": str(context.get(k) or "")[:140], "type": "service", "depends_on": []})
            viz["components"] = synth
            logger.info("Synthesized %d components for visualization fallback.", len(synth))

        if not viz.get("milestones"):
            synth_ms = []
            for i, p in enumerate(context.get("phases_list", []) or []):
                start = None
                end = None
                try:
                    dur_w = int(p.get("duration_weeks", 2))
                except Exception:
                    dur_w = 2
                # generate start date sequence from proposal_date or today
                try:
                    base = context.get("proposal_date")
                    if isinstance(base, str):
                        base_dt = None
                        try:
                            base_dt = datetime.date.fromisoformat(base)
                        except Exception:
                            base_dt = None
                        if base_dt:
                            base = base_dt
                    if not base:
                        base = datetime.date.today()
                except Exception:
                    base = datetime.date.today()
                s = base + datetime.timedelta(days=i * dur_w * 7)
                e = s + datetime.timedelta(days=dur_w * 7)
                synth_ms.append({"name": p.get("phase_name") or f"Phase {i+1}", "start": s.isoformat(), "end": e.isoformat(), "duration_days": dur_w * 7})
            if synth_ms:
                viz["milestones"] = synth_ms
                logger.info("Synthesized %d milestones for visualization fallback.", len(synth_ms))
    except Exception:
        logger.exception("Synthesis fallback failed.")

    # 6. Check Graphviz presence
    try:
        if not shutil.which("dot"):
            logger.error("Graphviz 'dot' binary not found in PATH — diagrams may fail. Install Graphviz and add 'dot' to PATH.")
    except Exception:
        logger.exception("Could not check Graphviz binary presence.")

    # 7. Generate diagrams and save intermediate PNGs for debugging
    uml_png = gantt_png = None
    
    
    try:
        uml_png = generate_uml_diagram(viz)
        if tmpdir and uml_png:
            try:
                with open(os.path.join(tmpdir, "uml.png"), "wb") as fh:
                    fh.write(uml_png)
            except Exception:
                logger.exception("Failed saving uml.png")
        if uml_png and len(uml_png) < 1500:
            logger.warning("Gantt image likely placeholder (empty input or export error). Size=%d bytes", len(uml_png))
    except Exception:
        logger.exception("Gantt generation raised exception.")
    try:
        gantt_png = generate_gantt_image(viz)
        if tmpdir and gantt_png:
            try:
                with open(os.path.join(tmpdir, "gantt.png"), "wb") as fh:
                    fh.write(gantt_png)
            except Exception:
                logger.exception("Failed saving gantt.png")
        if gantt_png and len(gantt_png) < 1500:
            logger.warning("Gantt image likely placeholder (empty input or export error). Size=%d bytes", len(gantt_png))
    except Exception:
        logger.exception("Gantt generation raised exception.")

    # 8. Insert images into doc (if present). If missing, insert explicit note
   
    try:
        if uml_png:
            _insert_image_with_caption(doc, uml_png, "{{uml_diagram}}", caption_text="Figure. UML Diagram.")
        else:
            _find_and_replace_placeholder_with_image(doc, "{{uml_diagram}}", _placeholder_png_bytes("Dataflow diagram unavailable"), width_inches=6.5)
    except Exception:
        logger.exception("Inserting UML Diagram failed.")

    try:
        if gantt_png:
            _insert_image_with_caption(doc, gantt_png, "{{gantt_chart}}", caption_text="Figure. Project Timeline.")
        else:
            _find_and_replace_placeholder_with_image(doc, "{{gantt_chart}}", _placeholder_png_bytes("Gantt chart unavailable"), width_inches=6.5)
    except Exception:
        logger.exception("Inserting gantt failed.")

    # 9. Save docx to BytesIO and also log location of debug dir (if any)
    out = BytesIO()
    try:
        doc.save(out)
        out.seek(0)
    except Exception:
        logger.exception("Failed saving docx to BytesIO.")
        raise

    if tmpdir:
        logger.info("Visualization debug files saved to: %s", tmpdir)
    return out

