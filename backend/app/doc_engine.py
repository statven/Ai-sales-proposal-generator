# backend/app/doc_engine.py
import re
import logging
from io import BytesIO
from typing import Dict, Any, List, Optional
from docx import Document
from docx.shared import Pt, Inches

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

def _format_currency(value) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return str(value)

def _replace_in_paragraph(paragraph, mapping: Dict[str, str]) -> None:
    """
    Replace placeholders in a paragraph by joining runs, doing replace, then recreating a single run.
    This handles placeholders split across runs.
    """
    full_text = "".join(run.text for run in paragraph.runs)
    if not full_text:
        return
    new_text = full_text
    for k, v in mapping.items():
        placeholder = f"{{{{{k}}}}}"
        if placeholder in new_text:
            new_text = new_text.replace(placeholder, v if v is not None else "")
    if new_text != full_text:
        # clear runs and write single run with new_text (keeps it simple and safe)
        for run in paragraph.runs:
            run.text = ""
        paragraph.add_run(new_text)

def _replace_in_table(table, mapping: Dict[str, str]) -> None:
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                try:
                    _replace_in_paragraph(para, mapping)
                except Exception:
                    logger.exception("Paragraph replacement in table cell failed; continuing.")

def _replace_in_header_footer(container, mapping: Dict[str, str]) -> None:
    """
    Replace placeholders in a header/footer container.
    container is a Header/Footer object (can be None).
    Replace in paragraphs and in any tables in the header/footer.
    """
    if container is None:
        return
    # paragraphs
    for para in container.paragraphs:
        try:
            _replace_in_paragraph(para, mapping)
        except Exception:
            logger.exception("Header/footer paragraph replacement failed; continuing.")
    # tables
    for table in container.tables:
        try:
            _replace_in_table(table, mapping)
        except Exception:
            logger.exception("Header/footer table replacement failed; continuing.")

def _find_table_by_headers(doc: Document, headers: List[str]):
    """
    Find first table whose first row (header row) contains all given headers (case-insensitive).
    Returns the table or None.
    """
    headers_lower = [h.lower() for h in headers]
    for table in doc.tables:
        if not table.rows:
            continue
        first_row = table.rows[0]
        cells_text = [c.text.strip().lower() for c in first_row.cells]
        if all(any(h in ct for ct in cells_text) for h in headers_lower):
            return table
    return None

def _append_deliverables(table, deliverables: List[Dict[str, str]], max_rows: int = 200):
    """
    Append deliverables to table. Assumes table header exists.
    """
    to_add = deliverables[:max_rows]
    for d in to_add:
        try:
            row = table.add_row()
            cells = row.cells
            # ensure at least 3 cells
            if len(cells) < 3:
                # fallback: add text into first cell
                cells[0].text = f"{d.get('title','')} -- {d.get('description','')} -- {d.get('acceptance','')}"
            else:
                cells[0].text = d.get("title", "")
                cells[1].text = d.get("description", "")
                cells[2].text = d.get("acceptance", "")
        except Exception:
            logger.exception("Failed adding deliverable row; writing raw representation")
            try:
                row = table.add_row()
                row.cells[0].text = str(d)
            except Exception:
                logger.exception("Even fallback write failed for deliverable row")

def _append_timeline(table, phases: List[Dict[str, str]], max_rows: int = 200):
    for p in phases[:max_rows]:
        try:
            row = table.add_row()
            cells = row.cells
            if len(cells) < 3:
                cells[0].text = f"{p.get('phase_name','')} - {p.get('duration','')} - {p.get('tasks','')}"
            else:
                cells[0].text = p.get("phase_name", "")
                cells[1].text = str(p.get("duration", ""))
                cells[2].text = p.get("tasks", "")
        except Exception:
            logger.exception("Failed adding timeline row; writing fallback")
            try:
                row = table.add_row()
                row.cells[0].text = str(p)
            except Exception:
                logger.exception("Even fallback write failed for timeline row")

def render_docx_from_template(template_path: str, context: Dict[str, Any]) -> BytesIO:
    """
    Safe rendering:
     - Load template
     - Replace placeholders in document paragraphs and tables
     - Replace placeholders in headers and footers
     - Append deliverables and timeline rows if suitable tables are found
    Returns BytesIO with .docx bytes.
    """
    doc = Document(template_path)

    # Prepare mapping of string values (format currency nicely)
    mapping: Dict[str, str] = {}
    for k, v in context.items():
        if k in ("development_cost", "licenses_cost", "support_cost", "total_investment_cost"):
            mapping[k] = _format_currency(v)
        else:
            mapping[k] = "" if v is None else str(v)

    # Replace placeholders in main document paragraphs
    for para in doc.paragraphs:
        try:
            _replace_in_paragraph(para, mapping)
        except Exception:
            logger.exception("Paragraph replacement failed; continuing.")

    # Replace placeholders in main document tables
    for table in doc.tables:
        try:
            _replace_in_table(table, mapping)
        except Exception:
            logger.exception("Table replacement failed; continuing.")

    # Replace in headers and footers for each section
    for section in doc.sections:
        try:
            # first page headers/footers (if present)
            try:
                _replace_in_header_footer(section.first_page_header, mapping)
            except Exception:
                logger.exception("Replacement in first_page_header failed; continuing.")
            try:
                _replace_in_header_footer(section.first_page_footer, mapping)
            except Exception:
                # some templates may not have first_page_footer
                pass

            # primary header/footer
            try:
                _replace_in_header_footer(section.header, mapping)
            except Exception:
                logger.exception("Replacement in header failed; continuing.")
            try:
                _replace_in_header_footer(section.footer, mapping)
            except Exception:
                logger.exception("Replacement in footer failed; continuing.")
        except Exception:
            logger.exception("Header/footer processing for section failed; continuing.")

    # Append deliverables if table exists
    deliverables_table = _find_table_by_headers(doc, ["Deliverable", "Description", "Acceptance"])
    if deliverables_table and context.get("deliverables_list"):
        try:
            _append_deliverables(deliverables_table, context["deliverables_list"])
        except Exception:
            logger.exception("Appending deliverables failed; continuing.")

    # Append timeline if table exists
    timeline_table = _find_table_by_headers(doc, ["Phase", "Duration", "Key Tasks"])
    if timeline_table and context.get("phases_list"):
        try:
            _append_timeline(timeline_table, context["phases_list"])
        except Exception:
            logger.exception("Appending timeline failed; continuing.")

    # Save to bytes and return
    out = BytesIO()
    doc.save(out)
    out.seek(0)
    return out
