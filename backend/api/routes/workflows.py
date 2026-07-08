from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import get_workflow_manager
from backend.core.workflows import WorkflowManager

router = APIRouter()


class WorkflowValidateRequest(BaseModel):
    script: str


class WorkflowStartRequest(BaseModel):
    script: str
    args: Dict[str, Any] = {}
    parent_node_id: Optional[str] = None
    created_by_run_id: Optional[str] = None
    cancellation_parent_run_id: Optional[str] = None
    budget: Optional[Dict[str, Any]] = None


@router.post("/workflows/validate", response_model=Dict[str, Any])
async def validate_workflow(
    request: WorkflowValidateRequest,
    manager: WorkflowManager = Depends(get_workflow_manager),
):
    try:
        return manager.validate(request.script)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/conversations/{conversation_id}/workflows/runs", response_model=Dict[str, Any])
async def start_workflow(
    conversation_id: str,
    request: WorkflowStartRequest,
    manager: WorkflowManager = Depends(get_workflow_manager),
):
    try:
        return await manager.start(
            conversation_id=conversation_id,
            script=request.script,
            args=request.args,
            parent_node_id=request.parent_node_id,
            created_by_run_id=request.created_by_run_id,
            cancellation_parent_run_id=request.cancellation_parent_run_id,
            budget=request.budget,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/workflows/{run_id}/graph", response_model=Dict[str, Any])
async def get_workflow_graph(
    run_id: str,
    manager: WorkflowManager = Depends(get_workflow_manager),
):
    run = manager.run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow 不存在")
    events = manager.run_manager.read_events(run_id, 0)
    return {
        "run": run,
        "events": events,
    }
