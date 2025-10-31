# backend/app/main.py
import logging
import os
import json
import re
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import ValidationError
from datetime import datetime, date
from io import BytesIO
from urllib.parse import quote
import asyncio

logger = logging.getLogger("uvicorn.error")

# --- optional imports from your project (best-effort; keep app runnable if missing) ---
try:
    from backend.app.doc_engine import render_docx_from_template
except Exception:
    render_docx_from_template = None
    logger.warning("doc_engine not importable; DOCX generation disabled in this environment.")

try:
    from backend.app.models import ProposalInput
except Exception:
    class ProposalInput:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
        def dict(self):
            return dict(self.__dict__)
    logger.warning("models.ProposalInput not importable; using shim (not strict validation).")

try:
    from backend.app import db
except Exception:
    class _MockDB:
        def init_db(self): pass
        def save_version(self, *args, **kwargs): return None
        def get_version(self, id): return None
    db = _MockDB()
    logger.warning("db not importable; using mock DB.")

try:
    from backend.app import ai_core
except Exception:
    ai_core = None
    logger.warning("ai_core not importable; AI generation disabled.")

try:
    from backend.app.services import openai_service
except Exception:
    openai_service = None
    logger.warning("openai_service not importable; suggestion service disabled.")

# --- app init ---
app = FastAPI(title="AI Sales Proposal Generator (Backend)")

TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", os.path.join(os.getcwd(), "docs", "template.docx"))
if not os.path.exists(TEMPLATE_PATH):
    logger.warning("Template not found at %s. Ensure template.docx is present.", TEMPLATE_PATH)

try:
    db.init_db()
except Exception:
    logger.exception("db.init_db() failed (continuing)")

# ----------------- Helpers -----------------
def _format_date(val: Optional[Any]) -> str:
    """Приводим дату к читаемому виду: 31 October 2025. При None -> empty string."""
    if val is None or val == "":
        return ""
    if isinstance(val, date):
        try:
            return val.strftime("%d %B %Y")
        except Exception:
            return val.isoformat()
    if isinstance(val, str):
        try:
            d = date.fromisoformat(val)
            return d.strftime("%d %B %Y")
        except Exception:
            return val
    return str(val)

def _safe_filename(name: Optional[str]) -> str:
    if not name:
        return "proposal"
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    return safe[:120] or "proposal"

def _calculate_total_investment(financials: Optional[Dict[str, Any]]) -> float:
    if not isinstance(financials, dict):
        return 0.0
    def f(k):
        v = financials.get(k)
        try:
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0
    return f("development_cost") + f("licenses_cost") + f("support_cost")

# --- replace _prepare_list_data with this improved version ---
def _prepare_list_data(context: Dict[str, Any]) -> None:
    """
    Convert canonical Pydantic payload keys to keys expected by doc_engine/template:
      - deliverables -> deliverables_list, acceptance_criteria -> acceptance
      - phases -> phases_list, duration_weeks -> duration
    Add numbering for phases (Phase 1, Phase 2, ...).
    This mutates context in-place.
    """
    # deliverables -> deliverables_list (acceptance rename)
    if "deliverables" in context and isinstance(context["deliverables"], list):
        deliverables = []
        for d in context["deliverables"]:
            if not isinstance(d, dict):
                continue
            dd = dict(d)
            if "acceptance_criteria" in dd and "acceptance" not in dd:
                dd["acceptance"] = dd.get("acceptance_criteria")
            for k in ("title", "description", "acceptance"):
                dd[k] = "" if dd.get(k) is None else str(dd[k])
            deliverables.append({"title": dd["title"], "description": dd["description"], "acceptance": dd["acceptance"]})
        context.pop("deliverables", None)
        context["deliverables_list"] = deliverables

    # phases -> phases_list with numbering and normalized duration
    if "phases" in context and isinstance(context["phases"], list):
        phases_out = []
        for idx, p in enumerate(context["phases"]):
            if not isinstance(p, dict):
                continue
            pp = dict(p)
            # normalize weeks/duration
            if "duration_weeks" in pp and "duration" not in pp:
                pp["duration"] = pp.get("duration_weeks")
            elif "duration" in pp and "duration_weeks" not in pp:
                try:
                    pp["duration"] = int(str(pp["duration"]).split()[0])
                except Exception:
                    pp["duration"] = 1
            # generate phase_name with index if not provided
            raw_name = pp.get("phase_name") or pp.get("name") or ""
            if not raw_name or raw_name.strip().lower() in ("phase", "этап"):
                phase_name = f"Phase {idx+1}"
            else:
                phase_name = raw_name
            tasks = pp.get("tasks") or ""
            # ensure strings
            phases_out.append({
                "phase_name": phase_name,
                "duration": str(pp.get("duration") or ""),
                "tasks": str(tasks)
            })
        context.pop("phases", None)
        context["phases_list"] = phases_out


# Normalize incoming payload keys (support aliases)
def _normalize_incoming_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(raw) if isinstance(raw, dict) else {}
    if "client_company_name" not in p and "client_name" in p:
        p["client_company_name"] = p["client_name"]
    if "provider_company_name" not in p and "provider_name" in p:
        p["provider_company_name"] = p["provider_name"]
    if "scope" not in p and "scope_description" in p:
        p["scope"] = p["scope_description"]
    if "scope_description" not in p and "scope" in p:
        p["scope_description"] = p["scope"]

    # tone mapping (safe)
    if "tone" in p and isinstance(p["tone"], str):
        t = p["tone"].strip()
        mapping = {
            "Формальный": "Formal",
            "Маркетинг": "Marketing",
            "Маркетирование": "Marketing",
            "Technical": "Technical",
            "Технический": "Technical",
            "Friendly": "Friendly",
            "Дружелюбный": "Friendly",
        }
        p["tone"] = mapping.get(t, t if t in ("Formal", "Marketing", "Technical", "Friendly") else "Formal")

    # deliverables normalization
    if "deliverables" in p and isinstance(p["deliverables"], list):
        normalized = []
        for d in p["deliverables"]:
            if not isinstance(d, dict):
                continue
            nd = dict(d)
            if "acceptance_criteria" not in nd and "acceptance" in nd:
                nd["acceptance_criteria"] = nd.get("acceptance")
            normalized.append(nd)
        p["deliverables"] = normalized

    # phases normalization
    if "phases" in p and isinstance(p["phases"], list):
        normalized = []
        for ph in p["phases"]:
            if not isinstance(ph, dict):
                continue
            np_ = dict(ph)
            if "duration_weeks" not in np_ and "duration" in np_:
                try:
                    np_["duration_weeks"] = int(str(np_["duration"]).split()[0])
                except Exception:
                    np_["duration_weeks"] = 1
            normalized.append(np_)
        p["phases"] = normalized

    if "financials" in p and isinstance(p["financials"], dict):
        f = dict(p["financials"])
        for k in ("development_cost", "licenses_cost", "support_cost"):
            if k in f and f[k] is not None:
                try:
                    f[k] = float(f[k])
                except Exception:
                    pass
        p["financials"] = f

    logger.debug("Normalized payload keys: %s", list(p.keys()))
    return p

# ---------------- AI text sanitizer ----------------
_PLACEHOLDER_PATTERNS = [
    # [client_name], [client], {client_name}, {{client_name}}, etc.
    (re.compile(r"\[ *client_name *\]", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\[ *client *\]", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\{\{ *client_name *\}\}", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\{ *client_name *\}", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\[ *provider_name *\]", flags=re.IGNORECASE), "provider_company_name"),
    (re.compile(r"\[ *provider *\]", flags=re.IGNORECASE), "provider_company_name"),
    (re.compile(r"\{\{ *provider_name *\}\}", flags=re.IGNORECASE), "provider_company_name"),
    (re.compile(r"\{ *provider_name *\}", flags=re.IGNORECASE), "provider_company_name"),
]

def _sanitize_ai_text(s: Optional[str], context: Dict[str, Any]) -> str:
    if s is None:
        return ""
    text = str(s)
    # Replace known placeholder patterns with context values
    for patt, key in _PLACEHOLDER_PATTERNS:
        val = context.get(key) or context.get(key.replace("_company", "")) or ""
        if val is None:
            val = ""
        text = patt.sub(str(val), text)
    # also replace generic tokens like [client] / {client}
    # replace any {{client_company_name}} style with actual too
    text = re.sub(r"\{\{\s*client_company_name\s*\}\}", str(context.get("client_company_name", "")), text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*provider_company_name\s*\}\}", str(context.get("provider_company_name", "")), text, flags=re.IGNORECASE)
    # collapse multiple spaces and trim
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text

# ----------------- End helpers -----------------

@app.post("/api/v1/generate-proposal", tags=["Proposal Generation"])
async def generate_proposal(payload: Dict[str, Any] = Body(...)):
    if render_docx_from_template is None:
        raise HTTPException(status_code=503, detail="Document engine is not available on this server.")

    normalized = _normalize_incoming_payload(payload)

    try:
        proposal = ProposalInput(**normalized)
    except ValidationError as ve:
        logger.warning("Validation failed for incoming proposal: %s", ve.json())
        return JSONResponse(status_code=422, content={"detail": ve.errors()})

    # AI generation
    ai_sections: Dict[str, Any] = {}
    used_model: Optional[str] = None
    try:
        if ai_core is None:
            logger.debug("ai_core not available; skipping AI generation.")
            ai_sections = {}
        else:
            ai_sections = await ai_core.generate_ai_sections(proposal.dict())
            if isinstance(ai_sections, dict) and "_used_model" in ai_sections:
                used_model = ai_sections.pop("_used_model")
            elif isinstance(ai_sections, dict) and "used_model" in ai_sections:
                used_model = ai_sections.get("used_model")
    except Exception as e:
        logger.exception("AI generation failed: %s", e)
        # fallback to safe if ai_core implements fallback
        try:
            ai_sections = await getattr(ai_core, "generate_ai_sections_safe")(proposal.dict())
        except Exception:
            raise HTTPException(status_code=500, detail=f"AI generation failed: {type(e).__name__}")

    # Build context
    context = proposal.dict()
    # keep UI helper dates if provided originally
    if "proposal_date" in payload:
        context["proposal_date"] = payload.get("proposal_date")
    if "valid_until_date" in payload:
        context["valid_until_date"] = payload.get("valid_until_date")
    # signatures: ensure keys exist (avoid leaving placeholders un-replaced)
    # Если пользователь не передал имя/дату подписи — подставляем видимый заполнитель
    default_sig_line = "_________________________"
    context["client_signature_name"] = context.get("client_signature_name") or default_sig_line
    context["provider_signature_name"] = context.get("provider_signature_name") or default_sig_line

    # даты подписи — оставляем пустыми если не заданы (в формате dd Month YYYY если заданы)
    context["client_signature_date"] = _format_date(context.get("client_signature_date"))
    context["provider_signature_date"] = _format_date(context.get("provider_signature_date"))

    # ensure both naming variants exist for templates and sanitization
    client_name_val = context.get("client_company_name") or context.get("client_name") or ""
    provider_name_val = context.get("provider_company_name") or context.get("provider_name") or ""
    context["client_company_name"] = client_name_val
    context["client_name"] = client_name_val
    context["provider_company_name"] = provider_name_val
    context["provider_name"] = provider_name_val

    # sanitize AI text (replace placeholders embedded in LLM output)
    if isinstance(ai_sections, dict):
        for k, v in list(ai_sections.items()):
            ai_sections[k] = _sanitize_ai_text(v, context)

    # merge AI sections into context (AI text now sanitized)
    if isinstance(ai_sections, dict):
        context.update(ai_sections)

    # convert lists & keys for doc engine
    _prepare_list_data(context)

    # computed/flattened fields
    context["current_date"] = _format_date(date.today())
    context["expected_completion_date"] = _format_date(context.get("deadline"))
    context["proposal_date"] = _format_date(context.get("proposal_date"))
    context["valid_until_date"] = _format_date(context.get("valid_until_date"))

    # signatures: ensure keys exist (avoid leaving placeholders un-replaced)
    context["client_signature_name"] = context.get("client_signature_name") or ""
    context["provider_signature_name"] = context.get("provider_signature_name") or ""
    context["client_signature_date"] = _format_date(context.get("client_signature_date"))
    context["provider_signature_date"] = _format_date(context.get("provider_signature_date"))

    # financials flatten / totals
    if context.get("financials") and isinstance(context["financials"], dict):
        fin = context["financials"]
        context["development_cost"] = fin.get("development_cost")
        context["licenses_cost"] = fin.get("licenses_cost")
        context["support_cost"] = fin.get("support_cost")
        context["total_investment_cost"] = _calculate_total_investment(fin)

    logger.debug("Rendering context keys: %s", sorted(list(context.keys())))
    # Render DOCX
    def _format_signature_date(val):
        if val is None or val == "":
            return ""
        # if already a date-like iso string, try parse and pretty-format
        try:
            if isinstance(val, str):
                # try ISO parse
                try:
                    dt = date.fromisoformat(val)
                    # readable: 31 October 2025 (you can adapt to locale if needed)
                    return dt.strftime("%d %B %Y")
                except Exception:
                    return val
            if isinstance(val, date):
                return val.strftime("%d %B %Y")
        except Exception:
            pass
        return str(val)

    # Default visible line for missing name (so placeholder doesn't disappear visually)
    _default_sig_line = "_________________________"

    # Ensure keys exist — take from context if present, otherwise safe fallback
    context["client_signature_name"] = context.get("client_signature_name") or context.get("client_name") or context.get("client_company_name") or _default_sig_line
    context["provider_signature_name"] = context.get("provider_signature_name") or context.get("provider_name") or context.get("provider_company_name") or _default_sig_line

    # Format signature dates (empty string if missing)
    context["client_signature_date"] = _format_signature_date(context.get("client_signature_date") or context.get("client_signature_date_iso") or "")
    context["provider_signature_date"] = _format_signature_date(context.get("provider_signature_date") or context.get("provider_signature_date_iso") or "")

    # Now render_docx_from_template(...) can safely replace {{client_signature_name}} etc.

    try:
        doc_out = render_docx_from_template(TEMPLATE_PATH, context)
    except Exception as e:
        logger.exception("DOCX rendering failed: %s", e)
        raise HTTPException(status_code=500, detail=f"DOCX rendering failed: {str(e)}")

    # get bytes
    try:
        if isinstance(doc_out, BytesIO):
            doc_bytes = doc_out.getvalue()
        elif hasattr(doc_out, "getvalue"):
            doc_bytes = doc_out.getvalue()
        elif isinstance(doc_out, (bytes, bytearray)):
            doc_bytes = bytes(doc_out)
        else:
            doc_bytes = bytes(doc_out)
    except Exception as e:
        logger.exception("Failed to extract bytes from doc engine output: %s", e)
        raise HTTPException(status_code=500, detail="DOCX generation returned unexpected type")

    # Save version
    version_id = None
    try:
        version_id = db.save_version(payload=proposal.dict(), ai_sections=ai_sections or {}, used_model=used_model)
    except Exception:
        logger.exception("Failed to save proposal version (non-fatal)")

    filename = f"{_safe_filename(context.get('client_company_name') or '')}_{_safe_filename(context.get('project_goal') or '')}.docx"
    encoded = quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    if version_id:
        headers["X-Proposal-Version"] = str(version_id)

    return StreamingResponse(BytesIO(doc_bytes), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)




@app.post("/proposal/regenerate", tags=["Proposal Generation"])
async def regenerate_proposal(body: Dict[str, Any] = Body(...)):
    """
    Regenerate a proposal by version_id (body={"version_id": 123}) or by passing a full payload (same shape as /api/v1/generate-proposal).
    """
    if render_docx_from_template is None:
        raise HTTPException(status_code=503, detail="Document engine is not available on this server.")

    version_id = body.get("version_id")
    if version_id:
        # load from DB
        try:
            rec = db.get_version(int(version_id))
        except Exception as e:
            logger.exception("DB get_version failed: %s", e)
            raise HTTPException(status_code=500, detail="Database read failed")
        if not rec:
            raise HTTPException(status_code=404, detail="Version not found")
        # rec expected to contain 'payload' and 'ai_sections' as JSON strings or already-parsed
        payload = rec.get("payload")
        ai_sections = rec.get("ai_sections") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                # if payload is not JSON, assume it's dict-like stored differently
                pass
        if isinstance(ai_sections, str):
            try:
                ai_sections = json.loads(ai_sections)
            except Exception:
                ai_sections = {}
    else:
        # direct regen from provided payload
        payload = body
        ai_sections = {}

    # normalize incoming payload (aliases)
    normalized = _normalize_incoming_payload(payload)

    # validate
    try:
        proposal = ProposalInput(**normalized)
    except ValidationError as ve:
        logger.warning("Validation failed for regeneration payload: %s", ve.json())
        return JSONResponse(status_code=422, content={"detail": ve.errors()})

    # Build context and merge ai_sections if present — IMPORTANT: by_alias=True
    context = proposal.dict(by_alias=True, exclude_none=True)
    if isinstance(ai_sections, dict):
        context.update(ai_sections)

    _prepare_list_data(context)
    context["current_date"] = _format_date(date.today())
    context["expected_completion_date"] = _format_date(context.get("deadline"))
    context["proposal_date"] = _format_date(context.get("proposal_date"))
    context["valid_until_date"] = _format_date(context.get("valid_until_date"))

    if context.get("financials") and isinstance(context["financials"], dict):
        fin = context["financials"]
        context["development_cost"] = fin.get("development_cost")
        context["licenses_cost"] = fin.get("licenses_cost")
        context["support_cost"] = fin.get("support_cost")
        context["total_investment_cost"] = _calculate_total_investment(fin)
    # --- Ensure signature placeholders always exist in context ---
    # Put this right before calling render_docx_from_template(...)

    # helper to format date nicely for signature (dd Month YYYY) — uses _format_date if present
    def _format_signature_date(val):
        if val is None or val == "":
            return ""
        # if already a date-like iso string, try parse and pretty-format
        try:
            if isinstance(val, str):
                # try ISO parse
                try:
                    dt = date.fromisoformat(val)
                    # readable: 31 October 2025 (you can adapt to locale if needed)
                    return dt.strftime("%d %B %Y")
                except Exception:
                    return val
            if isinstance(val, date):
                return val.strftime("%d %B %Y")
        except Exception:
            pass
        return str(val)

    # Default visible line for missing name (so placeholder doesn't disappear visually)
    _default_sig_line = "_________________________"

    # Ensure keys exist — take from context if present, otherwise safe fallback
    context["client_signature_name"] = context.get("client_signature_name") or context.get("client_name") or context.get("client_company_name") or _default_sig_line
    context["provider_signature_name"] = context.get("provider_signature_name") or context.get("provider_name") or context.get("provider_company_name") or _default_sig_line

    # Format signature dates (empty string if missing)
    context["client_signature_date"] = _format_signature_date(context.get("client_signature_date") or context.get("client_signature_date_iso") or "")
    context["provider_signature_date"] = _format_signature_date(context.get("provider_signature_date") or context.get("provider_signature_date_iso") or "")

    # Now render_docx_from_template(...) can safely replace {{client_signature_name}} etc.

    try:
        doc_out = render_docx_from_template(TEMPLATE_PATH, context)
    except Exception as e:
        logger.exception("Regeneration DOCX rendering failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Regeneration failed: {str(e)}")

    try:
        if isinstance(doc_out, BytesIO):
            doc_bytes = doc_out.getvalue()
        elif hasattr(doc_out, "getvalue"):
            doc_bytes = doc_out.getvalue()
        elif isinstance(doc_out, (bytes, bytearray)):
            doc_bytes = bytes(doc_out)
        else:
            doc_bytes = bytes(doc_out)
    except Exception as e:
        logger.exception("Failed to extract bytes on regen: %s", e)
        raise HTTPException(status_code=500, detail="Regeneration returned unexpected type")

    filename = f"Regen_V{version_id or 'manual'}_{_safe_filename(context.get('client_company_name') or '')}.docx"
    encoded = quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    if version_id:
        headers["X-Proposal-Version"] = str(version_id)

    return StreamingResponse(BytesIO(doc_bytes), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)

@app.post("/api/v1/suggest", tags=["AI Suggestions"])
async def suggest_content(payload: Dict[str, Any] = Body(...)):
    """
    Return suggested deliverables and phases (for UI suggest_mode).
    The response should be a JSON object such as:
      {"suggested_deliverables": [...], "suggested_phases": [...]}
    """
    if openai_service is None:
        return JSONResponse(status_code=503, content={"detail": "AI suggestion service is not available."})

    normalized = _normalize_incoming_payload(payload)

    # validate minimal shape (we only need brief to generate suggestions)
    try:
        # Use ProposalInput validation to ensure payload makes sense
        ProposalInput(**normalized)
    except ValidationError as ve:
        logger.warning("Suggestion request validation failed: %s", ve.json())
        return JSONResponse(status_code=422, content={"detail": ve.errors()})

    try:
        # expected to return a dict/json
        suggestions = openai_service.generate_suggestions(normalized)
        # ensure JSON-serializable dict
        if isinstance(suggestions, str):
            try:
                suggestions = json.loads(suggestions)
            except Exception:
                suggestions = {"raw": suggestions}
        return JSONResponse(status_code=200, content=suggestions)
    except Exception as e:
        logger.exception("Suggestion generation failed: %s", e)
        return JSONResponse(status_code=500, content={"detail": "Suggestion generation failed."})
