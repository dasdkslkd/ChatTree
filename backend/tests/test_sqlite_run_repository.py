import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import runs as runs_route
from backend.core.persistence.blob_store import BlobStore
from backend.core.persistence.content import INLINE_TEXT_LIMIT
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import RunManager, RunStatus


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
    assert [event["payload"]["content"] for event in events] == ["b"]
    assert events[0]["payload"]["event_index"] == 1


def test_run_repository_marks_interrupted_runs_on_startup(tmp_path):
    _persistence, _chat, runs, conv_id, node_id = _repositories(tmp_path)
    run_id = runs.create_run(conv_id, kind="chat", target_node_id=node_id)

    interrupted = runs.mark_unfinished_as_interrupted()

    assert run_id in interrupted
    assert runs.get_run(run_id)["status"] == "interrupted"


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
        events = runs.read_events(record.run_id)
        assert events[-1]["payload"]["type"] == "run_target_bound"
        assert events[-1]["payload"]["target_node_id"] == target_node_id

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
