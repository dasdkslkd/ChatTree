from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.api.dependencies import get_task_notification_service, get_transcript_assembler
from backend.core.notifications import TaskNotificationService, TaskNotificationTransitionError
from backend.core.transcript import TranscriptAssembler


router = APIRouter()


class TaskNotificationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_id: str
    source_run_kind: str = ""
    summary: str = ""
    content: str = ""
    notification_id: str | None = None


class TaskNotificationBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_node_id: str


def _notification_node_id(notification: dict[str, Any]) -> str | None:
    return (
        notification.get("delivery_node_id")
        or notification.get("anchor_node_id")
        or notification.get("target_node_id")
    )


def _notification_patch(
    assembler: TranscriptAssembler,
    conversation_id: str,
    notification: dict[str, Any],
) -> dict[str, Any]:
    node_id = _notification_node_id(notification)
    item_id = f"task-notification:{notification['id']}"
    item = None
    if node_id:
        try:
            snapshot = assembler.snapshot(conversation_id, str(node_id))
        except KeyError:
            snapshot = None
        if snapshot:
            item = next(
                (candidate for candidate in snapshot["items"] if candidate.get("id") == item_id),
                None,
            )
    return {
        "type": "transcript_patch",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "revision": assembler.next_revision(conversation_id, node_id),
        "operations": [{"op": "upsert", "item": item}] if item is not None else [],
    }


@router.get("/conversations/{conversation_id}/task-notifications")
async def list_task_notifications(
    conversation_id: str,
    service: TaskNotificationService = Depends(get_task_notification_service),
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "notifications": service.list(conversation_id),
    }


@router.post("/conversations/{conversation_id}/task-notifications")
async def create_task_notification(
    conversation_id: str,
    body: TaskNotificationCreateRequest,
    service: TaskNotificationService = Depends(get_task_notification_service),
) -> dict[str, Any]:
    try:
        return {
            "notification": service.create(
                conversation_id=conversation_id,
                source_run_id=body.source_run_id,
                source_run_kind=body.source_run_kind,
                summary=body.summary,
                content=body.content,
                notification_id=body.notification_id,
            )
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="notification source not found") from exc
    except TaskNotificationTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/task-notifications/{notification_id}/bind")
async def bind_task_notification(
    conversation_id: str,
    notification_id: str,
    body: TaskNotificationBindRequest,
    service: TaskNotificationService = Depends(get_task_notification_service),
    assembler: TranscriptAssembler = Depends(get_transcript_assembler),
) -> dict[str, Any]:
    try:
        notification = service.bind(
            conversation_id,
            notification_id,
            body.delivery_node_id,
        )
        return {
            "notification": notification,
            "transcript_patch": _notification_patch(assembler, conversation_id, notification),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="notification not found") from exc
    except TaskNotificationTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/conversations/{conversation_id}/task-notifications/{notification_id}")
async def delete_task_notification(
    conversation_id: str,
    notification_id: str,
    service: TaskNotificationService = Depends(get_task_notification_service),
    assembler: TranscriptAssembler = Depends(get_transcript_assembler),
) -> dict[str, Any]:
    try:
        notification = service.delete(conversation_id, notification_id)
        return {
            "notification": notification,
            "transcript_patch": _notification_patch(assembler, conversation_id, notification),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="notification not found") from exc
    except TaskNotificationTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
