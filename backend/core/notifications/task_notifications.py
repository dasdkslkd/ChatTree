from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from backend.core.runs import FINISHED_RUN_STATUSES, RunKind, RunManager, RunStatus

logger = logging.getLogger(__name__)

TERMINAL_STATUS_VALUES = {status.value for status in FINISHED_RUN_STATUSES}
VISIBLE_NOTIFICATION_STATUSES = {"unbound", "bound", "delivering"}


def format_task_notification_content(notification: dict[str, Any]) -> str:
    payload = {
        "kind": "task_notification",
        "summary": notification.get("summary") or "",
        "source_run_id": notification.get("source_run_id"),
        "source_run_kind": notification.get("source_run_kind"),
        "task_id": notification.get("task_id"),
        **dict(notification.get("payload") or {}),
        "content": notification.get("content") or "",
    }
    return "<task-notification>\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n</task-notification>"


class TaskNotificationService:
    def __init__(
        self,
        *,
        repository: Any,
        run_manager: RunManager,
        chat_manager: Any = None,
    ) -> None:
        self.repository = repository
        self.run_manager = run_manager
        self.chat_manager = chat_manager
        self._delivering: set[str] = set()

    async def publish_run_notification(
        self,
        *,
        run_id: str,
        source_status: str,
        summary: str,
        content: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        run = self.run_manager.get_run(run_id)
        if not run:
            return None
        metadata = dict(run.get("metadata") or {})
        if metadata.get("result_observed_at"):
            self.repository.mark_observed_by_source(run_id)
            return None
        if task_id is None:
            task_id = metadata.get("task_id")
        notification = self.repository.upsert_for_run(
            conversation_id=str(run["conversation_id"]),
            source_run_id=run_id,
            source_run_kind=str(run.get("kind") or ""),
            task_id=task_id,
            summary=summary,
            content=content,
            payload={
                "source_status": source_status,
                **dict(payload or {}),
            },
        )
        if notification.get("status") == "bound":
            delivered = await self.try_deliver(str(notification["id"]))
            if delivered is not None:
                return delivered
        return notification

    async def register_run_notification(
        self,
        *,
        run_id: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        run = self.run_manager.get_run(run_id)
        if not run:
            return None
        metadata = dict(run.get("metadata") or {})
        if metadata.get("result_observed_at"):
            return None
        if task_id is None:
            task_id = metadata.get("task_id")
        return self.repository.upsert_for_run(
            conversation_id=str(run["conversation_id"]),
            source_run_id=run_id,
            source_run_kind=str(run.get("kind") or ""),
            task_id=task_id,
            summary=summary,
            content="",
            payload={
                "source_status": "running",
                **dict(payload or {}),
            },
        )

    def mark_observed_for_run(self, run_id: str) -> dict[str, Any] | None:
        return self.repository.mark_observed_by_source(run_id)

    def list_for_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.repository.list_for_conversation(conversation_id)
            if item.get("status") in VISIBLE_NOTIFICATION_STATUSES
        ]

    async def bind(
        self,
        *,
        notification_id: str,
        delivery_node_id: str,
        bound_by: str = "user",
        trigger: bool = True,
        focus_new_node: bool = False,
    ) -> dict[str, Any]:
        notification = self.repository.get(notification_id)
        if notification is None:
            raise KeyError(notification_id)
        self._validate_delivery_node(notification, delivery_node_id)
        bound = self.repository.bind(notification_id, delivery_node_id, bound_by=bound_by)
        if trigger:
            delivered = await self.try_deliver(notification_id, focus_new_node=focus_new_node)
            if delivered is not None:
                return delivered
        return bound

    async def try_deliver(self, notification_id: str, *, focus_new_node: bool = False) -> dict[str, Any] | None:
        notification = self.repository.get(notification_id)
        if notification is None:
            return None
        if notification.get("status") != "bound":
            return notification
        source_run = self.run_manager.get_run(str(notification.get("source_run_id") or ""))
        if not source_run or str(source_run.get("status") or "") not in TERMINAL_STATUS_VALUES:
            return notification
        if (source_run.get("metadata") or {}).get("result_observed_at"):
            return self.repository.mark_observed_by_source(str(source_run["run_id"]))
        conversation_id = str(notification.get("conversation_id") or "")
        delivery_node_id = str(notification.get("delivery_node_id") or "")
        if not delivery_node_id or not self.is_node_idle(conversation_id, delivery_node_id):
            return notification
        if notification_id in self._delivering:
            return notification
        self._delivering.add(notification_id)
        try:
            run = await self._start_notification_chat_run(notification, focus_new_node=focus_new_node)
            return self.repository.mark_delivering(notification_id, str(run["run_id"]))
        finally:
            self._delivering.discard(notification_id)

    async def try_deliver_bound_for_conversation(self, conversation_id: str) -> None:
        for notification in self.repository.list_bound(conversation_id):
            await self.try_deliver(str(notification["id"]))

    def is_node_idle(self, conversation_id: str, node_id: str) -> bool:
        for run in self.run_manager.list_active(conversation_id):
            if run.get("kind") == RunKind.CHAT.value and (
                run.get("anchor_node_id") == node_id or run.get("target_node_id") == node_id
            ):
                return False
        controllers = getattr(self.chat_manager, "_active_controllers", {}) if self.chat_manager is not None else {}
        if node_id in controllers:
            return False
        return not self.repository.list_bound_for_node(conversation_id, node_id)

    def handle_run_finished(self, run: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        conversation_id = str(run.get("conversation_id") or "")
        run_id = str(run.get("run_id") or "")
        notification = self.repository.get_by_source_run(run_id)
        if notification and notification.get("status") == "bound":
            loop.create_task(self.try_deliver(str(notification["id"])))
        if run.get("kind") == RunKind.CHAT.value and conversation_id:
            loop.create_task(self.try_deliver_bound_for_conversation(conversation_id))

    async def delete(self, notification_id: str) -> dict[str, Any]:
        return self.repository.delete(notification_id)

    async def _start_notification_chat_run(
        self,
        notification: dict[str, Any],
        *,
        focus_new_node: bool,
    ) -> dict[str, Any]:
        if self.chat_manager is None:
            raise RuntimeError("TaskNotificationService requires chat_manager to deliver notifications")
        conversation_id = str(notification["conversation_id"])
        delivery_node_id = str(notification["delivery_node_id"])
        content = format_task_notification_content(notification)
        run = await self.run_manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=delivery_node_id,
            summary=notification.get("summary") or "Task notification",
            metadata={
                "origin": "task_notification",
                "notification_id": notification["id"],
                "source_run_id": notification["source_run_id"],
                "source_run_kind": notification["source_run_kind"],
            },
        )

        async def produce() -> None:
            final_status = RunStatus.COMPLETED
            final_error: str | None = None
            delivered_node_id: str | None = None
            try:
                async for chunk in self.chat_manager.send_message_stream(
                    conversation_id=conversation_id,
                    content=content,
                    parent_node_id=delivery_node_id,
                    focus_new_node=focus_new_node,
                    message_subtype="task_notification",
                    run_id=run.run_id,
                ):
                    chunk_data = dict(chunk)
                    if chunk_data.get("node_id"):
                        delivered_node_id = str(chunk_data["node_id"])
                        await self.run_manager.bind_target_node(run.run_id, delivered_node_id)
                    await self.run_manager.append_event(run.run_id, chunk_data)
                    if chunk_data.get("status") == "error":
                        final_status = RunStatus.FAILED
                        final_error = chunk_data.get("error")
                    elif chunk_data.get("status") == "stopped":
                        final_status = RunStatus.CANCELLED
            except Exception as exc:
                logger.exception("Task notification delivery failed")
                final_status = RunStatus.FAILED
                final_error = str(exc)
                await self.run_manager.append_event(run.run_id, {
                    "status": "error",
                    "content": "",
                    "conversation_id": conversation_id,
                    "run_id": run.run_id,
                    "error": final_error,
                })
            finally:
                await self.run_manager.finish_run(run.run_id, final_status, final_error)
                self.repository.mark_delivered(
                    str(notification["id"]),
                    delivered_run_id=run.run_id,
                    delivered_node_id=delivered_node_id,
                )

        asyncio.create_task(produce())
        return run.to_dict()

    def _validate_delivery_node(self, notification: dict[str, Any], delivery_node_id: str) -> None:
        if self.chat_manager is None:
            return
        conversation = self.chat_manager.get_conversation(str(notification["conversation_id"]))
        if conversation is None:
            raise KeyError(str(notification["conversation_id"]))
        if delivery_node_id not in conversation.nodes:
            raise KeyError(delivery_node_id)
