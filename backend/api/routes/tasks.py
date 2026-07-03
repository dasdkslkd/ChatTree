from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import get_task_ledger
from backend.core.tasks import TaskLedger, TaskNotFoundError

router = APIRouter()


class CreateTaskRequest(BaseModel):
    title: str
    detail: Optional[str] = None


class UpdateTaskRequest(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    detail: Optional[str] = None
    evidence_summary: Optional[str] = None


@router.get("/conversations/{conversation_id}/tasks", response_model=list[Dict[str, Any]])
async def list_conversation_tasks(
    conversation_id: str,
    include_finished: bool = False,
    task_ledger: TaskLedger = Depends(get_task_ledger),
):
    tasks = await task_ledger.list_tasks(
        conversation_id,
        include_finished=include_finished,
    )
    return [task.to_dict() for task in tasks]


@router.post("/conversations/{conversation_id}/tasks", response_model=Dict[str, Any])
async def create_conversation_task(
    conversation_id: str,
    request: CreateTaskRequest,
    task_ledger: TaskLedger = Depends(get_task_ledger),
):
    try:
        task = await task_ledger.create_task(
            conversation_id=conversation_id,
            title=request.title,
            detail=request.detail or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return task.to_dict()


@router.patch("/conversations/{conversation_id}/tasks/{task_id}", response_model=Dict[str, Any])
async def update_conversation_task(
    conversation_id: str,
    task_id: str,
    request: UpdateTaskRequest,
    task_ledger: TaskLedger = Depends(get_task_ledger),
):
    try:
        task = await task_ledger.update_task(
            conversation_id=conversation_id,
            task_id=task_id,
            status=request.status,
            title=request.title,
            detail=request.detail,
            evidence_summary=request.evidence_summary,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return task.to_dict()
