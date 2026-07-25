from __future__ import annotations

import asyncio
import hashlib
import uuid
from copy import deepcopy
from typing import Any, Dict, Optional

from .types import AgentDeliveryPolicy, AgentMailboxMessage, AgentMailboxMessageType


class AgentMailbox:
    """Transient agent-to-agent input queue; task history uses task_notifications."""

    def __init__(self) -> None:
        self._messages: Dict[str, AgentMailboxMessage] = {}
        self._dedupe: Dict[tuple[str, str, str], str] = {}
        self._lock = asyncio.Lock()

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
            self._messages[message_id] = message
            self._dedupe[dedupe_key] = message_id
        return deepcopy(message)

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
