import logging
import os
import json 
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import ValidationError
from datetime import datetime, date
from io import BytesIO
from urllib.parse import quote

# ----------------------------------------------------
# ЗАГЛУШКИ ДЛЯ ИМПОРТОВ: 
# Предполагается, что эти модули существуют в вашем проекте
try:
    from backend.app.services import openai_service 
except ImportError:
    # ... (пропуск кода заглушки)
    openai_service = None
    logging.warning("openai_service not found. AI generation disabled.")

try:
    from backend.app.doc_engine import render_docx_from_template
except ImportError:
    render_docx_from_template = None
    logging.warning("doc_engine not found. DOCX generation disabled.")

try:
    from backend.app.models import ProposalInput, Financials
except ImportError:
    # ... (пропуск кода заглушки)
    class Financials: pass 
    class ProposalInput: 
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
        def dict(self):
            return self.__dict__
    logging.warning("models.py not found. Using minimal model structure.")


try:
    from backend.app import db
except ImportError:
    # ... (пропуск кода заглушки)
    class MockDB:
        def init_db(self): pass
        def save_version(self, *args, **kwargs): return 1
        def get_version(self, version_id): 
            return {'payload': '{"client_company_name": "Test Client", "provider_company_name": "Test Provider", "project_goal": "Goal", "scope": "Scope", "technologies": [], "deadline": "2025-12-31", "tone": "Formal", "proposal_date": "2025-01-01", "valid_until_date": "2025-01-31", "financials": {"development_cost": 1000.0, "licenses_cost": 0.0, "support_cost": 0.0}, "deliverables": [], "phases": []}',
                    'ai_sections': '{}'}
    db = MockDB()
    logging.warning("db.py not found. Using mock database.")

try:
    from backend.app import ai_core
except ImportError:
    # ... (пропуск кода заглушки)
    class MockAICore:
        def generate_ai_sections(self, data): 
            return {"executive_summary_text": "AI summary placeholder.", "used_model": "mock-llm"}
    ai_core = MockAICore()
    logging.warning("ai_core.py not found. Using mock AI core.")

# ----------------------------------------------------

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="AI Sales Proposal Generator (Backend)")

TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", os.path.join(os.getcwd(), "docs", "template.docx"))
if not os.path.exists(TEMPLATE_PATH):
    logger.warning("Template not found at %s. Ensure template.docx is present.", TEMPLATE_PATH)

db.init_db()

# --- Вспомогательные функции ---

def _format_date(val: Any) -> str:
    """Formats date/datetime object to ISO string."""
    if val is None:
        return ""
    if isinstance(val, date):
        return val.isoformat() 
    if isinstance(val, str):
        return val
    return str(val)

def _safe_filename(name: str) -> str:
    """Sanitize string for use in a filename."""
    return "".join(c for c in name if c.isalnum() or c in (' ', '_', '-')).rstrip().replace(' ', '_')[:50]

def _calculate_total_investment(financials_dict: Dict[str, Any]) -> float:
    """Calculates the sum of development, licenses, and support costs."""
    dev = financials_dict.get('development_cost')
    lic = financials_dict.get('licenses_cost')
    sup = financials_dict.get('support_cost')
    
    development_cost = float(dev) if dev is not None else 0.0
    licenses_cost = float(lic) if lic is not None else 0.0
    support_cost = float(sup) if sup is not None else 0.0
    
    return development_cost + licenses_cost + support_cost

def _prepare_list_data(context: Dict[str, Any]) -> None:
    """
    Исправляет несоответствие ключей между Pydantic моделями и doc_engine.py.
    """
    # 1. Deliverables: 'deliverables' -> 'deliverables_list' & 'acceptance_criteria' -> 'acceptance'
    if 'deliverables' in context:
        deliverables = context.pop('deliverables')
        # ПЕРЕИМЕНОВАНИЕ КЛЮЧА
        for d in deliverables:
            if 'acceptance_criteria' in d:
                d['acceptance'] = d.pop('acceptance_criteria')
        context['deliverables_list'] = deliverables

    # 2. Phases: 'phases' -> 'phases_list' & 'duration_weeks' -> 'duration'
    if 'phases' in context:
        phases = context.pop('phases')
        # ПЕРЕИМЕНОВАНИЕ КЛЮЧА
        for p in phases:
            if 'duration_weeks' in p:
                p['duration'] = p.pop('duration_weeks')
        context['phases_list'] = phases


# --- Функции генерации и регенерации ---

@app.post("/api/v1/generate-proposal", tags=["Proposal Generation"])
async def generate_proposal(proposal: ProposalInput = Body(...)):
    """
    Generates the DOCX proposal document using a template and LLM-generated content.
    """
    if not render_docx_from_template:
          raise HTTPException(status_code=503, detail="Document engine is not available.")

    # 1. Generate AI sections
    try:
        ai_sections = await ai_core.generate_ai_sections(proposal.dict())
    except Exception as e:
        logger.exception("AI generation failed: %s", e)
        ai_sections = {}

    # 2. Build rendering context
    context = proposal.dict()
    context.update(ai_sections)
    
    # === ИСПРАВЛЕНИЕ ТАБЛИЦ: Выравнивание имен ключей ===
    _prepare_list_data(context)
    # ==========================================
    
    # Flatten dates
    context['current_date'] = _format_date(date.today())
    context['expected_completion_date'] = _format_date(context.get('deadline'))
    
    # Prepare financials and CALCULATE TOTAL
    if context.get("financials"):
        fin_dict = context["financials"]
        # Перенос всех финансовых полей на верхний уровень context
        context.update(fin_dict)
        
        total_investment_cost = _calculate_total_investment(fin_dict)
        context['total_investment_cost'] = total_investment_cost
    
    # 3. Render DOCX
    try:
        doc_bytes = render_docx_from_template(
            template_path=TEMPLATE_PATH,
            context=context
        )
    except Exception as e:
        logger.exception("DOCX rendering failed: %s", e)
        return JSONResponse(status_code=500, content={"detail": f"DOCX rendering failed: {e}"})

    # 4. Save to DB 
    version_id = None
    try:
        version_id = db.save_version(
            proposal.dict(), 
            ai_sections=ai_sections, 
            used_model=ai_sections.get("used_model")
        )
    except Exception as e:
        logger.exception("Failed to save proposal to DB: %s", e)
    
    # 5. Return file
    filename = f"{_safe_filename(context.get('client_company_name') or 'proposal')}_{_safe_filename(context.get('project_goal') or 'doc')}.docx"
    encoded_filename = quote(filename)

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
    }
    if version_id is not None:
          headers["X-Proposal-Version"] = str(version_id)

    return StreamingResponse(
        BytesIO(doc_bytes.getvalue()),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers
    )

@app.post("/proposal/regenerate", tags=["Proposal Generation"])
async def regenerate_proposal(version_data: Dict[str, int]):
    """
    Regenerates a DOCX file from a previously saved version ID.
    """
    if not render_docx_from_template:
          raise HTTPException(status_code=503, detail="Document engine is not available.")

    version_id = version_data.get("version_id")
    if version_id is None:
        raise HTTPException(status_code=400, detail="version_id is required")

    version_record = db.get_version(version_id)
    if not version_record:
        raise HTTPException(status_code=404, detail=f"Version {version_id} not found")

    try:
        # 1. Load data
        proposal_payload = json.loads(version_record['payload'])
        ai_sections = json.loads(version_record['ai_sections'])
        
        # 2. Rebuild context
        context = proposal_payload
        context.update(ai_sections)

        # === ИСПРАВЛЕНИЕ ТАБЛИЦ: Выравнивание имен ключей ===
        _prepare_list_data(context)
        # ==========================================
        
        context['current_date'] = _format_date(date.today())
        context['expected_completion_date'] = _format_date(context.get('deadline'))

        # Re-calculate total investment for regenerated version
        if context.get("financials"):
            fin_dict = context["financials"]
            context.update(fin_dict)
            context['total_investment_cost'] = _calculate_total_investment(fin_dict)
        
        # 3. Render DOCX
        doc_bytes = render_docx_from_template(
            template_path=TEMPLATE_PATH,
            context=context
        )

        # 4. Return file
        filename = f"Regen_V{version_id}_{_safe_filename(context.get('client_company_name') or 'proposal')}.docx"
        encoded_filename = quote(filename)

        return StreamingResponse(
            BytesIO(doc_bytes.getvalue()),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
                "X-Proposal-Version": str(version_id)
            }
        )

    except Exception as e:
        logger.exception("Regeneration failed for version %s: %s", version_id, e)
        raise HTTPException(status_code=500, detail=f"Regeneration failed: {e}")

@app.post("/api/v1/suggest", tags=["AI Suggestions"])
async def suggest_content(proposal: ProposalInput = Body(...)):
    """
    Suggests deliverables and phases based on the proposal brief.
    """
    if not openai_service:
         return JSONResponse(status_code=503, content={"detail": "AI suggestion service is not available."})
    
    try:
        suggestions = openai_service.generate_suggestions(proposal.dict()) 
        return suggestions
    except Exception as e:
        logger.exception("Suggestion generation failed: %s", e)
        return JSONResponse(status_code=500, content={"detail": "Suggestion generation failed."})

# ------------------- Placeholder for Gantt generation -------------------
def _generate_gantt_bytes(phases: List[Dict[str, Any]]):
    """
    Placeholder for Gantt chart generation.
    """
    return None
