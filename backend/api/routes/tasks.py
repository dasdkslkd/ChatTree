from __future__ import annotations

import base64
import binascii
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from backend.api.dependencies import get_task_service
from backend.core.tasks import (
    ActiveTaskConflictError,
    ActiveTaskNotFoundError,
    ActiveTaskService,
    TaskStepStatus,
)


router = APIRouter()


def _task_etag(task) -> str:
    raw = f"{task.generation_id}:{task.revision}".encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f'"{token}"'


def _parse_task_etag(value: Optional[str]) -> tuple[str, int]:
    if not value:
        raise HTTPException(status_code=428, detail="If-Match task version is required")
    token = value.strip()
    if token.startswith("W/"):
        raise HTTPException(status_code=400, detail="weak task versions are not supported")
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        generation_id, revision_text = decoded.rsplit(":", 1)
        revision = int(revision_text)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid task version") from exc
    if not generation_id or revision < 0:
        raise HTTPException(status_code=400, detail="invalid task version")
    return generation_id, revision


def _task_conflict_status(exc: ActiveTaskConflictError) -> int:
    message = str(exc)
    if "generation changed" in message or "revision changed" in message:
        return 412
    return 409


class TaskStepRequest(BaseModel):
    title: str
    detail: Optional[str] = None


class CreateTaskRequest(BaseModel):
    title: str
    detail: Optional[str] = None
    steps: list[TaskStepRequest] = Field(default_factory=list)


class SetTaskStepRequest(BaseModel):
    status: str
    evidence: str


class CancelTaskRequest(BaseModel):
    reason: str


@router.get("/conversations/{conversation_id}/task", response_model=Optional[dict[str, Any]])
async def get_active_task(
    conversation_id: str,
    response: Response,
    task_service: ActiveTaskService = Depends(get_task_service),
):
    task = await task_service.get_active_task(conversation_id)
    if task is not None:
        response.headers["ETag"] = _task_etag(task)
    return task.public_dict() if task is not None else None


@router.post("/conversations/{conversation_id}/task", response_model=dict[str, Any])
async def create_active_task(
    conversation_id: str,
    request: CreateTaskRequest,
    response: Response,
    task_service: ActiveTaskService = Depends(get_task_service),
):
    try:
        task = await task_service.create_task(
            conversation_id=conversation_id,
            title=request.title,
            detail=request.detail or "",
            steps=[step.model_dump() for step in request.steps],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ActiveTaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.headers["ETag"] = _task_etag(task)
    return task.public_dict()


@router.patch(
    "/conversations/{conversation_id}/task/steps/{step}",
    response_model=dict[str, Any],
)
async def set_active_task_step(
    conversation_id: str,
    step: int,
    request: SetTaskStepRequest,
    response: Response,
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    task_service: ActiveTaskService = Depends(get_task_service),
):
    current = await task_service.get_active_task(conversation_id)
    if current is None:
        raise HTTPException(status_code=404, detail="active task or step not found")
    expected_generation, expected_revision = _parse_task_etag(if_match)
    try:
        result = await task_service.set_step_result(
            conversation_id=conversation_id,
            step=step,
            status=TaskStepStatus(request.status),
            evidence_summary=request.evidence,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
        )
    except ActiveTaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="active task or step not found") from exc
    except ActiveTaskConflictError as exc:
        raise HTTPException(status_code=_task_conflict_status(exc), detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.task is not None:
        response.headers["ETag"] = _task_etag(result.task)
    return result.public_dict()


@router.delete("/conversations/{conversation_id}/task", response_model=dict[str, Any])
async def cancel_active_task(
    conversation_id: str,
    request: CancelTaskRequest,
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    task_service: ActiveTaskService = Depends(get_task_service),
):
    current = await task_service.get_active_task(conversation_id)
    if current is None:
        raise HTTPException(status_code=404, detail="active task not found")
    expected_generation, expected_revision = _parse_task_etag(if_match)
    try:
        cancelled = await task_service.cancel_task(
            conversation_id=conversation_id,
            reason=request.reason,
            expected_generation=expected_generation,
            expected_revision=expected_revision,
        )
    except ActiveTaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="active task not found") from exc
    except ActiveTaskConflictError as exc:
        raise HTTPException(status_code=_task_conflict_status(exc), detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"cancelled": cancelled}
