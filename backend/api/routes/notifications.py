from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from backend.api.dependencies import get_task_notification_service, get_task_service
from backend.api.task_state import apply_task_state_etag, build_task_state
from backend.core.notifications import TaskNotificationService
from backend.core.tasks import ActiveTaskService


router = APIRouter()


class BindTaskNotificationRequest(BaseModel):
    delivery_node_id: str
    trigger: bool = True


@router.post("/task-notifications/{notification_id}/bind", response_model=Dict[str, Any])
async def bind_task_notification(
    notification_id: str,
    request: BindTaskNotificationRequest,
    response: Response,
    service: TaskNotificationService = Depends(get_task_notification_service),
    task_service: ActiveTaskService = Depends(get_task_service),
):
    try:
        notification = await service.bind(
            notification_id=notification_id,
            delivery_node_id=request.delivery_node_id,
            trigger=request.trigger,
        )
        state = await build_task_state(
            str(notification["conversation_id"]),
            task_service=task_service,
            notification_service=service,
        )
        apply_task_state_etag(response, state)
        return state
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Task notification 不存在: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/task-notifications/{notification_id}/delete", response_model=Dict[str, Any])
async def delete_task_notification(
    notification_id: str,
    response: Response,
    service: TaskNotificationService = Depends(get_task_notification_service),
    task_service: ActiveTaskService = Depends(get_task_service),
):
    try:
        notification = await service.delete(notification_id)
        state = await build_task_state(
            str(notification["conversation_id"]),
            task_service=task_service,
            notification_service=service,
        )
        apply_task_state_etag(response, state)
        return state
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Task notification 不存在: {exc}") from exc
