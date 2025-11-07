import re
import json
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

# --- Импорт безопасных генераторов диаграмм ---
from backend.app.services.visualization_service import (
    generate_component_diagram,
    generate_dataflow_diagram,
    generate_deployment_diagram,
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
def render_docx_from_template(template_path: str, context: Dict[str, Any]) -> BytesIO:
    """Генерирует .docx предложение, включая все диаграммы"""
    doc = Document(template_path)

    # 1. Подготовка замен
    mapping = {k: (_format_currency(v) if "cost" in k else str(v or "")) for k, v in context.items()}

    # 2. Подстановка текста
    for p in doc.paragraphs:
        _replace_in_paragraph(p, mapping)
    for t in doc.tables:
        _replace_in_table(t, mapping)

    # 3. Headers/Footers
    for s in doc.sections:
        _replace_in_header_footer(s.first_page_header, mapping)
        _replace_in_header_footer(s.first_page_footer, mapping)
        _replace_in_header_footer(s.header, mapping)
        _replace_in_header_footer(s.footer, mapping)

    # 4. Таблицы Deliverables / Timeline
    del_table = _find_table_by_headers(doc, ["Deliverable", "Description", "Acceptance"])
    if del_table and context.get("deliverables_list"):
        _append_deliverables(del_table, context["deliverables_list"])

    ph_table = _find_table_by_headers(doc, ["Phase", "Duration", "Key Tasks"])
    if ph_table and context.get("phases_list"):
        _append_timeline(ph_table, context["phases_list"])

    # 5. Генерация диаграмм
    try:
        viz = _normalize_visualization(context)

        comp_png = generate_component_diagram(viz)
        dfd_png = generate_dataflow_diagram(viz)
        dep_png = generate_deployment_diagram(viz)
        gantt_png = generate_gantt_image(viz)

        if comp_png:
            _insert_image_with_caption(doc, comp_png, "{{components_diagram}}", f"Figure. System components for {mapping.get('client_company_name','')}")
        if dfd_png:
            _insert_image_with_caption(doc, dfd_png, "{{dataflow_diagram}}", "Figure. Data Flow Diagram.")
        if dep_png:
            _insert_image_with_caption(doc, dep_png, "{{deployment_diagram}}", "Figure. Deployment Diagram.")
        if gantt_png:
            _insert_image_with_caption(doc, gantt_png, "{{gantt_chart}}", "Figure. Project Timeline.")
    except Exception as e:
        logger.exception("Diagram generation failed: %s", e)

    # 6. Возврат .docx
    output = BytesIO()
    doc.save(output)
    output.seek(0)
    return output
