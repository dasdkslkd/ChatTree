from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from fastapi import Response

from backend.core.notifications import TaskNotificationService
from backend.core.tasks import ActiveTaskService


def _task_state_version(payload: Dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def build_task_state(
    conversation_id: str,
    *,
    task_service: ActiveTaskService,
    notification_service: TaskNotificationService,
) -> Dict[str, Any]:
    task = await task_service.get_active_task(conversation_id)
    notifications = notification_service.list_for_conversation(conversation_id)
    task_payload = task.public_dict() if task is not None else None
    flags = {
        "running": task_payload is not None and task_payload.get("execution_state") in {"running", "stopping"},
        "delivering": any(item.get("status") == "delivering" for item in notifications),
        "needsFollowup": any(
            item.get("status") in {"unbound", "bound", "delivery_failed", "delivery_cancelled"}
            for item in notifications
        ),
    }
    payload: Dict[str, Any] = {
        "conversation_id": conversation_id,
        "task": task_payload,
        "notifications": notifications,
        "flags": flags,
    }
    payload["version"] = _task_state_version(payload)
    return payload


def apply_task_state_etag(response: Response, state: Dict[str, Any]) -> str:
    etag = f'"{state["version"]}"'
    response.headers["ETag"] = etag
    return etag
