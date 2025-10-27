# backend/app/main.py  (замените/дополните ваш текущий main.py)
import logging
import os
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import ValidationError
from datetime import datetime, date, timedelta
from io import BytesIO
from urllib.parse import quote

from backend.app.ai_core import generate_ai_sections, generate_ai_sections_safe
from backend.app.doc_engine import render_docx_from_template
from backend.app.models import ProposalInput, Deliverable, Phase
from backend.app import db

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="AI Sales Proposal Generator (Backend)")

TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", r"D:\programming\ai-sales-proposal-generator\docs\template.docx")
if not os.path.exists(TEMPLATE_PATH):
    logger.warning("Template not found at %s. Please ensure template.docx is present.", TEMPLATE_PATH)

# init DB
db.init_db()

def _format_date(val) -> str:
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
    return "".join(c for c in (name or "") if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_") or "proposal"

# Additional server-side validation beyond Pydantic
def extra_validate_model(proposal: ProposalInput) -> Optional[Dict[str, Any]]:
    errors = []
    # deadline not in the past (allow today)
    if proposal.deadline:
        if isinstance(proposal.deadline, str):
            # Pydantic already converted, but safety
            try:
                d = date.fromisoformat(proposal.deadline)
            except Exception:
                d = None
        else:
            d = proposal.deadline
        if d and d < datetime.utcnow().date():
            errors.append({"loc": ["deadline"], "msg": "deadline must not be in the past", "type": "value_error"})
    # finances >= 0
    fin = proposal.financials
    if fin:
        for attr in ("development_cost", "licenses_cost", "support_cost"):
            val = getattr(fin, attr)
            if val is not None and val < 0:
                errors.append({"loc": [attr], "msg": "must be >= 0", "type": "value_error"})
    if errors:
        return {"detail": errors}
    return None

def _generate_gantt_bytes(phases: list) -> Optional[bytes]:
    """
    Create a simple Gantt chart using Plotly and return PNG bytes.
    phases: list of dicts with keys: phase_name, duration (e.g., '4 weeks') or duration_weeks, tasks
    """
    try:
        import plotly.express as px
        import pandas as pd
        # Normalize phases into start/end
        rows = []
        start = datetime.utcnow().date()
        for p in phases:
            duration_weeks = None
            if isinstance(p, dict) and "duration" in p:
                # expecting '4 weeks' format
                try:
                    duration_weeks = int(str(p["duration"]).split()[0])
                except Exception:
                    duration_weeks = 1
            elif hasattr(p, "duration_weeks"):
                duration_weeks = p.duration_weeks
            else:
                duration_weeks = 1
            end = start + timedelta(weeks=duration_weeks)
            rows.append({"Task": p.get("phase_name", "Phase"), "Start": start.isoformat(), "Finish": end.isoformat()})
            start = end  # next starts when previous ends
        if not rows:
            return None
        df = pd.DataFrame(rows)
        fig = px.timeline(df, x_start="Start", x_end="Finish", y="Task")
        fig.update_yaxes(autorange="reversed")
        # export to PNG bytes using kaleido
        img_bytes = fig.to_image(format="png", engine="kaleido")
        return img_bytes
    except Exception as e:
        logger.exception("Gantt generation failed: %s", e)
        return None

def _generate_uml_bytes(proposal: Dict[str, Any]) -> Optional[bytes]:
    """
    Create a very small PlantUML diagram using PLANTUML_SERVER_URL (env) if provided.
    Returns PNG bytes or None.
    """
    PLANTUML_SERVER = os.getenv("PLANTUML_SERVER_URL")
    if not PLANTUML_SERVER:
        return None
    # Compose small UML (sequence) based on deliverables/phases
    try:
        # Basic PlantUML text
        uml = "@startuml\ntitle Proposal overview\nactor Client\nparticipant Provider\nClient -> Provider: Request proposal\nProvider -> Provider: Prepare deliverables\nProvider -> Client: Deliver proposal\n@enduml"
        # PlantUML server expects encoded payload in URL path (but many accept POST)
        # We'll POST as plain text for servers that accept it
        import requests
        headers = {"Content-Type": "text/plain"}
        r = requests.post(PLANTUML_SERVER, data=uml.encode("utf-8"), headers=headers, timeout=15)
        if r.status_code == 200:
            return r.content
        else:
            logger.warning("PlantUML server returned status %s: %s", r.status_code, r.text)
            return None
    except Exception as e:
        logger.exception("UML generation failed: %s", e)
        return None

@app.post("/api/v1/generate-proposal")
async def generate_proposal(payload: Dict[str, Any]):
    # 1) Validate pydantic
    try:
        proposal = ProposalInput(**payload)
    except ValidationError as e:
        logger.warning("Validation failed: %s", e.json())
        return JSONResponse(status_code=422, content={"detail": e.errors()})

    # 1.5) extra validation
    extra = extra_validate_model(proposal)
    if extra:
        return JSONResponse(status_code=422, content=extra)

    # 2) AI generation
    used_model_info = "unknown"
    try:
        ai_sections = await generate_ai_sections(proposal.dict(), tone=getattr(proposal, "tone", "Formal"))
        if not isinstance(ai_sections, dict):
            raise RuntimeError("AI returned non-dict")
        used_model_info = "ai_generated"
    except Exception as exc:
        logger.exception("Primary AI generation failed: %s — falling back to safe.", exc)
        ai_sections = await generate_ai_sections_safe(proposal.dict())
        used_model_info = "fallback_safe"

    # 3) Financials
    fin = proposal.financials or {}
    dev = getattr(fin, "development_cost", 0) or 0
    lic = getattr(fin, "licenses_cost", 0) or 0
    sup = getattr(fin, "support_cost", 0) or 0
    total = sum(float(x or 0) for x in [dev, lic, sup])

    # 4) Gantt + UML
    phases_input = []
    for p in proposal.phases or []:
        phases_input.append({"phase_name": f"Phase {len(phases_input)+1}", "duration": f"{p.duration_weeks} weeks", "tasks": p.tasks})
    gantt_bytes = _generate_gantt_bytes(phases_input)
    uml_bytes = _generate_uml_bytes(payload)

    # 5) Build context
    payload_dict = proposal.dict() if hasattr(proposal, "dict") else {}
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
        "client_signature_name": payload_dict.get("client_signature_name", "") or "",
        "client_signature_date": _format_date(payload_dict.get("client_signature_date", None)),
        "provider_signature_name": payload_dict.get("provider_signature_name", "") or "",
        "provider_signature_date": _format_date(payload_dict.get("provider_signature_date", None)),
        "deliverables_list": [{"title": d.title, "description": d.description, "acceptance": d.acceptance_criteria} for d in (proposal.deliverables or [])],
        "phases_list": phases_input,
        "gantt_image": gantt_bytes,
        "uml_image": uml_bytes,
    }

    # 6) Render docx
    try:
        docx_io: BytesIO = render_docx_from_template(TEMPLATE_PATH, context)
    except Exception as e:
        logger.exception("Document generation failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Document generation failed: {str(e)}")

    # 7) Save version to DB (store payload and ai_sections and used_model)
    try:
        version_id = db.save_version(payload=payload, ai_sections=ai_sections, used_model=used_model_info, note="generated")
        logger.info("Saved proposal version id=%s", version_id)
    except Exception:
        logger.exception("Failed to save version to DB, continuing without versioning.")

    # 8) Streaming response
    filename_raw = f"proposal_{proposal.client_name or 'proposal'}.docx"
    quoted = quote(filename_raw, safe="")
    content_disposition = f"attachment; filename*=UTF-8''{quoted}"

    return StreamingResponse(
        docx_io,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": content_disposition, "X-Proposal-Version": str(version_id if 'version_id' in locals() else "")}
    )

@app.post("/proposal/regenerate")
async def regenerate(payload: Dict[str, Any] = Body(...)):
    """
    Regenerate endpoint.
    Accepts either:
      - {"version_id": 123}  -> fetch payload from DB and regenerate
      - or full payload same as /api/v1/generate-proposal to regenerate and create a new version
    Returns the generated docx (and sets X-Proposal-Version header).
    """
    version_id = payload.get("version_id")
    if version_id:
        try:
            version_id = int(version_id)
        except Exception:
            return JSONResponse(status_code=400, content={"detail": "version_id must be an integer"})
        v = db.get_version(version_id)
        if not v:
            return JSONResponse(status_code=404, content={"detail": "version not found"})
        regen_payload = v["payload"]
    else:
        regen_payload = payload

    # Reuse generation endpoint logic by calling generate_proposal helper path
    return await generate_proposal(regen_payload)
