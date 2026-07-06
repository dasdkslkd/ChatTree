from __future__ import annotations

import asyncio
import hashlib
import uuid
from copy import deepcopy
from time import time
from typing import Any, Callable, Dict, Optional

from .types import AgentDeliveryPolicy, AgentMailboxMessage, AgentMailboxMessageType


class AgentMailbox:
    """In-memory notification delivery state for agent results."""

    def __init__(self) -> None:
        self._messages: Dict[str, AgentMailboxMessage] = {}
        self._by_conversation: Dict[str, list[str]] = {}
        self._dedupe: Dict[tuple[str, str, str], str] = {}
        self._pending_listener: Optional[Callable[[str], None]] = None
        self._lock = asyncio.Lock()

    def set_pending_listener(self, listener: Optional[Callable[[str], None]]) -> None:
        self._pending_listener = listener

    async def publish(
        self,
        *,
        conversation_id: str,
        source_run_id: str,
        source_run_kind: str,
        message_type: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        delivery_policy: str = "auto",
    ) -> AgentMailboxMessage:
        msg_type = AgentMailboxMessageType(str(message_type))
        policy = AgentDeliveryPolicy(str(delivery_policy or "auto"))
        dedupe_key = self._dedupe_key(source_run_id, msg_type.value, content, metadata or {})
        notify = False
        async with self._lock:
            existing_id = self._dedupe.get(dedupe_key)
            if existing_id and existing_id in self._messages:
                return deepcopy(self._messages[existing_id])

            message_id = f"agentmsg_{uuid.uuid4().hex}"
            message = AgentMailboxMessage(
                message_id=message_id,
                conversation_id=conversation_id,
                source_run_id=source_run_id,
                source_run_kind=source_run_kind,
                message_type=msg_type,
                content=content,
                metadata=dict(metadata or {}),
                delivery_policy=policy,
            )
            if self._should_notify(message):
                message.notification_enqueued_at = time()
                notify = True
            self._messages[message_id] = message
            self._dedupe[dedupe_key] = message_id
            self._by_conversation.setdefault(conversation_id, []).append(message_id)
        if notify and self._pending_listener is not None:
            self._pending_listener(conversation_id)
        return deepcopy(message)

    async def list_pending_notifications(self, conversation_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                deepcopy(message).to_dict()
                for message in self._conversation_messages_locked(conversation_id)
                if self._is_pending_notification(message)
            ]

    async def claim_notification(self, conversation_id: str, message_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            message = self._messages.get(message_id)
            if not message or message.conversation_id != conversation_id:
                return None
            if not self._is_pending_notification(message):
                return None
            message.notification_delivered_at = time()
            return deepcopy(message).to_dict()

    async def claim_next_notification(self, conversation_id: str) -> Optional[dict[str, Any]]:
        pending = await self.list_pending_notifications(conversation_id)
        if not pending:
            return None
        return await self.claim_notification(conversation_id, str(pending[0]["message_id"]))

    async def release_notification(self, conversation_id: str, message_id: str) -> None:
        async with self._lock:
            message = self._messages.get(message_id)
            if message and message.conversation_id == conversation_id and message.integrated_at is None:
                message.notification_delivered_at = None
        if self._pending_listener is not None:
            self._pending_listener(conversation_id)

    async def mark_integrated(self, conversation_id: str, message_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            message = self._messages.get(message_id)
            if not message or message.conversation_id != conversation_id:
                return None
            if message.integrated_at is None:
                message.integrated_at = time()
            return deepcopy(message).to_dict()

    async def acknowledge(self, conversation_id: str, message_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            message = self._messages.get(message_id)
            if not message or message.conversation_id != conversation_id:
                return None
            if message.acknowledged_at is None:
                message.acknowledged_at = time()
            return deepcopy(message).to_dict()

    async def is_integrated(self, conversation_id: str, message_id: str) -> bool:
        async with self._lock:
            message = self._messages.get(message_id)
            return bool(message and message.conversation_id == conversation_id and message.integrated_at is not None)

    def _conversation_messages_locked(self, conversation_id: str) -> list[AgentMailboxMessage]:
        return [
            self._messages[message_id]
            for message_id in self._by_conversation.get(conversation_id, [])
            if message_id in self._messages
        ]

    def _is_pending_notification(self, message: AgentMailboxMessage) -> bool:
        return (
            self._should_notify(message)
            and message.notification_enqueued_at is not None
            and message.notification_delivered_at is None
            and message.integrated_at is None
            and message.acknowledged_at is None
        )

    @staticmethod
    def _should_notify(message: AgentMailboxMessage) -> bool:
        return (
            message.message_type in {AgentMailboxMessageType.RESULT, AgentMailboxMessageType.ERROR}
            and message.delivery_policy in {
                AgentDeliveryPolicy.AUTO,
                AgentDeliveryPolicy.NOTIFY,
            }
        )

    @staticmethod
    def _dedupe_key(
        source_run_id: str,
        message_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> tuple[str, str, str]:
        result_version = str(metadata.get("result_version") or metadata.get("event_type") or "")
        digest = hashlib.sha256(f"{content}\0{result_version}".encode("utf-8")).hexdigest()
        return source_run_id, message_type, digest
