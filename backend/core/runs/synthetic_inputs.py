from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from time import time
from typing import Any, Callable, Dict, Optional


@dataclass
class SyntheticInput:
    input_id: str
    kind: str
    conversation_id: str
    anchor_node_id: Optional[str]
    source_run_id: str
    source_run_kind: str
    status: str
    summary: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time)
    consumed_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SyntheticInputQueue:
    """In-memory queue for synthetic inputs produced by detached runs."""

    def __init__(self) -> None:
        self._items: Dict[str, SyntheticInput] = {}
        self._source_index: Dict[tuple[str, str, str], str] = {}
        self._pending_listener: Optional[Callable[[str], None]] = None

    def set_pending_listener(self, listener: Optional[Callable[[str], None]]) -> None:
        self._pending_listener = listener

    def _notify_pending(self, conversation_id: str) -> None:
        if self._pending_listener is not None:
            self._pending_listener(conversation_id)

    def enqueue(
        self,
        *,
        kind: str,
        conversation_id: str,
        anchor_node_id: Optional[str],
        source_run_id: str,
        source_run_kind: str,
        status: str = "pending",
        summary: str = "",
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SyntheticInput:
        source_key = (kind, source_run_kind, source_run_id)
        existing_id = self._source_index.get(source_key)
        if existing_id and existing_id in self._items:
            return deepcopy(self._items[existing_id])

        input_id = f"synthetic_{uuid.uuid4().hex}"
        item = SyntheticInput(
            input_id=input_id,
            kind=kind,
            conversation_id=conversation_id,
            anchor_node_id=anchor_node_id,
            source_run_id=source_run_id,
            source_run_kind=source_run_kind,
            status=status,
            summary=summary,
            content=content,
            metadata=dict(metadata or {}),
        )
        self._items[input_id] = item
        self._source_index[source_key] = input_id
        if item.status == "pending":
            self._notify_pending(conversation_id)
        return deepcopy(item)

    def list_pending(self, conversation_id: str) -> list[Dict[str, Any]]:
        return [
            deepcopy(item).to_dict()
            for item in sorted(self._items.values(), key=lambda candidate: candidate.created_at)
            if item.conversation_id == conversation_id and item.status == "pending"
        ]

    def get(self, conversation_id: str, input_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(input_id)
        if not item or item.conversation_id != conversation_id:
            return None
        return deepcopy(item).to_dict()

    def mark_consumed(self, conversation_id: str, input_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(input_id)
        if not item or item.conversation_id != conversation_id:
            return None
        if item.status != "consumed":
            item.status = "consumed"
            item.consumed_at = time()
        return deepcopy(item).to_dict()

    def claim(self, conversation_id: str, input_id: str) -> Optional[Dict[str, Any]]:
        item = self._items.get(input_id)
        if not item or item.conversation_id != conversation_id or item.status != "pending":
            return None
        item.status = "processing"
        return deepcopy(item).to_dict()

    def claim_next(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        pending = self.list_pending(conversation_id)
        if not pending:
            return None
        return self.claim(conversation_id, str(pending[0]["input_id"]))

    def release(self, conversation_id: str, input_id: str, *, notify: bool = True) -> Optional[Dict[str, Any]]:
        item = self._items.get(input_id)
        if not item or item.conversation_id != conversation_id:
            return None
        if item.status == "processing":
            item.status = "pending"
            item.consumed_at = None
            if notify:
                self._notify_pending(conversation_id)
        return deepcopy(item).to_dict()

    def dequeue(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        pending = self.list_pending(conversation_id)
        if not pending:
            return None
        return self.mark_consumed(conversation_id, str(pending[0]["input_id"]))
