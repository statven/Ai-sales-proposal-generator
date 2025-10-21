# backend/app/main.py
import logging
import os
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import ValidationError
from datetime import datetime, date
from io import BytesIO
from urllib.parse import quote

from backend.app.ai_core import generate_ai_sections, generate_ai_sections_safe
from backend.app.doc_engine import render_docx_from_template
from backend.app.models import ProposalInput, Deliverable, Phase

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="AI Sales Proposal Generator (Backend)")

TEMPLATE_PATH = r"D:\programming\ai-sales-proposal-generator\docs\template.docx"
if not os.path.exists(TEMPLATE_PATH):
    logger.warning("Template not found at %s. Please ensure template.docx is present.", TEMPLATE_PATH)


def _format_date(val) -> str:
    """Format date-like values to 'DD Month YYYY' or return empty/string fallback."""
    if val is None:
        return ""
    if isinstance(val, (date, datetime)):
        return val.strftime("%d %B %Y")
    try:
        d = date.fromisoformat(str(val))
        return d.strftime("%d %B %Y")
    except Exception:
        return str(val)


def _safe_filename(name: str) -> str:
    """
    Prepare a safe filename for filesystem use (not used for HTTP header encoding).
    Keep only alnum, space, underscore, dash and replace spaces with underscores.
    """
    return "".join(c for c in (name or "") if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or "proposal"


@app.post("/api/v1/generate-proposal")
async def generate_proposal(payload: Dict[str, Any]):
    """
    Accept JSON matching ProposalInput and return a generated DOCX proposal.

    Flow:
      1) Validate payload with Pydantic
      2) Ask AI Core for structured JSON (generate_ai_sections)
      3) Fallback to generate_ai_sections_safe on errors/timeouts
      4) Build context, render DOCX and stream back with UTF-8 filename support
    """
    # 1) Validate input
    try:
        proposal = ProposalInput(**payload)
    except ValidationError as e:
        logger.warning("Validation failed: %s", e.json())
        return JSONResponse(status_code=422, content={"detail": e.errors()})

    # 2) Ask primary AI Core (expects structured JSON). If it fails, use safe fallback.
    try:
        ai_sections = await generate_ai_sections(proposal.dict(), tone=getattr(proposal, "tone", "Formal"))
        # ensure it's a dict with expected string values (defensive)
        if not isinstance(ai_sections, dict):
            logger.warning("AI returned non-dict result, falling back to safe stub.")
            raise RuntimeError("AI returned non-dict")
    except Exception as exc:
        logger.exception("Primary AI generation failed: %s. Falling back to safe generator.", exc)
        try:
            ai_sections = await generate_ai_sections_safe(proposal.dict())
        except Exception:
            logger.exception("Safe AI generator also failed; using empty defaults.")
            ai_sections = {k: "" for k in [
                "executive_summary_text",
                "project_mission_text",
                "solution_concept_text",
                "project_methodology_text",
                "financial_justification_text",
                "payment_terms_text",
                "development_note",
                "licenses_note",
                "support_note"
            ]}

    # 3) Prepare financials
    fin = proposal.financials or {}
    dev = getattr(fin, "development_cost", 0) or 0
    lic = getattr(fin, "licenses_cost", 0) or 0
    sup = getattr(fin, "support_cost", 0) or 0
    total = sum(float(x or 0) for x in [dev, lic, sup])

    # 4) Build context for template
    context: Dict[str, Any] = {
        "current_date": _format_date(datetime.utcnow().date()),
        "client_company_name": proposal.client_name or "",
        "provider_company_name": proposal.provider_name or "",
        "expected_completion_date": _format_date(proposal.deadline),
        "development_cost": dev,
        "licenses_cost": lic,
        "support_cost": sup,
        "total_investment_cost": total,
        **(ai_sections or {}),
        # Signatures (defaults if not provided in payload)
        "client_signature_name": getattr(proposal, "client_signature_name", "") or "",
        "client_signature_date": _format_date(getattr(proposal, "client_signature_date", None)),
        "provider_signature_name": getattr(proposal, "provider_signature_name", "") or "",
        "provider_signature_date": _format_date(getattr(proposal, "provider_signature_date", None))
    }

    # 5) Deliverables: both list (for dynamic table) and numbered placeholders (back-compat)
    deliverables_list = []
    for idx in range(4):  # create up to 4 numbered placeholders
        if proposal.deliverables and idx < len(proposal.deliverables):
            d: Deliverable = proposal.deliverables[idx]
            deliverables_list.append({
                "title": d.title,
                "description": d.description,
                "acceptance": d.acceptance_criteria
            })
            context[f"deliverable_{idx+1}_title"] = d.title
            context[f"deliverable_{idx+1}_description"] = d.description
            context[f"deliverable_{idx+1}_acceptance"] = d.acceptance_criteria
        else:
            context[f"deliverable_{idx+1}_title"] = ""
            context[f"deliverable_{idx+1}_description"] = ""
            context[f"deliverable_{idx+1}_acceptance"] = ""
    context["deliverables_list"] = deliverables_list

    # 6) Phases: both list and numbered placeholders
    phases_list = []
    for idx in range(3):  # support up to 3 phases placeholders
        if proposal.phases and idx < len(proposal.phases):
            p: Phase = proposal.phases[idx]
            phases_list.append({
                "phase_name": f"Phase {idx+1}",
                "duration": f"{p.duration_weeks} weeks",
                "tasks": p.tasks
            })
            context[f"phase_{idx+1}_tasks"] = p.tasks
            context[f"phase_{idx+1}_duration"] = f"{p.duration_weeks} weeks"
        else:
            context[f"phase_{idx+1}_tasks"] = ""
            context[f"phase_{idx+1}_duration"] = ""
    context["phases_list"] = phases_list

    # 7) Render DOCX
    try:
        docx_io: BytesIO = render_docx_from_template(TEMPLATE_PATH, context)
    except Exception as e:
        logger.exception("Document generation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Document generation failed: {str(e)}")

    # 8) Prepare Content-Disposition with RFC5987 UTF-8 filename encoding (widely supported)
    # Use the raw client name in filename but percent-encode it for header
    filename_raw = f"proposal_{proposal.client_name or 'proposal'}.docx"
    quoted = quote(filename_raw, safe="")
    content_disposition = f"attachment; filename*=UTF-8''{quoted}"

    return StreamingResponse(
        docx_io,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": content_disposition}
    )
