# backend/app/routes/visualization.py
from fastapi import APIRouter, Response, HTTPException
from pydantic import BaseModel
from typing import Any, Dict
import logging

from backend.app.services.visualization_service import generate_uml_image, generate_gantt_image

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/visualize", tags=["visualize"])

class ProposalModel(BaseModel):
    __root__: Dict[str, Any]

@router.post("/uml", response_class=Response)
def visualize_uml(proposal: ProposalModel):
    try:
        img = generate_uml_image(proposal.__root__)
        return Response(content=img, media_type="image/png")
    except Exception as e:
        logger.exception("visualize_uml failed: %s", e)
        raise HTTPException(status_code=500, detail="UML generation error")

@router.post("/gantt", response_class=Response)
def visualize_gantt(proposal: ProposalModel):
    try:
        img = generate_gantt_image(proposal.__root__)
        return Response(content=img, media_type="image/png")
    except Exception as e:
        logger.exception("visualize_gantt failed: %s", e)
        raise HTTPException(status_code=500, detail="Gantt generation error")
