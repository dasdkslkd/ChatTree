from __future__ import annotations

from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.dependencies import get_workflow_manager
from backend.api.run_start import (
    RunStartResponse,
    require_idempotency_key,
    run_start_api_error,
    run_start_openapi_responses,
    run_start_response,
)
from backend.core.runs import (
    RunIdempotency,
    RunIdempotencyConflictError,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunRequestFingerprintError,
    RunStartReservationError,
    RunStartSchedulingError,
    RunStartValidationError,
    fingerprint_run_request,
)
from backend.core.workflows import WorkflowManager, normalize_workflow_budget
from backend.core.runs.public import public_run_dict

router = APIRouter()


class WorkflowValidateRequest(BaseModel):
    script: str


class WorkflowStartRequest(BaseModel):
    script: str
    args: Dict[str, Any] = Field(default_factory=dict)
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


@router.post(
    "/conversations/{conversation_id}/workflows/runs",
    response_model=RunStartResponse,
    responses=run_start_openapi_responses(),
)
async def start_workflow(
    conversation_id: str,
    body: WorkflowStartRequest,
    http_request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    manager: WorkflowManager = Depends(get_workflow_manager),
):
    try:
        normalized_budget = normalize_workflow_budget(body.budget)
        manager.run_manager.validate_run_references(
            conversation_id,
            anchor_node_id=body.parent_node_id,
            created_by_run_id=body.created_by_run_id,
            cancellation_parent_run_id=body.cancellation_parent_run_id,
        )
        request_payload = body.model_dump(mode="json")
        request_payload["budget"] = normalized_budget
        idempotency = RunIdempotency(
            key=idempotency_key,
            request_fingerprint=fingerprint_run_request(
                operation="workflow",
                conversation_id=conversation_id,
                anchor_node_id=body.parent_node_id,
                payload={"request": request_payload},
            ),
        )
        result = await manager.start_idempotent(
            conversation_id=conversation_id,
            script=body.script,
            args=body.args,
            parent_node_id=body.parent_node_id,
            created_by_run_id=body.created_by_run_id,
            cancellation_parent_run_id=body.cancellation_parent_run_id,
            budget=normalized_budget,
            idempotency=idempotency,
            request_id=http_request.state.request_id,
        )
        return run_start_response(result)
    except (
        RunRequestFingerprintError,
        RunReferenceNotFoundError,
        RunReferenceConversationMismatchError,
        RunIdempotencyConflictError,
        RunStartReservationError,
        RunStartSchedulingError,
        RunStartValidationError,
    ) as exc:
        raise run_start_api_error(exc) from exc


@router.get("/workflows/{run_id}/graph", response_model=Dict[str, Any])
async def get_workflow_graph(
    run_id: str,
    manager: WorkflowManager = Depends(get_workflow_manager),
):
    run = manager.run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Workflow 不存在")
    return {
        "run": public_run_dict(run),
    }
