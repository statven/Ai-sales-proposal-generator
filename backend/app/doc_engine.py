# backend/app/doc_engine.py
import os
import re
import json
import tempfile
import logging
import locale
from io import BytesIO
from typing import Dict, Any, List, Optional
from docx import Document
from docx.shared import Inches
from docx.table import Table
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from PIL import Image, ImageDraw, ImageFont

DEFAULT_TARGET_DPI = 300
MAX_PAGE_WIDTH_INCHES = 7.3
MAX_PAGE_HEIGHT_INCHES = 8.3 
# --- Импорт безопасных генераторов диаграмм ---
from backend.app.services.visualization_service import (
    generate_gantt_image,
    generate_lifecycle_diagram,
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

# --- Очистка context от повторных подписей/имен компаний ---
def sanitize_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Убирает вхождения client/provider имени в конце больших текстовых полей,
    чтобы имена компаний появлялись только через соответствующие placeholders.
    """
    out = dict(ctx)  # shallow copy
    client = str(out.get("client_company_name") or out.get("client_name") or "").strip()
    provider = str(out.get("provider_company_name") or out.get("provider_name") or "").strip()

    if not client and not provider:
        return out

    # regex: удаляет повторяющиеся вхождения имени клиента/провайдера в конце строки
    def _strip_trailing_names(s: str) -> str:
        if not isinstance(s, str) or not s.strip():
            return s
        res = s
        # удаляем как клиент, так и провайдер если они в конце
        for nm in (client, provider):
            if not nm:
                continue
            res = re.sub(rf"(\r?\n|\s)*{re.escape(nm)}(\s*)$", "", res)
        # трим пробельные окончания и лишние пустые строки на конце
        res = re.sub(r"\n{3,}", "\n\n", res).rstrip()
        return res

    # ключи, которые не трогаем (это сами поля с именами/подписями)
    keep = {
        "client_company_name", "client_name", "client_signature_name",
        "provider_company_name", "provider_name", "provider_signature_name",
        "client_signature_date", "provider_signature_date"
    }

    for k, v in list(out.items()):
        if k in keep:
            continue
        if isinstance(v, str):
            out[k] = _strip_trailing_names(v)
    return out

# --- Вспомогательные функции замены текста ---
def _replace_in_paragraph(paragraph, mapping: Dict[str, str]) -> None:
    """
    Надёжная замена: склеиваем runs, заменяем (ключи по убыванию длины),
    очищаем run'ы и добавляем один run с результирующим текстом.
    """
    full_text = "".join(run.text for run in paragraph.runs)
    if not full_text:
        return

    new_text = full_text
    replaced = False
    matched_keys: List[str] = []

    # сортируем ключи по длине — чтобы исключить проблемы типа 'title' и 'project_title'
    for k in sorted(mapping.keys(), key=lambda x: -len(x)):
        v = mapping.get(k, "")
        ph = f"{{{{{k}}}}}"
        if ph in new_text:
            new_text = new_text.replace(ph, v or "")
            replaced = True
            matched_keys.append(k)

    if matched_keys:
        logger.info("[DOC_ENGINE] Paragraph %s matched keys: %s", hex(id(paragraph)), matched_keys)

    if replaced and new_text != full_text:
        for run in paragraph.runs:
            run.text = ""
        paragraph.add_run(new_text)
        logger.debug("[DOC_ENGINE] AFTER_REPLACE paragraph id=%s new_text=%r", hex(id(paragraph)), new_text)


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
def _compute_target_image_inches(png_bytes: bytes,
                                 max_width_in: float = MAX_PAGE_WIDTH_INCHES,
                                 max_height_in: float = MAX_PAGE_HEIGHT_INCHES,
                                 fallback_dpi: int = DEFAULT_TARGET_DPI):
    """
    Возвращает (target_w_in, target_h_in, dpi, orig_w_in, orig_h_in).
    Масштабирует пропорционально, чтобы вписаться в max_width_in x max_height_in.
    Не масштабирует вверх (scale <= 1.0).
    """
    try:
        img = Image.open(BytesIO(png_bytes))
        info = img.info or {}
        dpi = None
        if "dpi" in info:
            dval = info.get("dpi")
            if isinstance(dval, (tuple, list)) and len(dval) >= 1:
                try:
                    dpi = int(dval[0])
                except Exception:
                    dpi = None
            else:
                try:
                    dpi = int(dval)
                except Exception:
                    dpi = None
        if not dpi:
            dpi = int(fallback_dpi)

        orig_w_in = img.width / dpi
        orig_h_in = img.height / dpi

        # если размеры некорректны — fallback
        if orig_w_in <= 0 or orig_h_in <= 0:
            return None, None, dpi, None, None

        # вычисляем масштаб, чтобы вписать в оба ограничения
        scale_w = max_width_in / orig_w_in if orig_w_in > 0 else 1.0
        scale_h = max_height_in / orig_h_in if orig_h_in > 0 else 1.0

        scale = min(scale_w, scale_h, 1.0)  # не увеличиваем (не upscale)

        target_w_in = orig_w_in * scale
        target_h_in = orig_h_in * scale

        return round(target_w_in, 3), round(target_h_in, 3), int(dpi), round(orig_w_in, 3), round(orig_h_in, 3)
    except Exception:
        return None, None, fallback_dpi, None, None


# --- Поиск и вставка изображений ---
def _find_and_replace_placeholder_with_image(doc: Document, placeholder: str, image_bytes: bytes,
                                             width_inches: float = 7.5, height_inches: Optional[float] = None) -> bool:
    """Находит {{placeholder}} и заменяет его изображением"""
    for p in doc.paragraphs:
        if placeholder in p.text:
            for r in list(p.runs):
                r.text = ""
            run = p.add_run()
            try:
                if height_inches:
                    run.add_picture(BytesIO(image_bytes), width=Inches(width_inches), height=Inches(height_inches))
                else:
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
                            if height_inches:
                                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches), height=Inches(height_inches))
                            else:
                                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
                        except Exception:
                            p.add_run("[Image could not be embedded]")
                        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
                        return True
    return False


def _insert_image_with_caption(doc: Document, image_bytes: bytes, placeholder: str,
                               width_inches: float = 8.5, height_inches: Optional[float] = None):
    inserted = _find_and_replace_placeholder_with_image(doc, placeholder, image_bytes, width_inches, height_inches)
    if not inserted:
        # если плейсхолдер не найден — добавляем в конец
        p = doc.add_paragraph()
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = p.add_run()
        try:
            if height_inches:
                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches), height=Inches(height_inches))
            else:
                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
        except Exception:
            p.add_run("[Image could not be embedded]")
    # подпи



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

def _placeholder_png_bytes(text: str = "Diagram unavailable", width: int = 800, height: int = 400) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except IOError:
        font = ImageFont.load_default()
    
    try:
        # draw.textbbox((x, y), text, font) возвращает (left, top, right, bottom)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    except Exception as e:
        # Fallback values
        logger.error("Error calculating text size using textbbox: %s", str(e))
        text_width, text_height = 300, 25 
    
    # 2. Вычисляем позицию для центрирования
    x = (width - text_width) / 2
    y = (height - text_height) / 2
    
    # 3. Рисуем текст
    draw.text((x, y), text, fill=(50, 50, 50), font=font)
    
    # 4. Сохраняем в буфер
    buf = BytesIO()
    img.save(buf, format="PNG")
    
    return buf.getvalue()

def _insert_lifecycle_diagram(doc: Document, image_bytes: bytes, placeholder: str, 
                              width_inches: float = 6.5, height_inches: Optional[float] = None):
    inserted = _find_and_replace_placeholder_with_image(doc, placeholder, image_bytes, width_inches, height_inches)
    if not inserted:
        p = doc.add_paragraph()
        p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        run = p.add_run()
        try:
            if height_inches:
                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches), height=Inches(height_inches))
            else:
                run.add_picture(BytesIO(image_bytes), width=Inches(width_inches))
        except Exception:
            p.add_run("[Image could not be embedded]")


def render_docx_from_template(template_path: str, context: Dict[str, Any]) -> BytesIO:
    doc = Document(template_path)
    # 1. Prepare mapping (currency formatting preserved)
        # 1. Prepare mapping (currency formatting preserved)
    # Сначала чистим context от лишних подписей
    context = sanitize_context(context)

    mapping = {}
    for k, v in context.items():
        if k in ("development_cost", "licenses_cost", "support_cost", "total_investment_cost"):
            try:
                mapping[k] = _format_currency(v)
            except Exception:
                mapping[k] = str(v or "")
        else:
            mapping[k] = "" if v is None else str(v)

    # diagnostic dump mapping (temporary)
    try:
        dbg = os.path.join(tempfile.gettempdir(), "doc_engine_mapping_dump.json")
        with open(dbg, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, indent=2)
        logger.info("[DOC_ENGINE] Mapping dumped to %s", dbg)
    except Exception:
        logger.exception("Failed dumping mapping")



    # 2. Replace placeholders in paragraphs and tables
    original_paragraphs = list(doc.paragraphs)
    
    for p in original_paragraphs:
        
        full_text = "".join(run.text for run in p.runs)

        if "{{" not in full_text:
            continue # Нет плейсхолдеров, пропускаем


        new_full_text = full_text
        has_replacement = False
        for k, v in mapping.items():
            ph = f"{{{{{k}}}}}"
            if ph in new_full_text:
                new_full_text = new_full_text.replace(ph, v) # 'v' уже строка
                has_replacement = True
        
        if not has_replacement:
            continue
        for r in p.runs:
            r.text = ""
        lines = new_full_text.split('\n')
        _apply_formatting_to_run(p, lines[0] if lines else "")
            
        anchor_element = p._p 
        
        for line in lines[1:]:
            new_p = doc.add_paragraph()
            _apply_formatting_to_run(new_p, line)
            anchor_element.addnext(new_p._p)
            anchor_element = new_p._p




    # 3. Headers/footers (Эта часть остается без изменений)
    for section in doc.sections:
        try:
            _replace_in_header_footer(section.first_page_header, mapping)
            _replace_in_header_footer(section.first_page_footer, mapping)
            _replace_in_header_footer(section.header, mapping)
            _replace_in_header_footer(section.footer, mapping)
        except Exception:
            logger.exception("Header/footer replacement failed.")
            
    # 4. Tables
    for table in doc.tables:
        try:
            _replace_in_table(table, mapping)
        except Exception:
            logger.exception("Table replace failed.")
            
    # 4.1 Append deliverables & timeline (Эта часть остается без изменений)
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

    # 6. Generate lifecycle diagram image
    
    lifecycle_png = None
    try:
        logger.debug("Visualization data for lifecycle diagram: %s", json.dumps(viz, ensure_ascii=False))

        lifecycle_png = generate_lifecycle_diagram(viz)
        if tmpdir and lifecycle_png:
            try:
                with open(os.path.join(tmpdir, "lifecycle.png"), "wb") as fh:
                    fh.write(lifecycle_png)
            except Exception:
                logger.exception("Failed saving lifecycle.png")
        if lifecycle_png and len(lifecycle_png) < 1500:
            logger.warning("Lifecycle image likely placeholder (empty input or export error). Size=%d bytes", len(lifecycle_png))
    except Exception:
        logger.exception("Lifecycle generation raised exception.")

    # 7. Insert images into doc 
    try:

        if tmpdir and lifecycle_png:
            try:
                with open(os.path.join(tmpdir, "lifecycle.png"), "wb") as fh:
                    fh.write(lifecycle_png)
            except Exception:
                logger.exception("Failed saving lifecycle.png")
        if lifecycle_png and len(lifecycle_png) < 1500:
            logger.warning("Lifecycle image likely placeholder (empty input or export error). Size=%d bytes", len(lifecycle_png))

        # Вставка lifecycle: вычисляем реальные инчи и корректируем по странице
                # Вставка lifecycle: вычисляем целевые размеры и корректируем по странице (ширина и высота)
        if lifecycle_png:
            w_in, h_in, dpi, orig_w_in, orig_h_in = _compute_target_image_inches(lifecycle_png)
            if w_in is None:
                # fallback: стандартная вставка
                _insert_lifecycle_diagram(doc, lifecycle_png, "{{lifecycle_diagram}}")
            else:
                logger.debug("Lifecycle image: orig (in) %s x %s @%sdpi -> target (in) %s x %s",
                            orig_w_in, orig_h_in, dpi, w_in, h_in)
                # Передаём ТОЛЬКО width_inches, чтобы сохранить пропорции при вставке.
                # height_inches=None даст python-docx возможность сохранить пропорции.
                _insert_lifecycle_diagram(doc, lifecycle_png, "{{lifecycle_diagram}}",
                                        width_inches=float(w_in), height_inches=None)
        else:
            placeholder_png = _placeholder_png_bytes("Lifecycle diagram unavailable", width=800, height=400)
            _find_and_replace_placeholder_with_image(doc, "{{lifecycle_diagram}}", placeholder_png, width_inches=min(6.5, MAX_PAGE_WIDTH_INCHES), height_inches=None)


    except Exception:
        logger.exception("Inserting lifecycle diagram failed.")

    # 7. Generate diagrams and save intermediate PNGs for debugging
    gantt_png = None
    
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
        if gantt_png:
            w_in, h_in, dpi, orig_w_in, orig_h_in = _compute_target_image_inches(gantt_png)
            if w_in is None:
                _insert_image_with_caption(doc, gantt_png, "{{gantt_chart}}")
            else:
                logger.debug("Gantt image: orig (in) %s x %s @%sdpi -> target (in) %s x %s",
                            orig_w_in, orig_h_in, dpi, w_in, h_in)
                _insert_image_with_caption(doc, gantt_png, "{{gantt_chart}}",
                                        width_inches=float(w_in), height_inches=None)
        else:
            placeholder_png = _placeholder_png_bytes("Gantt chart unavailable", width=1000, height=500)
            _find_and_replace_placeholder_with_image(doc, "{{gantt_chart}}", placeholder_png, width_inches=min(7.5, MAX_PAGE_WIDTH_INCHES), height_inches=None)


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

def _apply_formatting_to_run(paragraph, text_line: str):
    if not text_line:

        paragraph.add_run("")
        return
    parts = re.split(r"(\*\*.*?\*\*|\*.*?\*)", text_line)
    
    for part in parts:
        if not part:
            continue
            
        if part.startswith("**") and part.endswith("**"):
            # жирный текст
            text = part[2:-2] # **
            run = paragraph.add_run(text)
            run.bold = True
        elif part.startswith("*") and part.endswith("*"):
            # курсив
            text = part[1:-1] 
            run = paragraph.add_run(text)
            run.italic = True
        else:
            # обычный текст
            run = paragraph.add_run(part)