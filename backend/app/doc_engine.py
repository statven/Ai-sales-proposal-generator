# backend/app/doc_engine.py  (или тот же путь, где находится ваш render_docx_from_template)
import re
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

# CHANGED: корректные импорты - убедитесь, что функции с такими именами
# действительно существуют в backend.app.services.visualization_service
from backend.app.services.visualization_service import (
    generate_component_diagram,
    generate_dataflow_diagram,
    generate_deployment_diagram,
    generate_gantt_image,
)

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# --- НАДЕЖНОЕ ФОРМАТИРОВАНИЕ ВАЛЮТЫ ---
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

# --------------------------------------

def _replace_in_paragraph(paragraph, mapping: Dict[str, str]) -> None:
    full_text = "".join(run.text for run in paragraph.runs)
    if not full_text:
        return

    new_text = full_text
    found_placeholders = False

    for k, v in mapping.items():
        placeholder = f"{{{{{k}}}}}"
        if placeholder in new_text:
            new_text = new_text.replace(placeholder, v or "")
            found_placeholders = True

    if found_placeholders and new_text != full_text:
        for run in paragraph.runs:
            run.text = ""
        paragraph.add_run(new_text)

def _replace_in_table(table: Table, mapping: Dict[str, str]) -> None:
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                try:
                    _replace_in_paragraph(para, mapping)
                except Exception:
                    logger.exception("Paragraph replacement in table cell failed; continuing.")

def _replace_in_header_footer(container, mapping: Dict[str, str]) -> None:
    if container is None:
        return
    for para in container.paragraphs:
        try:
            _replace_in_paragraph(para, mapping)
        except Exception:
            logger.exception("Header/footer paragraph replacement failed; continuing.")
    if hasattr(container, 'tables'):
        for table in container.tables:
            try:
                _replace_in_table(table, mapping)
            except Exception:
                logger.exception("Header/footer table replacement failed; continuing.")

def _find_and_replace_placeholder_with_image(doc: Document, placeholder: str, image_bytes: bytes, width_inches: float = 6.0) -> bool:
    # search paragraphs
    for p in doc.paragraphs:
        if placeholder in p.text:
            for run in list(p.runs):
                run.text = ""
            run = p.add_run()
            try:
                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
            except Exception:
                # fallback: if image is invalid, write placeholder text
                p.add_run("[Image could not be embedded]")
            p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            return True

    # search in tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if placeholder in p.text:
                        for run in list(p.runs):
                            run.text = ""
                        run = p.add_run()
                        try:
                            run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
                        except Exception:
                            p.add_run("[Image could not be embedded]")
                        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                        return True
    return False

def _find_table_by_headers(doc: Document, headers: List[str]) -> Optional[Table]:
    headers_lower = [h.lower() for h in headers]
    for table in doc.tables:
        if not table.rows:
            continue
        first_row = table.rows[0]
        cells_text = [c.text.strip().lower() for c in first_row.cells]
        if all(any(h in ct for ct in cells_text) for h in headers_lower):
            return table
    return None

def _append_deliverables(table: Table, deliverables: List[Dict[str, str]], max_rows: int = 200):
    for d in deliverables[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            cells_count = len(cells)

            title = d.get("title", "") or ""
            desc = d.get("description", "") or ""
            acc = d.get("acceptance_criteria", d.get("acceptance", "")) or ""

            if cells_count >= 3:
                cells[0].text = title
                cells[1].text = desc
                cells[2].text = acc
            elif cells_count == 2:
                cells[0].text = title
                cells[1].text = f"{desc} / {acc}".strip(" /")
            else:
                cells[0].text = f"{title} - {desc} - {acc}"
        except Exception:
            logger.exception("Failed adding deliverable row; writing fallback")
            try:
                row = table.add_row()
                row.cells[0].text = str(d)
            except Exception:
                logger.exception("Even fallback write failed for deliverable row")

def _append_timeline(table: Table, phases: List[Dict[str, str]], max_rows: int = 200):
    for p in phases[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            name = p.get("phase_name", "")
            duration = p.get("duration", p.get("duration_weeks", ""))
            tasks = p.get("tasks", "")
            if len(cells) >= 3:
                cells[0].text = str(name)
                cells[1].text = str(duration)
                cells[2].text = str(tasks)
            else:
                cells[0].text = f"{name} - {duration} - {tasks}"
        except Exception:
            logger.exception("Failed adding timeline row; writing fallback")
            try:
                row = table.add_row()
                row.cells[0].text = str(p)
            except Exception:
                logger.exception("Even fallback write failed for timeline row")

def _run_has_picture(run):
    try:
        for r in run._element.xpath('.//pic:pic', {'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture'}):
            return True
    except Exception:
        try:
            if run._element.xpath('.//w:drawing'):
                return True
        except Exception:
            pass
    return False

def _insert_image_with_caption(doc: Document, image_bytes: bytes, placeholder: str, caption_text: str = "", width_inches: float = 6.0):
    inserted = _find_and_replace_placeholder_with_image(doc, placeholder, image_bytes, width_inches=width_inches)
    if inserted:
        # find paragraph containing the picture and insert caption after it
        for idx, p in enumerate(doc.paragraphs):
            for r in p.runs:
                if _run_has_picture(r):
                    insert_index = idx + 1
                    if insert_index < len(doc.paragraphs):
                        cap_para = doc.paragraphs[insert_index]
                        if cap_para.text.strip():
                            cap_para = doc.add_paragraph()
                    else:
                        cap_para = doc.add_paragraph()
                    cap_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    run = cap_para.add_run(caption_text)
                    run.font.size = Pt(9)
                    run.italic = True
                    return True
    else:
        p = doc.add_paragraph()
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = p.add_run()
        try:
            run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
        except Exception:
            p.add_run("[Image could not be embedded]")
        if caption_text:
            cap_para = doc.add_paragraph(caption_text)
            cap_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            cap_run = cap_para.runs[0]
            cap_run.font.size = Pt(9)
            cap_run.italic = True
        return False

def render_docx_from_template(template_path: str, context: Dict[str, Any]) -> BytesIO:
    doc = Document(template_path)

    # 1. Prepare mapping
    mapping: Dict[str, str] = {}
    for k, v in context.items():
        if k in ("development_cost", "licenses_cost", "support_cost", "total_investment_cost"):
            mapping[k] = _format_currency(v)
        else:
            mapping[k] = "" if v is None else str(v)
    if "client_company_name" in mapping and "client_name" not in mapping:
        mapping["client_name"] = mapping["client_company_name"]
    if "provider_company_name" in mapping and "provider_name" not in mapping:
        mapping["provider_name"] = mapping["provider_company_name"]
    if "expected_completion_date" in mapping and "expected_date" not in mapping:
        mapping["expected_date"] = mapping["expected_completion_date"]

    # 2. Replace placeholders in body and tables
    for para in doc.paragraphs:
        _replace_in_paragraph(para, mapping)
    for table in doc.tables:
        _replace_in_table(table, mapping)

    # 3. Headers/footers
    for section in doc.sections:
        _replace_in_header_footer(section.first_page_header, mapping)
        _replace_in_header_footer(section.first_page_footer, mapping)
        _replace_in_header_footer(section.header, mapping)
        _replace_in_header_footer(section.footer, mapping)

    # 4. Deliverables
    deliverables_table = _find_table_by_headers(doc, ["Deliverable", "Description", "Acceptance"])
    if deliverables_table and context.get("deliverables_list"):
        _append_deliverables(deliverables_table, context["deliverables_list"])

    # 5. Timeline table
    timeline_table = _find_table_by_headers(doc, ["Phase", "Duration", "Key Tasks"])
    if timeline_table and context.get("phases_list"):
        _append_timeline(timeline_table, context["phases_list"])

    # 6. Diagrams generation and insertion
    try:
        # внутри render_docx_from_template (перед сохранением) — предполагаем, что mapping/context уже сформирован
        viz = context.get("visualization") or {}
        # If proposal may have keys top-level
        if not viz:
            # try fallback from top-level keys (backwards compatibility)
            viz = {
                "components": context.get("components") or context.get("deliverables_list") or [],
                "infrastructure": context.get("infrastructure") or [],
                "data_flows": context.get("data_flows") or [],
                "connections": context.get("connections") or [],
                "milestones": context.get("milestones") or context.get("phases_list") or []
            }

        # call safe generators
        try:
            comp_png = generate_component_diagram(viz)
        except Exception:
            comp_png = None

        try:
            df_png = generate_dataflow_diagram(viz)
        except Exception:
            df_png = None

        try:
            dep_png = generate_deployment_diagram(viz)
        except Exception:
            dep_png = None

        try:
            gantt_png = generate_gantt_image(viz)
        except Exception:
            gantt_png = None

        # insert into placeholders if found (using your _insert_image_with_caption helper)
        if comp_png:
            _insert_image_with_caption(doc, comp_png, "{{components_diagram}}", caption_text=f"Figure. System components for {mapping.get('client_company_name','')}", width_inches=6.5)
        if df_png:
            _insert_image_with_caption(doc, df_png, "{{dataflow_diagram}}", caption_text="Figure. Data flow diagram.", width_inches=6.5)
        if dep_png:
            _insert_image_with_caption(doc, dep_png, "{{deployment_diagram}}", caption_text="Figure. Deployment diagram.", width_inches=6.5)
        if gantt_png:
            _insert_image_with_caption(doc, gantt_png, "{{gantt_chart}}", caption_text="Figure. Project timeline.", width_inches=6.5)


    except Exception:
        logger.exception("Diagram generation/insert failed; continuing without diagrams.")

    # 7. Save and return
    out = BytesIO()
    doc.save(out)
    out.seek(0)
    return out
