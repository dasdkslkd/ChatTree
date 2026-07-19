import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import runs as runs_route
from backend.core.persistence.blob_store import BlobStore
from backend.core.persistence.content import INLINE_TEXT_LIMIT
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import (
    RunIdempotencyConflictError,
    RunManager,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunStatus,
)


def _repositories(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    runs = SQLiteRunRepository(persistence)
    conv_id = chat.create_conversation(title="Runs")
    node_id = chat.create_node(conv_id, parent_id=None)
    return persistence, chat, runs, conv_id, node_id


def test_run_repository_persists_run_events_and_replays_from_index(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)

    run_id = runs.create_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        target_node_id=node_id,
        summary="main",
        metadata={"source": "test"},
    )
    runs.append_event(run_id, {"status": "content", "content": "a"})
    runs.append_event(run_id, {"status": "content", "content": "b"})

    run = runs.get_run(run_id)
    events = runs.read_events(run_id, from_event=1)

    assert run["conversation_id"] == conv_id
    assert run["status"] == "running"
    assert run["event_count"] == 2
    assert run["metadata"] == {"source": "test"}
    assert run["idempotency_key"] is None
    assert run["request_fingerprint"] is None
    assert [event["payload"]["content"] for event in events] == ["b"]
    assert events[0]["payload"]["event_index"] == 1


def test_create_or_get_run_returns_existing_for_same_fingerprint(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)

    first, first_created = runs.create_or_get_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        idempotency_key="op_same",
        request_fingerprint="a" * 64,
    )
    second, second_created = runs.create_or_get_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        idempotency_key="op_same",
        request_fingerprint="a" * 64,
    )

    assert first_created is True
    assert second_created is False
    assert first["run_id"] == second["run_id"]
    assert second["idempotency_key"] == "op_same"
    assert second["request_fingerprint"] == "a" * 64


def test_create_or_get_run_rejects_same_key_with_different_fingerprint(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    first, _ = runs.create_or_get_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        idempotency_key="op_conflict",
        request_fingerprint="a" * 64,
    )

    with pytest.raises(RunIdempotencyConflictError) as raised:
        runs.create_or_get_run(
            conv_id,
            kind="workflow",
            anchor_node_id=node_id,
            idempotency_key="op_conflict",
            request_fingerprint="b" * 64,
        )

    assert raised.value.existing_run_id == first["run_id"]


def test_create_or_get_run_rejects_cross_conversation_key_collision(tmp_path):
    _persistence, chat, runs, conv_id, node_id = _repositories(tmp_path)
    other_conv_id = chat.create_conversation(title="Other")
    other_node_id = chat.create_node(other_conv_id, parent_id=None)
    first, _ = runs.create_or_get_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        idempotency_key="op_cross_conversation",
        request_fingerprint="a" * 64,
    )

    with pytest.raises(RunIdempotencyConflictError) as raised:
        runs.create_or_get_run(
            other_conv_id,
            kind="chat",
            anchor_node_id=other_node_id,
            idempotency_key="op_cross_conversation",
            request_fingerprint="b" * 64,
        )

    assert raised.value.existing_run_id == first["run_id"]


def test_concurrent_create_or_get_has_one_winner(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)

    def create_one(_index):
        return runs.create_or_get_run(
            conv_id,
            kind="chat",
            anchor_node_id=node_id,
            idempotency_key="op_concurrent",
            request_fingerprint="c" * 64,
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(create_one, range(20)))

    assert sum(1 for _run, created in results if created) == 1
    assert len({run["run_id"] for run, _created in results}) == 1


@pytest.mark.parametrize(
    ("idempotency_key", "request_fingerprint"),
    (("op_key_only", None), (None, "d" * 64)),
)
def test_create_or_get_run_rejects_idempotency_pair_mismatch(
    tmp_path,
    idempotency_key,
    request_fingerprint,
):
    _persistence, _chat, runs, conv_id, _node_id = _repositories(tmp_path)

    with pytest.raises(
        ValueError,
        match="idempotency key and request fingerprint must be provided together",
    ):
        runs.create_or_get_run(
            conv_id,
            kind="chat",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )


def test_create_or_get_run_reraises_unrelated_foreign_key_failure(tmp_path):
    _persistence, _chat, runs, _conv_id, _node_id = _repositories(tmp_path)

    with pytest.raises(sqlite3.IntegrityError):
        runs.create_or_get_run(
            "missing-conversation",
            kind="chat",
            idempotency_key="op_missing_conversation",
            request_fingerprint="e" * 64,
        )

    assert runs.get_run_by_idempotency_key("op_missing_conversation") is None


def test_create_or_get_run_handles_bare_integrity_error_after_rollback(
    tmp_path,
    monkeypatch,
):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    seeded, _ = runs.create_or_get_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        idempotency_key="op_bare_integrity",
        request_fingerprint="f" * 64,
    )

    class BareIntegrityError(sqlite3.IntegrityError):
        pass

    def lose_insert(*_args, **_kwargs):
        raise BareIntegrityError()

    monkeypatch.setattr(runs, "_insert_run_in_connection", lose_insert)

    replayed, created = runs.create_or_get_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        idempotency_key="op_bare_integrity",
        request_fingerprint="f" * 64,
    )

    assert created is False
    assert replayed["run_id"] == seeded["run_id"]


def test_create_or_get_run_winner_is_materialized_without_get_run(
    tmp_path,
    monkeypatch,
):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)

    def fail_get_run(_run_id):
        raise AssertionError("winner performed a post-commit get_run")

    monkeypatch.setattr(runs, "get_run", fail_get_run)

    created_run, created = runs.create_or_get_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
        idempotency_key="op_materialized",
        request_fingerprint="1" * 64,
    )

    assert created is True
    assert created_run["idempotency_key"] == "op_materialized"


def test_create_or_get_run_rolls_back_when_winner_materialization_fails(
    tmp_path,
    monkeypatch,
):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    original_run_from_row = runs._run_from_row

    def fail_materialization(_row):
        raise RuntimeError("materialization failed")

    monkeypatch.setattr(runs, "_run_from_row", fail_materialization)
    with pytest.raises(RuntimeError, match="materialization failed"):
        runs.create_or_get_run(
            conv_id,
            kind="chat",
            anchor_node_id=node_id,
            idempotency_key="op_materialization_failure",
            request_fingerprint="4" * 64,
        )

    monkeypatch.setattr(runs, "_run_from_row", original_run_from_row)
    assert runs.get_run_by_idempotency_key("op_materialization_failure") is None


def test_validate_run_references_distinguishes_missing_references_without_insert(
    tmp_path,
):
    persistence, _chat, runs, conv_id, _node_id = _repositories(tmp_path)
    cases = (
        ("conversation_id", "missing-conversation", "missing-conversation", {}),
        (
            "anchor_node_id",
            "missing-anchor",
            conv_id,
            {"anchor_node_id": "missing-anchor"},
        ),
        (
            "created_by_run_id",
            "missing-created-by",
            conv_id,
            {"created_by_run_id": "missing-created-by"},
        ),
        (
            "cancellation_parent_run_id",
            "missing-cancellation-parent",
            conv_id,
            {"cancellation_parent_run_id": "missing-cancellation-parent"},
        ),
    )

    with persistence.connect() as conn:
        initial_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    for reference_kind, reference_id, conversation_id, arguments in cases:
        with pytest.raises(RunReferenceNotFoundError) as raised:
            runs.validate_run_references(conversation_id, **arguments)
        assert raised.value.reference_kind == reference_kind
        assert raised.value.reference_id == reference_id
    with persistence.connect() as conn:
        final_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert final_count == initial_count


def test_validate_run_references_rejects_existing_ids_of_wrong_entity_type(
    tmp_path,
):
    persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(
        conv_id,
        kind="chat",
        anchor_node_id=node_id,
    )
    cases = (
        ("conversation_id", node_id, node_id, {}),
        ("conversation_id", run_id, run_id, {}),
        (
            "anchor_node_id",
            conv_id,
            conv_id,
            {"anchor_node_id": conv_id},
        ),
        (
            "anchor_node_id",
            run_id,
            conv_id,
            {"anchor_node_id": run_id},
        ),
        (
            "created_by_run_id",
            conv_id,
            conv_id,
            {"created_by_run_id": conv_id},
        ),
        (
            "created_by_run_id",
            node_id,
            conv_id,
            {"created_by_run_id": node_id},
        ),
        (
            "cancellation_parent_run_id",
            conv_id,
            conv_id,
            {"cancellation_parent_run_id": conv_id},
        ),
        (
            "cancellation_parent_run_id",
            node_id,
            conv_id,
            {"cancellation_parent_run_id": node_id},
        ),
    )

    with persistence.connect() as conn:
        initial_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    for reference_kind, reference_id, conversation_id, arguments in cases:
        with pytest.raises(RunReferenceConversationMismatchError) as raised:
            runs.validate_run_references(conversation_id, **arguments)
        assert raised.value.reference_kind == reference_kind
        assert raised.value.reference_id == reference_id
    with persistence.connect() as conn:
        final_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert final_count == initial_count


def test_validate_run_references_rejects_cross_conversation_references(tmp_path):
    persistence, chat, runs, conv_id, _node_id = _repositories(tmp_path)
    other_conv_id = chat.create_conversation(title="Other")
    other_node_id = chat.create_node(other_conv_id, parent_id=None)
    other_run_id = runs.create_run(
        other_conv_id,
        kind="chat",
        anchor_node_id=other_node_id,
    )
    cases = (
        ("anchor_node_id", other_node_id, {"anchor_node_id": other_node_id}),
        ("created_by_run_id", other_run_id, {"created_by_run_id": other_run_id}),
        (
            "cancellation_parent_run_id",
            other_run_id,
            {"cancellation_parent_run_id": other_run_id},
        ),
    )

    with persistence.connect() as conn:
        initial_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    for reference_kind, reference_id, arguments in cases:
        with pytest.raises(RunReferenceConversationMismatchError) as raised:
            runs.validate_run_references(conv_id, **arguments)
        assert raised.value.reference_kind == reference_kind
        assert raised.value.reference_id == reference_id
    with persistence.connect() as conn:
        final_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert final_count == initial_count


def test_run_manager_validate_run_references_delegates_to_repository(tmp_path):
    _persistence, chat, runs, conv_id, node_id = _repositories(tmp_path)
    other_conv_id = chat.create_conversation(title="Other")
    other_node_id = chat.create_node(other_conv_id, parent_id=None)
    manager = RunManager(repository=runs)

    manager.validate_run_references(conv_id, anchor_node_id=node_id)

    with pytest.raises(RunReferenceConversationMismatchError):
        manager.validate_run_references(conv_id, anchor_node_id=other_node_id)


def test_reference_deleted_after_validation_reraises_foreign_key_failure(tmp_path):
    persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    runs.validate_run_references(conv_id, anchor_node_id=node_id)
    with persistence.connect() as conn:
        conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

    with pytest.raises(sqlite3.IntegrityError):
        runs.create_or_get_run(
            conv_id,
            kind="chat",
            anchor_node_id=node_id,
            idempotency_key="op_deleted_anchor",
            request_fingerprint="2" * 64,
        )

    assert runs.get_run_by_idempotency_key("op_deleted_anchor") is None


def test_run_repository_appends_event_batches(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)

    returned = runs.append_events(run_id, [
        {"status": "content", "content": "a"},
        {"status": "content", "content": "b"},
        {"status": "complete", "content": None},
    ])

    assert [event["event_index"] for event in returned] == [0, 1, 2]
    assert runs.get_run(run_id)["event_count"] == 3
    assert [event["payload"]["content"] for event in runs.read_events(run_id)[:2]] == ["a", "b"]


def test_run_repository_appends_indexed_event_batches_with_one_run_update(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)

    returned = runs.append_indexed_events(run_id, [
        {
            "run_id": run_id,
            "event_index": 0,
            "payload": {"status": "content", "content": "a", "event_index": 0},
            "created_at": 1000.0,
        },
        {
            "run_id": run_id,
            "event_index": 1,
            "payload": {"status": "content", "content": "b", "event_index": 1},
            "created_at": 1001.0,
        },
    ])

    assert [event["event_index"] for event in returned] == [0, 1]
    assert [event["payload"]["content"] for event in runs.read_events(run_id)] == ["a", "b"]
    assert runs.get_run(run_id)["event_count"] == 2
    assert runs.get_run(run_id)["updated_at"] == 1001.0


def test_run_repository_rejects_out_of_order_indexed_event_batches(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)

    try:
        runs.append_indexed_events(run_id, [
            {
                "run_id": run_id,
                "event_index": 1,
                "payload": {"status": "content", "content": "late", "event_index": 1},
                "created_at": 1000.0,
            },
        ])
    except ValueError as exc:
        assert "expected event_index 0" in str(exc)
    else:
        raise AssertionError("append_indexed_events accepted a gap")

    assert runs.read_events(run_id) == []
    assert runs.get_run(run_id)["event_count"] == 0


def test_run_repository_marks_interrupted_runs_on_startup(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)
    keyed_run, keyed_created = runs.create_or_get_run(
        conv_id,
        kind="chat",
        target_node_id=node_id,
        idempotency_key="op_startup_interrupted",
        request_fingerprint="3" * 64,
    )

    interrupted = runs.mark_unfinished_as_interrupted()
    replayed, replay_created = runs.create_or_get_run(
        conv_id,
        kind="chat",
        target_node_id=node_id,
        idempotency_key="op_startup_interrupted",
        request_fingerprint="3" * 64,
    )

    assert run_id in interrupted
    assert keyed_created is True
    assert keyed_run["run_id"] in interrupted
    assert runs.get_run(run_id)["status"] == "interrupted"
    assert replay_created is False
    assert replayed["run_id"] == keyed_run["run_id"]
    assert replayed["status"] == "interrupted"


def test_run_repository_does_not_interrupt_stopped_runs_on_startup(tmp_path):
    persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    stopped_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)
    running_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)
    with persistence.connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'stopped' WHERE id = ?",
            (stopped_id,),
        )

    interrupted = runs.mark_unfinished_as_interrupted()

    assert stopped_id not in interrupted
    assert running_id in interrupted
    assert runs.get_run(stopped_id)["status"] == "stopped"
    assert runs.get_run(running_id)["status"] == "interrupted"


def test_run_repository_stores_large_payloads_as_blobs(tmp_path):
    persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)
    content = "x" * (INLINE_TEXT_LIMIT + 1)

    event = runs.append_event(run_id, {"status": "content", "content": content})

    with persistence.connect() as conn:
        row = conn.execute(
            """
            SELECT payload_inline, payload_blob_id
            FROM run_events
            WHERE run_id = ? AND event_index = ?
            """,
            (run_id, event["event_index"]),
        ).fetchone()
    assert row["payload_inline"] is None
    assert row["payload_blob_id"]
    assert BlobStore(persistence).get_text(row["payload_blob_id"]).startswith("{")
    assert runs.read_events(run_id)[0]["payload"]["content"] == content


def test_run_repository_concurrent_large_payload_events_do_not_leak_blobs(tmp_path):
    persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)
    call_count = 24

    def append(index):
        return runs.append_event(
            run_id,
            {
                "status": "content",
                "content": f"{index}-" + ("x" * (INLINE_TEXT_LIMIT + 1024)),
            },
        )

    with ThreadPoolExecutor(max_workers=call_count) as executor:
        returned_events = list(executor.map(append, range(call_count)))

    run = runs.get_run(run_id)
    stored_events = runs.read_events(run_id)
    returned_indexes = sorted(event["event_index"] for event in returned_events)
    stored_indexes = [event["event_index"] for event in stored_events]

    assert run["event_count"] == call_count
    assert returned_indexes == list(range(call_count))
    assert stored_indexes == list(range(call_count))
    assert [event["payload"]["event_index"] for event in stored_events] == list(
        range(call_count)
    )
    assert all(event["payload_blob_id"] for event in stored_events)
    with persistence.connect() as conn:
        blob_rows = conn.execute("SELECT id, ref_count FROM blobs").fetchall()
    assert len(blob_rows) == call_count
    assert [row["ref_count"] for row in blob_rows] == [1] * call_count


def test_run_repository_finish_and_stop_update_status(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)

    assert runs.request_stop(run_id) is True
    stopping = runs.get_run(run_id)
    finished = runs.finish_run(run_id, "cancelled", error="user stop")

    assert stopping["status"] == "stopping"
    assert finished["status"] == "cancelled"
    assert finished["metadata"]["error"] == "user stop"
    assert runs.request_stop(run_id) is False


def test_run_manager_uses_optional_repository_backend(tmp_path):
    async def scenario():
        _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
        manager = RunManager(repository=runs)

        record = await manager.create_run(
            conversation_id=conv_id,
            kind="chat",
            target_node_id=node_id,
            summary="repo-backed",
        )
        payload = await manager.append_event(
            record.run_id,
            {"status": "content", "content": "hello"},
        )
        finished = await manager.finish_run(record.run_id, RunStatus.COMPLETED)

        events = runs.read_events(record.run_id)
        assert payload["event_index"] == 1
        assert finished.status == RunStatus.COMPLETED
        assert runs.get_run(record.run_id)["status"] == "completed"
        assert [event["payload"].get("type") for event in events] == [
            "run_started",
            None,
            "run_finished",
        ]
        assert events[1]["payload"]["content"] == "hello"
        assert events[2]["payload"]["status"] == "completed"

    asyncio.run(scenario())


def test_run_manager_publishes_events_before_background_flush(tmp_path):
    async def scenario():
        _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
        manager = RunManager(repository=runs)
        record = await manager.create_run(
            conversation_id=conv_id,
            kind="chat",
            target_node_id=node_id,
        )

        await manager.append_events(record.run_id, [
            {"status": "content", "content": f"chunk-{index}"}
            for index in range(5)
        ])

        live_events = manager.read_events(record.run_id)
        assert [event.get("content") for event in live_events[1:]] == [
            "chunk-0",
            "chunk-1",
            "chunk-2",
            "chunk-3",
            "chunk-4",
        ]

        await manager.flush_run_events(record.run_id)
        stored_events = runs.read_events(record.run_id)
        assert [event["event_index"] for event in stored_events] == list(range(6))
        assert [event["payload"].get("content") for event in stored_events[1:]] == [
            "chunk-0",
            "chunk-1",
            "chunk-2",
            "chunk-3",
            "chunk-4",
        ]

    asyncio.run(scenario())


def test_run_manager_flushes_pending_events_before_finish(tmp_path):
    async def scenario():
        _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
        manager = RunManager(repository=runs)
        record = await manager.create_run(
            conversation_id=conv_id,
            kind="chat",
            target_node_id=node_id,
        )
        await manager.append_events(record.run_id, [
            {"status": "content", "content": "a"},
            {"status": "content", "content": "b"},
        ])

        finished = await manager.finish_run(record.run_id, RunStatus.COMPLETED)

        stored_events = runs.read_events(record.run_id)
        assert finished.status == RunStatus.COMPLETED
        assert [event["event_index"] for event in stored_events] == [0, 1, 2, 3]
        assert [event["payload"].get("type") for event in stored_events] == [
            "run_started",
            None,
            None,
            "run_finished",
        ]
        assert stored_events[-1]["payload"]["status"] == "completed"

    asyncio.run(scenario())


def test_run_manager_bind_target_node_persists_sqlite_run_record(tmp_path):
    async def scenario():
        _persistence, chat, runs, conv_id, node_id = _repositories(tmp_path)
        target_node_id = chat.create_node(conv_id, parent_id=node_id)
        manager = RunManager(repository=runs)
        record = await manager.create_run(
            conversation_id=conv_id,
            kind="chat",
            anchor_node_id=node_id,
        )

        updated = await manager.bind_target_node(record.run_id, target_node_id)

        assert updated.target_node_id == target_node_id
        assert manager.get_run(record.run_id)["target_node_id"] == target_node_id
        assert runs.get_run(record.run_id)["target_node_id"] == target_node_id
        await manager.flush_run_events(record.run_id)
        events = runs.read_events(record.run_id)
        assert events[-1]["payload"]["type"] == "run_target_bound"
        assert events[-1]["payload"]["target_node_id"] == target_node_id

    asyncio.run(scenario())


def test_run_manager_bind_anchor_node_persists_once(tmp_path):
    async def scenario():
        _persistence, chat, runs, conv_id, node_id = _repositories(tmp_path)
        next_anchor_id = chat.create_node(conv_id, parent_id=node_id)
        manager = RunManager(repository=runs)
        record = await manager.create_run(
            conversation_id=conv_id,
            kind="workflow",
            anchor_node_id=node_id,
        )

        updated = await manager.bind_anchor_node(record.run_id, next_anchor_id)
        repeated = await manager.bind_anchor_node(record.run_id, next_anchor_id)

        assert updated.anchor_node_id == next_anchor_id
        assert repeated.anchor_node_id == next_anchor_id
        assert manager.get_run(record.run_id)["anchor_node_id"] == next_anchor_id
        assert runs.get_run(record.run_id)["anchor_node_id"] == next_anchor_id
        await manager.flush_run_events(record.run_id)
        bound_events = [
            event["payload"]
            for event in runs.read_events(record.run_id)
            if event["payload"].get("type") == "run_anchor_bound"
        ]
        assert len(bound_events) == 1
        assert bound_events[0]["type"] == "run_anchor_bound"
        assert bound_events[0]["run_id"] == record.run_id
        assert bound_events[0]["anchor_node_id"] == next_anchor_id
        await manager.close()

    asyncio.run(scenario())


def test_run_manager_bind_anchor_node_preserves_cross_conversation_fk(tmp_path):
    async def scenario():
        _persistence, chat, runs, conv_id, node_id = _repositories(tmp_path)
        other_conv_id = chat.create_conversation(title="Other")
        other_anchor_id = chat.create_node(other_conv_id, parent_id=None)
        manager = RunManager(repository=runs)
        record = await manager.create_run(
            conversation_id=conv_id,
            kind="workflow",
            anchor_node_id=node_id,
        )

        with pytest.raises(sqlite3.IntegrityError):
            await manager.bind_anchor_node(record.run_id, other_anchor_id)

        assert manager.get_run(record.run_id)["anchor_node_id"] == node_id
        assert runs.get_run(record.run_id)["anchor_node_id"] == node_id
        await manager.flush_run_events(record.run_id)
        assert not [
            event
            for event in runs.read_events(record.run_id)
            if event["payload"].get("type") == "run_anchor_bound"
        ]
        await manager.close()

    asyncio.run(scenario())


def test_run_manager_bind_anchor_node_preserves_missing_anchor_fk(tmp_path):
    async def scenario():
        _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
        manager = RunManager(repository=runs)
        record = await manager.create_run(
            conversation_id=conv_id,
            kind="workflow",
            anchor_node_id=node_id,
        )

        with pytest.raises(sqlite3.IntegrityError):
            await manager.bind_anchor_node(record.run_id, "missing-anchor")

        assert manager.get_run(record.run_id)["anchor_node_id"] == node_id
        assert runs.get_run(record.run_id)["anchor_node_id"] == node_id
        await manager.close()

    asyncio.run(scenario())


def test_run_manager_rehydrates_repository_runs_after_restart(tmp_path):
    async def scenario():
        _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
        first = RunManager(repository=runs)
        record = await first.create_run(
            conversation_id=conv_id,
            kind="chat",
            target_node_id=node_id,
            summary="restartable",
        )
        await first.append_event(record.run_id, {"status": "content", "content": "persisted"})
        await first.close()

        restarted = RunManager(repository=runs)

        assert restarted.get_run(record.run_id)["status"] == "running"
        assert [run["run_id"] for run in restarted.list_active(conv_id)] == [record.run_id]
        assert restarted.read_events(record.run_id, 1)[0]["content"] == "persisted"

    asyncio.run(scenario())


def test_run_manager_startup_interrupts_are_visible_after_restart(tmp_path):
    async def scenario():
        _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
        first = RunManager(repository=runs)
        record = await first.create_run(
            conversation_id=conv_id,
            kind="chat",
            target_node_id=node_id,
        )
        await first.close()

        runs.mark_unfinished_as_interrupted()
        restarted = RunManager(repository=runs)

        assert restarted.list_active(conv_id) == []
        assert restarted.get_run(record.run_id)["status"] == "interrupted"
        events = restarted.read_events(record.run_id)
        assert events[-1]["status"] == "interrupted"

    asyncio.run(scenario())


def test_run_events_route_reads_sqlite_repository_events(tmp_path):
    async def scenario():
        _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
        manager = RunManager(repository=runs)
        record = await manager.create_run(
            conversation_id=conv_id,
            kind="chat",
            target_node_id=node_id,
        )
        await manager.append_event(record.run_id, {"status": "content", "content": "sqlite event"})
        return manager, record.run_id

    manager, run_id = asyncio.run(scenario())
    app = FastAPI()
    app.include_router(runs_route.router)
    app.state.run_manager = manager
    client = TestClient(app)

    response = client.get(f"/runs/{run_id}/events", params={"from_event": 1})

    assert response.status_code == 200
    assert response.json() == [
        {
            "status": "content",
            "content": "sqlite event",
            "run_id": run_id,
            "conversation_id": manager.get_run(run_id)["conversation_id"],
            "kind": "chat",
            "target_node_id": manager.get_run(run_id)["target_node_id"],
            "event_index": 1,
        }
    ]
