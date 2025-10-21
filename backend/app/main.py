# backend/app/main.py
import logging
import os
from typing import List
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import ValidationError
from datetime import datetime, date
from io import BytesIO
from urllib.parse import quote

from backend.app.ai_core import generate_ai_sections_safe
from backend.app.doc_engine import render_docx_from_template
from backend.app.models import ProposalInput, Deliverable, Phase

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="AI Sales Proposal Generator (Backend)")

TEMPLATE_PATH = r"D:\programming\ai-sales-proposal-generator\docs\template.docx"
if not os.path.exists(TEMPLATE_PATH):
    logger.warning("Template not found at %s. Please ensure template.docx is present.", TEMPLATE_PATH)


def _format_date(val):
    if val is None:
        return ""
    if isinstance(val, (date, datetime)):
        return val.strftime("%d %B %Y")
    try:
        d = date.fromisoformat(val)
        return d.strftime("%d %B %Y")
    except Exception:
        return str(val)


def _safe_filename(name: str) -> str:
    """Сделать имя файла безопасным для файла и ASCII-заголовков."""
    return "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")


@app.post("/api/v1/generate-proposal")
async def generate_proposal(payload: dict):
    # --- Валидация ---
    try:
        proposal = ProposalInput(**payload)
    except ValidationError as e:
        logger.warning("Validation failed: %s", e.json())
        return JSONResponse(status_code=422, content={"detail": e.errors()})

    # --- Генерация AI-секций ---
    try:
        ai_sections = await generate_ai_sections_safe(proposal.dict())
    except Exception:
        logger.exception("AI core unexpectedly failed; using empty AI sections.")
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

    # --- Финансы ---
    fin = proposal.financials or {}
    dev = getattr(fin, "development_cost", 0)
    lic = getattr(fin, "licenses_cost", 0)
    sup = getattr(fin, "support_cost", 0)
    total = sum(float(x or 0) for x in [dev, lic, sup])

    # --- Контекст для шаблона ---
    context = {
        "current_date": _format_date(datetime.utcnow().date()),
        "client_company_name": proposal.client_name,
        "provider_company_name": proposal.provider_name,
        "expected_completion_date": _format_date(proposal.deadline),
        "development_cost": dev,
        "licenses_cost": lic,
        "support_cost": sup,
        "total_investment_cost": total,
        **ai_sections,
        # Подписи (с дефолтами)
        "client_signature_name": getattr(proposal, "client_signature_name", "________________"),
        "client_signature_date": _format_date(getattr(proposal, "client_signature_date", datetime.utcnow().date())),
        "provider_signature_name": getattr(proposal, "provider_signature_name", "________________"),
        "provider_signature_date": _format_date(getattr(proposal, "provider_signature_date", datetime.utcnow().date()))
    }

    # --- Deliverables ---
    deliverables_list = []
    for idx in range(4):  # поддерживаем 4 deliverables
        if proposal.deliverables and idx < len(proposal.deliverables):
            d = proposal.deliverables[idx]
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

    # --- Phases ---
    phases_list = []
    for idx in range(3):  # поддерживаем 3 фазы
        if proposal.phases and idx < len(proposal.phases):
            p = proposal.phases[idx]
            phases_list.append({
                "phase_name": f"Phase {idx+1}",
                "duration": f"{p.duration_weeks} weeks",
                "tasks": p.tasks
            })
            context[f"phase_{idx+1}_tasks"] = p.tasks
        else:
            context[f"phase_{idx+1}_tasks"] = ""
    context["phases_list"] = phases_list

    # --- Генерация документа ---
    try:
        docx_io: BytesIO = render_docx_from_template(TEMPLATE_PATH, context)
    except Exception as e:
        logger.exception("Document generation failed")
        raise HTTPException(status_code=500, detail=f"Document generation failed: {str(e)}")

    # --- Безопасное имя файла с кириллицей ---
    filename = f"proposal_{proposal.client_name}.docx"
    quoted_filename = quote(filename)
    content_disposition = f"attachment; filename*=UTF-8''{quoted_filename}"

    return StreamingResponse(
        docx_io,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": content_disposition}
    )
