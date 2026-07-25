from __future__ import annotations

import base64
import binascii
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from backend.api.dependencies import get_task_service
from backend.api.errors import ApiError
from backend.api.task_state import apply_task_state_etag, build_task_state
from backend.core.tasks import (
    ActiveTaskConflictError,
    ActiveTaskNotFoundError,
    ActiveTaskService,
    ActiveTaskVersionConflictError,
    TaskStepStatus,
)


router = APIRouter()


def _task_etag_parts(generation_id: str, revision: int) -> str:
    raw = f"{generation_id}:{revision}".encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f'"{token}"'


def _task_etag(task) -> str:
    return _task_etag_parts(task.generation_id, task.revision)


def _parse_task_etag(value: Optional[str]) -> tuple[str, int]:
    if not value:
        raise ApiError(
            428,
            "task_version_required",
            "If-Match task version is required",
            False,
        )
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


@router.get("/conversations/{conversation_id}/task-state", response_model=dict[str, Any])
async def get_task_state(
    conversation_id: str,
    response: Response,
    if_none_match: Optional[str] = Header(default=None, alias="If-None-Match"),
    task_service: ActiveTaskService = Depends(get_task_service),
):
    state = await build_task_state(
        conversation_id,
        task_service=task_service,
    )
    etag = f'"{state["version"]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    apply_task_state_etag(response, state)
    return state


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
    except ActiveTaskVersionConflictError as exc:
        raise ApiError(
            412,
            "task_version_conflict",
            "Task version changed",
            True,
            details={
                "current_version": _task_etag_parts(
                    exc.current_generation_id,
                    exc.current_revision,
                )
            },
        ) from exc
    except ActiveTaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    except ActiveTaskVersionConflictError as exc:
        raise ApiError(
            412,
            "task_version_conflict",
            "Task version changed",
            True,
            details={
                "current_version": _task_etag_parts(
                    exc.current_generation_id,
                    exc.current_revision,
                )
            },
        ) from exc
    except ActiveTaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"cancelled": cancelled}
