from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .types import RunRecord


@dataclass(frozen=True)
class RunIdempotency:
    key: str
    request_fingerprint: str


@dataclass(frozen=True)
class RunStartResult:
    run: RunRecord
    created: bool


class RunIdempotencyConflictError(RuntimeError):
    def __init__(self, existing_run_id: str) -> None:
        super().__init__(f"idempotency key is already bound to run {existing_run_id}")
        self.existing_run_id = existing_run_id


class RunRequestFingerprintError(ValueError):
    """The validated request cannot be represented as finite canonical JSON."""


class RunReferenceNotFoundError(RuntimeError):
    def __init__(self, reference_kind: str, reference_id: str) -> None:
        super().__init__(f"{reference_kind} reference {reference_id} was not found")
        self.reference_kind = reference_kind
        self.reference_id = reference_id


class RunReferenceConversationMismatchError(RuntimeError):
    def __init__(self, reference_kind: str, reference_id: str) -> None:
        super().__init__(
            f"{reference_kind} reference {reference_id} belongs to another conversation"
        )
        self.reference_kind = reference_kind
        self.reference_id = reference_id


def fingerprint_run_request(
    *,
    operation: str,
    conversation_id: str,
    anchor_node_id: Optional[str],
    payload: Mapping[str, Any],
) -> str:
    try:
        canonical = json.dumps(
            {
                "operation": operation,
                "conversation_id": conversation_id,
                "anchor_node_id": anchor_node_id,
                "payload": dict(payload),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RunRequestFingerprintError(
            "run start request must contain finite JSON values"
        ) from exc
    return hashlib.sha256(canonical).hexdigest()
