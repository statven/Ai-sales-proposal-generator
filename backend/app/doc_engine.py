import re
import logging
from io import BytesIO
from typing import Dict, Any, List, Optional
from docx import Document
from docx.shared import Pt, Inches
from docx.table import Table
import locale

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# --- НАДЕЖНОЕ ФОРМАТИРОВАНИЕ ВАЛЮТЫ ---
try:
    # ru_RU.UTF-8 - для Linux/macOS
    locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8') 
except locale.Error:
    try:
        # Russian - для Windows
        locale.setlocale(locale.LC_ALL, 'Russian')
    except locale.Error:
        logger.warning("Could not set locale for Russian formatting. Using default string conversion.")

def _format_currency(value) -> str:
    if value is None or value == "":
        return ""
    try:
        amount = float(value)
        # Форматирование с локализованными разделителями
        formatted_value = locale.format_string("%.2f", amount, grouping=True)
        return f" {formatted_value}"
    except Exception:
        # Резервный вариант: формат "1 234,56"
        try:
            return f"{float(value):,.2f}".replace(',', 'TEMP_SEP').replace('.', ',').replace('TEMP_SEP', ' ')
        except ValueError:
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
    Добавляет строки с Deliverables.
    Ожидаемые ключи в элементах: "title", "description", "acceptance".
    """
    for d in deliverables[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            cells_count = len(cells)
            
            # Обеспечиваем, что при отсутствии ячеек 2 и 3 не будет IndexError
            if cells_count >= 3:
                cells[0].text = d.get("title", "")
                cells[1].text = d.get("description", "")
                cells[2].text = d.get("acceptance", "")
            elif cells_count == 2:
                 cells[0].text = d.get("title", "")
                 cells[1].text = f"{d.get('description','')} / {d.get('acceptance','')}"
            else: # cells_count == 1
                cells[0].text = f"{d.get('title','')} - {d.get('description','')} - {d.get('acceptance','')}"
                
        except Exception:
            logger.exception("Failed adding deliverable row; writing fallback")
            try:
                # Резервная запись сырого представления
                row = table.add_row()
                row.cells[0].text = str(d)
            except Exception:
                logger.exception("Even fallback write failed for deliverable row")

def _append_timeline(table: Table, phases: List[Dict[str, str]], max_rows: int = 200):
    """
    Добавляет строки с Phases.
    Ожидаемые ключи в элементах: "phase_name", "duration", "tasks".
    """
    for p in phases[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            cells_count = len(cells)

            # Обеспечиваем, что при отсутствии ячеек 2 и 3 не будет IndexError
            if cells_count >= 3:
                cells[0].text = p.get("phase_name", "")
                cells[1].text = str(p.get("duration", ""))
                cells[2].text = p.get("tasks", "")
            elif cells_count == 2:
                 cells[0].text = p.get("phase_name", "")
                 cells[1].text = f"Duration: {p.get('duration','')} / Tasks: {p.get('tasks','')}"
            else: # cells_count == 1
                cells[0].text = f"{p.get('phase_name','')} - {p.get('duration','')} - {p.get('tasks','')}"
                
        except Exception:
            logger.exception("Failed adding timeline row; writing fallback")
            try:
                row = table.add_row()
                row.cells[0].text = str(p)
            except Exception:
                logger.exception("Even fallback write failed for timeline row")

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

    # 6. Сохранение в BytesIO
    out = BytesIO()
    doc.save(out)
    out.seek(0) 
    return out