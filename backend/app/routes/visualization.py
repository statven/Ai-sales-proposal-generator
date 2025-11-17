from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, JSONResponse
from typing import Dict, Any, Optional
import logging

from backend.app.services.visualization_service import (
    generate_gantt_image,
    generate_lifecycle_diagram,
)

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/v1/visualization", tags=["Visualization"])


@router.post("/gantt")
async def gantt(data: Dict[str, Any], agent_mode: Optional[bool] = Query(False, description="If true, use agent enrichment for schedule"), width: Optional[int] = Query(1400, description="Image width in px")):
    """
    Generate Gantt chart and return a small JSON summary.
    Keeps backward-compatible shape: returns {"status":"ok","size": <bytes_len>}
    Accepts query params:
      - agent_mode (bool): if True, run agent_enrich_schedule to normalize/synthesize phases
      - width (int): rendering width in pixels
    """
    try:
        image_bytes = generate_gantt_image(data, width=width, agent_mode=bool(agent_mode))
        return {"status": "ok", "size": len(image_bytes)}
    except Exception as e:
        logger.exception("Gantt generation failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gantt/png")
async def gantt_png(data: Dict[str, Any], agent_mode: Optional[bool] = Query(False), width: Optional[int] = Query(2000)):
    """
    Generate Gantt chart and return raw PNG image (Content-Type: image/png).
    Useful for direct display in browser or embedding.
    """
    try:
        image_bytes = generate_gantt_image(data, width=width, agent_mode=bool(agent_mode))
        return Response(content=image_bytes, media_type="image/png")
    except Exception as e:
        logger.exception("Gantt PNG generation failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/lifecycle")
async def lifecycle(data: Dict[str, Any], width: Optional[int] = Query(1400, description="Image width in px"), height: Optional[int] = Query(None, description="Image height in px")):
    """
    Generate Development Lifecycle diagram and return JSON summary (size).
    """
    try:
        image_bytes = generate_lifecycle_diagram(data, width=width, height=height)
        return {"status": "ok", "size": len(image_bytes)}
    except Exception as e:
        logger.exception("Lifecycle generation failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/lifecycle/png")
async def lifecycle_png(data: Dict[str, Any], width: Optional[int] = Query(1400), height: Optional[int] = Query(None)):
    """
    Generate Development Lifecycle diagram and return raw PNG image.
    """
    try:
        image_bytes = generate_lifecycle_diagram(data, width=width, height=height)
        return Response(content=image_bytes, media_type="image/png")
    except Exception as e:
        logger.exception("Lifecycle PNG generation failed")
        raise HTTPException(status_code=500, detail=str(e))


# simple health/check endpoint for visualization router
@router.get("/health")
async def health():
    return JSONResponse({"status": "ok", "service": "visualization"})
