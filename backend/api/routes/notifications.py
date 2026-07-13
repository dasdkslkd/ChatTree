from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel

from backend.api.dependencies import get_task_notification_service
from backend.core.notifications import TaskNotificationService


router = APIRouter()


def _notifications_etag(notifications: List[Dict[str, Any]]) -> str:
    payload = json.dumps(
        sorted(notifications, key=lambda item: str(item.get("id") or "")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f'"{hashlib.sha256(payload).hexdigest()}"'


class BindTaskNotificationRequest(BaseModel):
    delivery_node_id: str
    trigger: bool = True


@router.get("/conversations/{conversation_id}/task-notifications", response_model=List[Dict[str, Any]])
async def list_task_notifications(
    conversation_id: str,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    service: TaskNotificationService = Depends(get_task_notification_service),
):
    notifications = service.list_for_conversation(conversation_id)
    etag = _notifications_etag(notifications)
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return notifications


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
