from fastapi import APIRouter, HTTPException
from typing import Dict, Any
from backend.app.services.visualization_service import (
    generate_uml_diagram,
    generate_gantt_image,
)

router = APIRouter(prefix="/api/v1/visualization", tags=["Visualization"])

@router.post("/uml")
async def uml(data: Dict[str, Any]):
    try:
        image_bytes = generate_uml_image(data)
        return {"status": "ok", "size": len(image_bytes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gantt")
async def gantt(data: Dict[str, Any]):
    try:
        image_bytes = generate_gantt_image(data)
        return {"status": "ok", "size": len(image_bytes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
