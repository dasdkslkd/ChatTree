from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import get_task_notification_service
from backend.core.notifications import TaskNotificationService


router = APIRouter()


class BindTaskNotificationRequest(BaseModel):
    delivery_node_id: str
    trigger: bool = True


@router.get("/conversations/{conversation_id}/task-notifications", response_model=List[Dict[str, Any]])
async def list_task_notifications(
    conversation_id: str,
    service: TaskNotificationService = Depends(get_task_notification_service),
):
    return service.list_for_conversation(conversation_id)


@router.post("/task-notifications/{notification_id}/bind", response_model=Dict[str, Any])
async def bind_task_notification(
    notification_id: str,
    request: BindTaskNotificationRequest,
    service: TaskNotificationService = Depends(get_task_notification_service),
):
    try:
        return await service.bind(
            notification_id=notification_id,
            delivery_node_id=request.delivery_node_id,
            trigger=request.trigger,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Task notification 不存在: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/task-notifications/{notification_id}/delete", response_model=Dict[str, Any])
async def delete_task_notification(
    notification_id: str,
    service: TaskNotificationService = Depends(get_task_notification_service),
):
    try:
        return await service.delete(notification_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Task notification 不存在: {exc}") from exc
