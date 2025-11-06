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
from backend.app.services.visualization_service import generate_uml_image, generate_gantt_image
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.shared import Pt
logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# --- НАДЕЖНОЕ ФОРМАТИРОВАНИЕ ВАЛЮТЫ ---
try:
    # ru_RU.UTF-8 - для Linux/macOS
    locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8') 
except locale.Error:
    try:
        # Russian - для Windows
        locale.setlocale(locale.LC_ALL, 'Russian_Russia')
    except locale.Error:
        logger.warning("Could not set locale for Russian formatting. Using default string conversion.")

def _format_currency(value) -> str:
    if value is None or value == "":
        return ""
    try:
        amount = float(value)
        formatted_value = locale.format_string("%.2f", amount, grouping=True)
        # REMOVE space after $ for consistent appearance: "$45 000,00"
        return f"{formatted_value}"
    except Exception:
        try:
            return f"{float(value):,.2f}".replace(',', 'TEMP').replace('.', ',').replace('TEMP', ' ')
        except Exception:
            return str(value)
# --------------------------------------

def _replace_in_paragraph(paragraph, mapping: Dict[str, str]) -> None:
    """
    Заменяет плейсхолдеры в абзаце. Объединяет runs для поиска плейсхолдеров, 
    разделенных форматированием, и затем записывает новый текст в один run.
    """
    full_text = "".join(run.text for run in paragraph.runs)
    if not full_text:
        return
    
    new_text = full_text
    found_placeholders = False
    
    # Ищем и заменяем плейсхолдеры
    for k, v in mapping.items():
        placeholder = f"{{{{{k}}}}}"
        if placeholder in new_text:
            new_text = new_text.replace(placeholder, v or "")
            found_placeholders = True
            
    if found_placeholders and new_text != full_text:
        # Если была замена: очищаем все runs и записываем новый текст
        for run in paragraph.runs:
            run.text = ""
        # Сохранение стиля абзаца
        paragraph.add_run(new_text)

def _replace_in_table(table: Table, mapping: Dict[str, str]) -> None:
    """Заменяет плейсхолдеры во всех ячейках таблицы."""
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                try:
                    _replace_in_paragraph(para, mapping)
                except Exception:
                    logger.exception("Paragraph replacement in table cell failed; continuing.")

def _replace_in_header_footer(container, mapping: Dict[str, str]) -> None:
    """Заменяет плейсхолдеры в колонтитулах и таблицах колонтитулов."""
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
    """
    Находит параграф, содержащий placeholder (например "{{uml_diagram}}"),
    очищает его и вставляет изображение. Если найдено - возвращает True, иначе False.
    """
    # поиск в параграфах
    for p in doc.paragraphs:
        if placeholder in p.text:
            # очистить runs
            for run in list(p.runs):
                run.text = ""
            # вставить картинку
            run = p.add_run()
            run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
            p.alignment = 1  # центровать
            return True

    # поиск в таблицах (ячейки)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if placeholder in p.text:
                        for run in list(p.runs):
                            run.text = ""
                        run = p.add_run()
                        run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
                        p.alignment = 1
                        return True
    return False


def _find_table_by_headers(doc: Document, headers: List[str]) -> Optional[Table]:
    """Находит первую таблицу, чья первая строка содержит все заданные заголовки."""
    headers_lower = [h.lower() for h in headers]
    for table in doc.tables:
        if not table.rows:
            continue
        first_row = table.rows[0]
        cells_text = [c.text.strip().lower() for c in first_row.cells]
        
        # Проверяем, что КАЖДЫЙ искомый заголовок содержится в тексте ячеек первой строки
        if all(any(h in ct for ct in cells_text) for h in headers_lower):
            return table
    return None

def _append_deliverables(table: Table, deliverables: List[Dict[str, str]], max_rows: int = 200):
    """
    Adds deliverables to the table. Accepts keys: "title", "description", "acceptance" OR "acceptance_criteria".
    """
    for d in deliverables[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            cells_count = len(cells)

            title = d.get("title", "") or ""
            desc = d.get("description", "") or ""
            # prefer acceptance_criteria (backend expected), but fallback to acceptance
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
    """
    Append phases to table. Expects phases items to contain:
      - phase_name (string, already numbered if needed)
      - duration (string)
      - tasks (string)
    """
    for p in phases[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            # normalize
            name = p.get("phase_name", "")
            duration = p.get("duration", "")
            tasks = p.get("tasks", "")
            if len(cells) >= 3:
                cells[0].text = str(name)
                cells[1].text = str(duration)
                cells[2].text = str(tasks)
            else:
                # fallback: put everything in first cell
                cells[0].text = f"{name} - {duration} - {tasks}"
        except Exception:
            logger.exception("Failed adding timeline row; writing fallback")
            try:
                row = table.add_row()
                row.cells[0].text = str(p)
            except Exception:
                logger.exception("Even fallback write failed for timeline row")

def _run_has_picture(run):
    # xpath with namespace as second positional arg (not keywords)
    try:
        for r in run._element.xpath('.//pic:pic', {'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture'}):
            return True
    except Exception:
        # fallback: check for drawing inline element
        try:
            if run._element.xpath('.//w:drawing'):
                return True
        except Exception:
            pass
    return False

def _insert_image_with_caption(doc: Document, image_bytes: bytes, placeholder: str, caption_text: str = "", width_inches: float = 6.0):
    inserted = _find_and_replace_placeholder_with_image(doc, placeholder, image_bytes, width_inches=width_inches)
    if inserted:
        # find the paragraph that contains the inserted image (search for runs that have a picture)
        for idx, p in enumerate(doc.paragraphs):
            for r in p.runs:
                if _run_has_picture(r):
                    # insert caption paragraph after this paragraph
                    cap_para = doc.paragraphs[idx+1] if idx+1 < len(doc.paragraphs) else doc.add_paragraph()
                    # if existing next paragraph is empty, use it; otherwise add new
                    if cap_para.text.strip():
                        cap_para = doc.add_paragraph()
                    cap_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                    run = cap_para.add_run(caption_text)
                    run.font.size = Pt(9)
                    run.italic = True
                    return True
    else:
        # append if placeholder was not found
        p = doc.add_paragraph()
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = p.add_run()
        run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
        if caption_text:
            cap_para = doc.add_paragraph(caption_text)
            cap_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            cap_run = cap_para.runs[0]
            cap_run.font.size = Pt(9)
            cap_run.italic = True
        return False

def render_docx_from_template(template_path: str, context: Dict[str, Any]) -> BytesIO:
    """
    Основная функция рендеринга документа.
    """
    doc = Document(template_path)

    # 1. Подготовка строкового маппинга
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
    # Keep backward compatibility keys
    if "expected_completion_date" in mapping and "expected_date" not in mapping:
        mapping["expected_date"] = mapping["expected_completion_date"]
    # 2. Замена плейсхолдеров в основном документе
    for para in doc.paragraphs:
        _replace_in_paragraph(para, mapping)
    for table in doc.tables:
        _replace_in_table(table, mapping)

    # 3. Замена плейсхолдеров в колонтитулах
    for section in doc.sections:
        _replace_in_header_footer(section.first_page_header, mapping)
        _replace_in_header_footer(section.first_page_footer, mapping)
        _replace_in_header_footer(section.header, mapping)
        _replace_in_header_footer(section.footer, mapping)

    # 4. Добавление Deliverables
    deliverables_table = _find_table_by_headers(doc, ["Deliverable", "Description", "Acceptance"])
    if deliverables_table and context.get("deliverables_list"):
        _append_deliverables(deliverables_table, context["deliverables_list"])

    # 5. Добавление Timeline
    timeline_table = _find_table_by_headers(doc, ["Phase", "Duration", "Key Tasks"])
    if timeline_table and context.get("phases_list"):
        _append_timeline(timeline_table, context["phases_list"])
    # 5.5 Generate and insert diagrams (UML & Gantt)
    try:
        # components: either come from context or synthesize from deliverables_list
        components = context.get("components")
        if components is None:
            components = []
            for i, d in enumerate(context.get("deliverables_list", []) or []):
                components.append({
                    "id": f"deliv_{i+1}",
                    "title": d.get("title",""),
                    "description": d.get("description",""),
                    "depends_on": []
                })



        milestones = context.get("milestones")
        if milestones is None:
            milestones = []
            # base date: prefer explicit deadline - duration sum, else proposal_date, else today
            base_date = None
            try:
                if context.get("deadline"):
                    # try to parse ISO date
                    base_date = datetime.date.fromisoformat(str(context["deadline"]))
                elif context.get("proposal_date"):
                    base_date = datetime.date.fromisoformat(str(context["proposal_date"]))
            except Exception:
                base_date = None
            if base_date is None:
                base_date = datetime.date.today()

            cursor = base_date
            for i, p in enumerate(context.get("phases_list", []) or []):
                name = p.get("phase_name") or (p.get("tasks") or "")[:60] or f"Phase {i+1}"
                # accept duration_weeks or duration_days or duration
                dur_days = None
                try:
                    if p.get("duration_weeks") is not None:
                        dur_days = int(p.get("duration_weeks")) * 7
                    elif p.get("duration") is not None:
                        dur_days = int(p.get("duration"))
                    elif p.get("duration_days") is not None:
                        dur_days = int(p.get("duration_days"))
                except Exception:
                    dur_days = None
                if dur_days is None:
                    dur_days = 7 * 2  # default 2 weeks

                start = cursor
                end = start + datetime.timedelta(days=dur_days)
                milestones.append({
                    "name": name,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "duration_days": dur_days
                })
                cursor = end + datetime.timedelta(days=1)


        # Generate images (these functions should return PNG bytes)
        uml_bytes = None
        gantt_bytes = None
        try:
            uml_bytes = generate_uml_image({"components": components})
        except Exception as e:
            logger.exception("UML generation failed: %s", e)

        try:
            gantt_bytes = generate_gantt_image({"milestones": milestones})
        except Exception as e:
            logger.exception("Gantt generation failed: %s", e)

        # Insert images into placeholders
        if uml_bytes:
            _insert_image_with_caption(doc, uml_bytes, "{{uml_diagram}}", caption_text=f"Figure. System architecture. Generated for {mapping.get('client_company_name','')}", width_inches=6.5)
        if gantt_bytes:
            _insert_image_with_caption(doc, gantt_bytes, "{{gantt_chart}}", caption_text=f"Figure. Project timeline (generated).", width_inches=6.5)

    except Exception:
        logger.exception("Diagram generation/insert failed; continuing without diagrams.")

    # 6. Сохранение в BytesIO
    out = BytesIO()
    doc.save(out)
    out.seek(0) 
    return out