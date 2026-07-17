from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from backend.core.runs import (
    RunIdempotency,
    RunIdempotencyConflictError,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunRequestFingerprintError,
    RunStartResult,
)
from backend.core.runs.idempotency import fingerprint_run_request
from backend.core.runs.types import RunKind, RunRecord


def test_fingerprint_is_canonical_and_utf8_stable():
    first = fingerprint_run_request(
        operation="message",
        conversation_id="conv-1",
        anchor_node_id="node-1",
        payload={"content": "中文", "options": {"b": 2, "a": 1}},
    )
    second = fingerprint_run_request(
        operation="message",
        conversation_id="conv-1",
        anchor_node_id="node-1",
        payload={"options": {"a": 1, "b": 2}, "content": "中文"},
    )
    canonical_utf8 = (
        '{"anchor_node_id":"node-1","conversation_id":"conv-1",'
        '"operation":"message","payload":{"content":"中文",'
        '"options":{"a":1,"b":2}}}'
    ).encode("utf-8")

    assert first == second
    assert first == hashlib.sha256(canonical_utf8).hexdigest()
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_fingerprint_changes_for_operation_conversation_anchor_or_payload():
    base = dict(
        operation="message",
        conversation_id="conv-1",
        anchor_node_id="node-1",
        payload={"content": "hello", "focus_new_node": True},
    )
    baseline = fingerprint_run_request(**base)

    assert fingerprint_run_request(**{**base, "operation": "workflow"}) != baseline
    assert fingerprint_run_request(**{**base, "conversation_id": "conv-2"}) != baseline
    assert fingerprint_run_request(**{**base, "anchor_node_id": "node-2"}) != baseline
    assert fingerprint_run_request(**{**base, "payload": {"content": "changed"}}) != baseline


@pytest.mark.parametrize(
    "non_finite",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
@pytest.mark.parametrize(
    "nest",
    [
        pytest.param(lambda value: {"value": value}, id="top-level"),
        pytest.param(lambda value: {"value": [value]}, id="list"),
        pytest.param(lambda value: {"value": {"nested": value}}, id="mapping"),
    ],
)
def test_fingerprint_rejects_non_finite_json_at_any_depth(non_finite, nest):
    with pytest.raises(RunRequestFingerprintError) as raised:
        fingerprint_run_request(
            operation="message",
            conversation_id="conv-1",
            anchor_node_id=None,
            payload=nest(non_finite),
        )

    assert type(raised.value) is RunRequestFingerprintError


@pytest.mark.parametrize(
    "value",
    [object(), {"not", "json"}, b"bytes"],
    ids=["object", "set", "bytes"],
)
def test_fingerprint_wraps_unserializable_values(value):
    with pytest.raises(RunRequestFingerprintError) as raised:
        fingerprint_run_request(
            operation="message",
            conversation_id="conv-1",
            anchor_node_id=None,
            payload={"nested": [value]},
        )

    assert type(raised.value) is RunRequestFingerprintError


def test_fingerprint_accepts_finite_json_scalars_canonically():
    first = fingerprint_run_request(
        operation="agent",
        conversation_id="会话-1",
        anchor_node_id=None,
        payload={
            "none": None,
            "booleans": [True, False],
            "integer": 42,
            "floats": [0.0, -1.25, 1.5e20],
            "nested": {"z": "终", "a": "始"},
        },
    )
    second = fingerprint_run_request(
        operation="agent",
        conversation_id="会话-1",
        anchor_node_id=None,
        payload={
            "nested": {"a": "始", "z": "终"},
            "floats": [0.0, -1.25, 1.5e20],
            "integer": 42,
            "booleans": [True, False],
            "none": None,
        },
    )

    assert first == second


def test_idempotency_domain_types_are_exported_and_immutable():
    idempotency = RunIdempotency(key="op-1", request_fingerprint="a" * 64)
    run = RunRecord(run_id="run-1", conversation_id="conv-1", kind=RunKind.CHAT)
    result = RunStartResult(run=run, created=True)

    assert idempotency.key == "op-1"
    assert idempotency.request_fingerprint == "a" * 64
    assert result == RunStartResult(run=run, created=True)
    with pytest.raises(FrozenInstanceError):
        idempotency.key = "changed"
    with pytest.raises(FrozenInstanceError):
        result.created = False


def test_idempotency_conflict_exposes_existing_run_id():
    error = RunIdempotencyConflictError("run-existing")

    assert error.existing_run_id == "run-existing"
    assert vars(error) == {"existing_run_id": "run-existing"}


@pytest.mark.parametrize(
    "error_type",
    [RunReferenceNotFoundError, RunReferenceConversationMismatchError],
)
def test_reference_errors_expose_only_safe_reference_fields(error_type):
    error = error_type("anchor_node_id", "node-1")

    assert error.reference_kind == "anchor_node_id"
    assert error.reference_id == "node-1"
    assert vars(error) == {
        "reference_kind": "anchor_node_id",
        "reference_id": "node-1",
    }
