from fastapi import APIRouter, HTTPException
from typing import Dict, Any
from backend.app.services.visualization_service import (
    generate_component_diagram,
    generate_dataflow_diagram,
    generate_deployment_diagram,
    generate_gantt_image,
)

router = APIRouter(prefix="/api/v1/visualization", tags=["Visualization"])

@router.post("/component-diagram")
async def component_diagram(data: Dict[str, Any]):
    try:
        image_bytes = generate_component_diagram(data)
        return {"status": "ok", "size": len(image_bytes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/dataflow-diagram")
async def dataflow_diagram(data: Dict[str, Any]):
    try:
        image_bytes = generate_dataflow_diagram(data)
        return {"status": "ok", "size": len(image_bytes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/deployment-diagram")
async def deployment_diagram(data: Dict[str, Any]):
    try:
        image_bytes = generate_deployment_diagram(data)
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
