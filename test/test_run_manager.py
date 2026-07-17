import asyncio

import pytest

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import (
    RunIdempotencyConflictError,
    RunKind,
    RunManager,
    RunNotFoundError,
    RunStatus,
    RunWriterConflictError,
)
from backend.core.runs.journal import RunJournal


def _sqlite_run_manager(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    repository = SQLiteRunRepository(persistence)
    conversation_id = chat.create_conversation(title="Runs")
    node_id = chat.create_node(conversation_id, parent_id=None)
    return RunManager(repository=repository), repository, conversation_id, node_id


def test_run_manager_allows_multiple_runs_and_replays_events(tmp_path):
    async def run():
        manager = RunManager(RunJournal(tmp_path))
        first = await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-a")
        second = await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-b")

        await manager.append_event(first.run_id, {"status": "content", "content": "hello"})
        await manager.append_event(first.run_id, {"status": "content", "content": " world"})

        active = manager.list_active("conv")
        assert {item["run_id"] for item in active} == {first.run_id, second.run_id}

        sub = manager.subscribe(first.run_id, 1)
        event = await asyncio.wait_for(anext(sub), timeout=1)
        assert event["content"] == "hello"
        await sub.aclose()

        await manager.finish_run(first.run_id, RunStatus.COMPLETED)
        assert {item["run_id"] for item in manager.list_active("conv")} == {second.run_id}
        journal_payloads = [
            event["payload"]
            for event in manager.journal.read_events("conv", first.run_id)
        ]
        assert any(payload.get("content") == "hello" for payload in journal_payloads)

    asyncio.run(run())


def test_run_manager_rejects_two_writers_for_same_target(tmp_path):
    async def run():
        manager = RunManager(RunJournal(tmp_path))
        await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-a")
        with pytest.raises(RunWriterConflictError):
            await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-a")

    asyncio.run(run())


def test_run_journal_stores_events_under_conversation_runs_dir(tmp_path):
    async def run():
        manager = RunManager(RunJournal(tmp_path))
        record = await manager.create_run(conversation_id="conv", kind=RunKind.WORKFLOW)
        await manager.append_event(record.run_id, {"status": "complete", "event_type": "workflow_result"})

        new_path = tmp_path / "conv" / "runs" / f"{record.run_id}.jsonl"
        old_path = tmp_path / "conv" / f"{record.run_id}.jsonl"
        assert new_path.exists()
        assert not old_path.exists()

        payloads = [event["payload"] for event in manager.journal.read_events("conv", record.run_id)]
        assert payloads[-1]["event_type"] == "workflow_result"

    asyncio.run(run())


def test_run_manager_wait_for_terminal_result_returns_terminal_result(tmp_path):
    async def run():
        manager = RunManager(RunJournal(tmp_path))
        record = await manager.create_run(conversation_id="conv", kind=RunKind.SUBAGENT)

        async def produce_result():
            await asyncio.sleep(0.01)
            await manager.append_event(record.run_id, {
                "status": "complete",
                "event_type": "subagent_result",
                "content": "OK",
            })
            await manager.finish_run(record.run_id, RunStatus.COMPLETED)

        task = asyncio.create_task(produce_result())
        result = await manager.wait_for_terminal_result(
            record.run_id,
            result_event_types={"subagent_result"},
            error_event_types={"subagent_error"},
            timeout=1,
        )
        await task

        assert result["run_id"] == record.run_id
        assert result["status"] == RunStatus.COMPLETED.value
        assert result["message_type"] == "result"
        assert result["event_type"] == "subagent_result"
        assert result["content"] == "OK"

    asyncio.run(run())


def test_run_journal_ignores_legacy_data_runs_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy_root = tmp_path / "data" / "runs"
    path = legacy_root / "conv" / "run_legacy.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"run_id":"run_legacy","event_index":0,"payload":{"type":"run_finished","status":"completed"}}\n',
        encoding="utf-8",
    )

    journal = RunJournal()

    events = journal.read_events("conv", "run_legacy")

    assert events == []


def test_reserve_then_publish_establishes_one_visible_run_started_event():
    async def run():
        manager = RunManager()
        reserved = []
        results = await asyncio.gather(*(
            manager.reserve_or_get_run(
                conversation_id="conv-1",
                kind=RunKind.CHAT,
                idempotency_key="op_manager",
                request_fingerprint="d" * 64,
                on_reserved=reserved.append,
            )
            for _ in range(12)
        ))
        run_ids = {record.run_id for record, _created in results}
        run_id = next(iter(run_ids))

        assert reserved == [run_id]
        assert manager.get_run(run_id) is None
        assert manager.list_runs() == []
        assert manager.list_active() == []
        with pytest.raises(RunNotFoundError):
            manager.read_events(run_id, 0)
        subscription = manager.subscribe(run_id, 0)
        with pytest.raises(RunNotFoundError):
            await anext(subscription)
        await subscription.aclose()

        await manager.publish_reserved_run(run_id)
        await manager.publish_reserved_run(run_id)
        events = manager.read_events(run_id, 0)
        return manager, results, events

    manager, results, events = asyncio.run(run())
    assert sum(1 for _record, created in results if created) == 1
    assert len({record.run_id for record, _created in results}) == 1
    assert manager.get_run(results[0][0].run_id)["status"] == RunStatus.RUNNING.value
    assert [event["type"] for event in events].count("run_started") == 1


def test_publish_rejects_non_reserved_runs():
    async def run():
        manager = RunManager()
        record = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
        )
        with pytest.raises(RuntimeError, match="not a reserved run"):
            await manager.publish_reserved_run(record.run_id)

    asyncio.run(run())


def test_reservation_hook_precedes_cache_and_interruption_preserves_replay():
    async def run():
        manager = RunManager()
        reserved_run_id = None

        def fail_after_reservation(run_id):
            nonlocal reserved_run_id
            reserved_run_id = run_id
            assert run_id in manager._pending_reservations
            assert run_id not in manager._runs
            assert manager.get_run(run_id) is None
            raise RuntimeError("cache bootstrap failed")

        with pytest.raises(RuntimeError, match="cache bootstrap failed"):
            await manager.reserve_or_get_run(
                conversation_id="conv-1",
                kind=RunKind.SUBAGENT,
                idempotency_key="op_hook_fault",
                request_fingerprint="e" * 64,
                on_reserved=fail_after_reservation,
            )

        assert reserved_run_id is not None
        assert reserved_run_id in manager._pending_reservations
        interrupted = await manager.interrupt_reserved_run(
            reserved_run_id,
            "cache bootstrap failed",
        )
        replay, created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.SUBAGENT,
            idempotency_key="op_hook_fault",
            request_fingerprint="e" * 64,
        )
        return manager, interrupted, replay, created

    manager, interrupted, replay, created = asyncio.run(run())
    assert interrupted.status == RunStatus.INTERRUPTED
    assert created is False
    assert replay.run_id == interrupted.run_id
    assert replay.status == RunStatus.INTERRUPTED
    assert manager.get_run(replay.run_id)["status"] == RunStatus.INTERRUPTED.value
    finished = [
        event for event in manager.read_events(replay.run_id, 0)
        if event.get("type") == "run_finished"
    ]
    assert len(finished) == 1
    assert finished[0]["status"] == RunStatus.INTERRUPTED.value


def test_recovery_lookup_sees_memory_pending_without_making_it_public():
    async def run():
        manager = RunManager()
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            idempotency_key="op_memory_recovery",
            request_fingerprint="1" * 64,
        )

        assert manager.get_run(record.run_id) is None
        recovered = await manager.get_run_for_recovery(record.run_id)
        still_hidden = manager.get_run(record.run_id)
        await manager.interrupt_reserved_run(record.run_id, "test cleanup")
        return manager, record, recovered, still_hidden

    manager, record, recovered, still_hidden = asyncio.run(run())
    assert recovered is not manager._runs[record.run_id]
    assert recovered.run_id == record.run_id
    assert recovered.status == RunStatus.RUNNING
    assert still_hidden is None


def test_unpublished_memory_task_binding_is_not_finished_before_it_is_bound():
    class TaskService:
        def __init__(self):
            self.bind_calls = 0
            self.finish_calls = 0

        async def bind_in_memory_run(self, _run_id, _binding):
            self.bind_calls += 1

        async def handle_run_finished(self, _run):
            self.finish_calls += 1
            raise AssertionError("an unpublished binding must not be finalized")

    async def run():
        manager = RunManager()
        service = TaskService()
        manager.task_service = service
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.WORKFLOW_STEP,
            idempotency_key="op_unbound_task",
            request_fingerprint="9" * 64,
            task_binding={"task_generation_id": "generation-1", "step_position": 1},
        )
        interrupted = await manager.interrupt_reserved_run(record.run_id, "bootstrap failed")
        return service, interrupted

    service, interrupted = asyncio.run(run())
    assert interrupted.status == RunStatus.INTERRUPTED
    assert service.bind_calls == 0
    assert service.finish_calls == 0


def test_concurrent_reservation_interruption_emits_one_terminal_event_and_notification():
    async def run():
        manager = RunManager()
        notifications = []
        manager.add_finish_listener(notifications.append)
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            idempotency_key="op_interrupt_once",
            request_fingerprint="a" * 64,
        )
        results = await asyncio.gather(*(
            manager.interrupt_reserved_run(record.run_id, "bootstrap failed")
            for _ in range(8)
        ))
        return manager, record, results, notifications

    manager, record, results, notifications = asyncio.run(run())
    assert {item.status for item in results} == {RunStatus.INTERRUPTED}
    assert len(notifications) == 1
    assert notifications[0]["run_id"] == record.run_id
    assert [
        event["type"] for event in manager.read_events(record.run_id, 0)
    ] == ["run_finished"]


def test_published_reservation_interruption_is_singleflight_and_exactly_once():
    class TaskService:
        def __init__(self):
            self.bind_calls = 0
            self.finished = []

        async def bind_in_memory_run(self, _run_id, _binding):
            self.bind_calls += 1

        async def handle_run_finished(self, run):
            self.finished.append(run)
            return None

    async def run():
        manager = RunManager()
        service = TaskService()
        notifications = []
        manager.task_service = service
        manager.add_finish_listener(notifications.append)
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.WORKFLOW_STEP,
            idempotency_key="op_published_interrupt",
            request_fingerprint="7" * 64,
            task_binding={"task_generation_id": "generation-1", "step_position": 1},
        )
        await manager.publish_reserved_run(record.run_id)

        results = await asyncio.gather(*(
            manager.interrupt_reserved_run(record.run_id, "bootstrap failed")
            for _ in range(8)
        ))
        repeated = await manager.interrupt_reserved_run(
            record.run_id,
            "ignored duplicate",
        )
        return manager, service, notifications, record, results, repeated

    manager, service, notifications, record, results, repeated = asyncio.run(run())
    assert {item.run_id for item in results} == {record.run_id}
    assert {item.status for item in results} == {RunStatus.INTERRUPTED}
    assert repeated.run_id == record.run_id
    assert repeated.status == RunStatus.INTERRUPTED
    assert service.bind_calls == 1
    assert len(service.finished) == 1
    assert service.finished[0]["run_id"] == record.run_id
    assert len(notifications) == 1
    assert notifications[0]["run_id"] == record.run_id
    assert [event["type"] for event in manager.read_events(record.run_id, 0)] == [
        "run_started",
        "run_finished",
    ]
    assert record.run_id not in manager._published_reservation_ids


def test_repository_published_reservation_interruption_persists_event_order(tmp_path):
    async def run():
        manager, repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        record, _created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=node_id,
            idempotency_key="op_repository_published_interrupt",
            request_fingerprint="8" * 64,
        )
        await manager.publish_reserved_run(record.run_id)
        results = await asyncio.gather(*(
            manager.interrupt_reserved_run(record.run_id, "bootstrap failed")
            for _ in range(4)
        ))
        repeated = await manager.interrupt_reserved_run(
            record.run_id,
            "ignored duplicate",
        )
        await manager.close()
        return repository, record, results, repeated

    repository, record, results, repeated = asyncio.run(run())
    assert {item.status for item in results} == {RunStatus.INTERRUPTED}
    assert repeated.status == RunStatus.INTERRUPTED
    assert [
        event["payload"]["type"]
        for event in repository.read_events(record.run_id, 0)
    ] == ["run_started", "run_finished"]


def test_published_interrupt_claim_fences_duplicate_publish():
    async def run():
        manager = RunManager()
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            idempotency_key="op_published_interrupt_fence",
            request_fingerprint="9" * 64,
        )
        await manager.publish_reserved_run(record.run_id)

        await manager._lock.acquire()
        interruption = asyncio.create_task(
            manager.interrupt_reserved_run(record.run_id, "bootstrap failed")
        )
        await asyncio.sleep(0)
        duplicate_publish = asyncio.create_task(
            manager.publish_reserved_run(record.run_id)
        )
        await asyncio.sleep(0)
        manager._lock.release()

        interrupted = await interruption
        with pytest.raises(RuntimeError, match="interruption"):
            await duplicate_publish
        return manager, interrupted

    manager, interrupted = asyncio.run(run())
    assert interrupted.status == RunStatus.INTERRUPTED
    assert [event["type"] for event in manager.read_events(interrupted.run_id, 0)] == [
        "run_started",
        "run_finished",
    ]


def test_interrupt_claim_prevents_later_publish_from_appending_start():
    async def run():
        manager = RunManager()
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            idempotency_key="op_interrupt_publish_race",
            request_fingerprint="b" * 64,
        )
        await manager._lock.acquire()
        interruption = asyncio.create_task(
            manager.interrupt_reserved_run(record.run_id, "bootstrap failed")
        )
        await asyncio.sleep(0)
        publication = asyncio.create_task(manager.publish_reserved_run(record.run_id))
        await asyncio.sleep(0)
        manager._lock.release()

        interrupted = await interruption
        with pytest.raises(RuntimeError, match="interruption"):
            await publication
        return manager, interrupted

    manager, interrupted = asyncio.run(run())
    assert interrupted.status == RunStatus.INTERRUPTED
    assert [event["type"] for event in manager.read_events(interrupted.run_id, 0)] == [
        "run_finished",
    ]


def test_bound_memory_reservation_is_terminalized_after_publication_failure(monkeypatch):
    class TaskService:
        def __init__(self):
            self.bind_calls = 0
            self.finished = []

        async def bind_in_memory_run(self, _run_id, _binding):
            self.bind_calls += 1

        async def handle_run_finished(self, run):
            self.finished.append(run)
            return None

    async def run():
        manager = RunManager()
        service = TaskService()
        manager.task_service = service
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.WORKFLOW_STEP,
            idempotency_key="op_bound_publish_failure",
            request_fingerprint="c" * 64,
            task_binding={"task_generation_id": "generation-1", "step_position": 1},
        )

        async def fail_start_event(_run_id, _payload):
            raise RuntimeError("start event failed")

        monkeypatch.setattr(manager, "append_event", fail_start_event)
        with pytest.raises(RuntimeError, match="start event failed"):
            await manager.publish_reserved_run(record.run_id)
        interrupted = await manager.interrupt_reserved_run(
            record.run_id,
            "start event failed",
        )
        return service, interrupted

    service, interrupted = asyncio.run(run())
    assert interrupted.status == RunStatus.INTERRUPTED
    assert service.bind_calls == 1
    assert len(service.finished) == 1
    assert service.finished[0]["run_id"] == interrupted.run_id
    assert service.finished[0]["status"] == RunStatus.INTERRUPTED.value


def test_idempotent_target_admission_orders_replay_before_writer_conflict():
    async def run():
        manager = RunManager()
        first, created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            target_node_id="node-1",
            idempotency_key="op_target",
            request_fingerprint="f" * 64,
        )
        same, same_created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            target_node_id="node-1",
            idempotency_key="op_target",
            request_fingerprint="f" * 64,
        )
        with pytest.raises(RunIdempotencyConflictError) as conflict:
            await manager.reserve_or_get_run(
                conversation_id="conv-1",
                kind=RunKind.CHAT,
                target_node_id="node-1",
                idempotency_key="op_target",
                request_fingerprint="0" * 64,
            )
        with pytest.raises(RunWriterConflictError):
            await manager.reserve_or_get_run(
                conversation_id="conv-1",
                kind=RunKind.CHAT,
                target_node_id="node-1",
                idempotency_key="op_other",
                request_fingerprint="1" * 64,
            )
        return manager, first, created, same, same_created, conflict.value

    manager, first, created, same, same_created, conflict = asyncio.run(run())
    assert created is True
    assert same_created is False
    assert same.run_id == first.run_id
    assert conflict.existing_run_id == first.run_id
    assert manager._idempotent_runs == {
        "op_target": (first.run_id, "f" * 64),
    }


def test_close_closes_admission_and_drains_pending_reservations():
    async def run():
        manager = RunManager()
        record, _created = await manager.reserve_or_get_run(
            conversation_id="conv-1",
            kind=RunKind.WORKFLOW,
            idempotency_key="op_close",
            request_fingerprint="a" * 64,
        )
        result = await manager.close(timeout=1)
        with pytest.raises(RuntimeError, match="run manager is closing"):
            await manager.create_run(
                conversation_id="conv-1",
                kind=RunKind.CHAT,
            )
        with pytest.raises(RuntimeError, match="run manager is closing"):
            await manager.reserve_or_get_run(
                conversation_id="conv-1",
                kind=RunKind.CHAT,
                idempotency_key="op_after_close",
                request_fingerprint="b" * 64,
            )
        return manager, record, result

    manager, record, result = asyncio.run(run())
    assert result.pending_run_ids == (record.run_id,)
    assert result.exhausted_run_ids == ()
    assert manager.get_run(record.run_id)["status"] == RunStatus.INTERRUPTED.value


def test_repository_reservation_hook_is_post_commit_and_interrupts_marker_only_row(tmp_path):
    async def run():
        manager, repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        reserved_run_id = None

        def fail_after_commit(run_id):
            nonlocal reserved_run_id
            reserved_run_id = run_id
            assert repository.get_run(run_id) is not None
            assert run_id in manager._pending_reservations
            assert run_id not in manager._runs
            assert manager.get_run(run_id) is None
            raise RuntimeError("post-commit cache fault")

        with pytest.raises(RuntimeError, match="post-commit cache fault"):
            await manager.reserve_or_get_run(
                conversation_id=conversation_id,
                kind=RunKind.CHAT,
                anchor_node_id=node_id,
                idempotency_key="op_repository_hook",
                request_fingerprint="2" * 64,
                on_reserved=fail_after_commit,
            )
        assert manager.list_runs() == []
        assert manager.list_active() == []
        with pytest.raises(RunNotFoundError):
            manager.read_events(reserved_run_id, 0)
        subscription = manager.subscribe(reserved_run_id, 0)
        with pytest.raises(RunNotFoundError):
            await anext(subscription)
        await subscription.aclose()
        interrupted = await manager.interrupt_reserved_run(
            reserved_run_id,
            "post-commit cache fault",
        )
        replay, created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=node_id,
            idempotency_key="op_repository_hook",
            request_fingerprint="2" * 64,
        )
        await manager.close()
        return repository, interrupted, replay, created

    repository, interrupted, replay, created = asyncio.run(run())
    assert created is False
    assert replay.run_id == interrupted.run_id
    assert replay.status == RunStatus.INTERRUPTED
    assert repository.get_run(replay.run_id)["status"] == RunStatus.INTERRUPTED.value
    events = repository.read_events(replay.run_id, 0)
    assert [event["payload"]["type"] for event in events] == ["run_finished"]


def test_recovery_lookup_hydrates_repository_only_pending_without_exposing_it(tmp_path):
    async def run():
        manager, repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        reserved_run_id = None

        def fail_after_commit(run_id):
            nonlocal reserved_run_id
            reserved_run_id = run_id
            raise RuntimeError("post-commit cache fault")

        with pytest.raises(RuntimeError, match="post-commit cache fault"):
            await manager.reserve_or_get_run(
                conversation_id=conversation_id,
                kind=RunKind.CHAT,
                anchor_node_id=node_id,
                idempotency_key="op_repository_recovery",
                request_fingerprint="2" * 64,
                on_reserved=fail_after_commit,
            )

        assert reserved_run_id not in manager._runs
        recovered = await manager.get_run_for_recovery(reserved_run_id)
        still_hidden = manager.get_run(reserved_run_id)
        await manager.interrupt_reserved_run(reserved_run_id, "test cleanup")
        await manager.close()
        return reserved_run_id, recovered, still_hidden

    reserved_run_id, recovered, still_hidden = asyncio.run(run())
    assert recovered.run_id == reserved_run_id
    assert recovered.status == RunStatus.RUNNING
    assert still_hidden is None


def test_recovery_lookup_hydrates_repository_only_terminal_run(tmp_path):
    async def run():
        persistence = SQLitePersistence(tmp_path)
        persistence.initialize()
        chat = ChatRepository(persistence)
        repository = SQLiteRunRepository(persistence)
        conversation_id = chat.create_conversation(title="Runs")
        stored, created = repository.create_or_get_run(
            conversation_id,
            kind=RunKind.SUBAGENT.value,
            idempotency_key="op_terminal_recovery",
            request_fingerprint="3" * 64,
        )
        assert created is True
        repository.finish_run(stored["run_id"], RunStatus.INTERRUPTED.value)
        manager = RunManager(repository=repository)

        assert stored["run_id"] not in manager._runs
        recovered = await manager.get_run_for_recovery(stored["run_id"])
        await manager.close()
        return stored["run_id"], recovered

    run_id, recovered = asyncio.run(run())
    assert recovered.run_id == run_id
    assert recovered.status == RunStatus.INTERRUPTED


def test_recovery_lookup_returns_none_after_conversation_cascade(
    tmp_path,
    monkeypatch,
):
    async def run():
        persistence = SQLitePersistence(tmp_path)
        persistence.initialize()
        chat = ChatRepository(persistence)
        repository = SQLiteRunRepository(persistence)
        conversation_id = chat.create_conversation(title="Runs")
        node_id = chat.create_node(conversation_id, parent_id=None)
        manager = RunManager(repository=repository)
        record, _created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=node_id,
            target_node_id=node_id,
            idempotency_key="op_cascade_recovery",
            request_fingerprint="4" * 64,
        )

        async def fail_start_event(_run_id, _payload):
            raise RuntimeError("start event failed")

        monkeypatch.setattr(manager, "append_event", fail_start_event)
        with pytest.raises(RuntimeError, match="start event failed"):
            await manager.publish_reserved_run(record.run_id)
        assert manager._publication_tasks[record.run_id].done()
        assert record.run_id in manager._runs
        assert chat.delete_conversation(conversation_id) is True
        assert repository.get_run(record.run_id) is None
        with pytest.raises(KeyError):
            await manager.interrupt_reserved_run(record.run_id, "cascade cleanup")
        recovered = await manager.get_run_for_recovery(record.run_id)
        close_result = await manager.close()
        return manager, record, recovered, close_result

    manager, record, recovered, close_result = asyncio.run(run())
    assert recovered is None
    assert manager.get_run(record.run_id) is None
    assert record.run_id not in manager._pending_reservations
    assert "op_cascade_recovery" not in manager._idempotent_runs
    assert record.run_id not in manager._writers_by_node.values()
    assert record.run_id not in manager._publication_tasks
    assert record.run_id not in manager._interruption_tasks
    assert record.run_id not in manager._interrupting_reservation_ids
    assert close_result.pending_run_ids == ()
    assert close_result.exhausted_run_ids == ()
    assert manager._event_writer._closed is True


def test_cascade_after_enqueue_does_not_poison_repository_event_writer(
    tmp_path,
    monkeypatch,
):
    async def run():
        persistence = SQLitePersistence(tmp_path)
        persistence.initialize()
        chat = ChatRepository(persistence)
        repository = SQLiteRunRepository(persistence)
        conversation_id = chat.create_conversation(title="Deleted")
        node_id = chat.create_node(conversation_id, parent_id=None)
        manager = RunManager(repository=repository)
        manager._event_writer._flush_interval_seconds = 60
        record, _created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=node_id,
            idempotency_key="op_enqueued_cascade",
            request_fingerprint="5" * 64,
        )
        entered_flush = asyncio.Event()
        release_flush = asyncio.Event()
        original_flush = manager.flush_run_events

        async def blocked_flush(run_id):
            if run_id == record.run_id:
                entered_flush.set()
                await release_flush.wait()
            await original_flush(run_id)

        monkeypatch.setattr(manager, "flush_run_events", blocked_flush)
        publication = asyncio.create_task(manager.publish_reserved_run(record.run_id))
        await asyncio.wait_for(entered_flush.wait(), timeout=1)
        assert chat.delete_conversation(conversation_id) is True
        release_flush.set()
        with pytest.raises(RuntimeError, match="run event writer failed"):
            await publication

        assert await manager.get_run_for_recovery(record.run_id) is None
        assert manager._event_writer._error is None

        next_conversation_id = chat.create_conversation(title="Still writable")
        next_node_id = chat.create_node(next_conversation_id, parent_id=None)
        next_record, _created = await manager.reserve_or_get_run(
            conversation_id=next_conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=next_node_id,
            idempotency_key="op_after_enqueued_cascade",
            request_fingerprint="6" * 64,
        )
        await manager.publish_reserved_run(next_record.run_id)
        close_result = await manager.close()
        return manager, repository, next_record, close_result

    manager, repository, next_record, close_result = asyncio.run(run())
    assert manager.get_run(next_record.run_id) is not None
    assert [
        event["payload"]["type"]
        for event in repository.read_events(next_record.run_id, 0)
    ] == ["run_started"]
    assert close_result.exhausted_run_ids == ()
    assert manager._event_writer._closed is True


def test_repository_publication_waits_for_flush_and_persists_one_start(tmp_path, monkeypatch):
    async def run():
        manager, repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        record, created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=node_id,
            idempotency_key="op_flush_gate",
            request_fingerprint="3" * 64,
        )
        entered_flush = asyncio.Event()
        release_flush = asyncio.Event()
        original_flush = manager.flush_run_events

        async def blocked_flush(run_id):
            entered_flush.set()
            await release_flush.wait()
            await original_flush(run_id)

        monkeypatch.setattr(manager, "flush_run_events", blocked_flush)
        publication = asyncio.create_task(manager.publish_reserved_run(record.run_id))
        await asyncio.wait_for(entered_flush.wait(), timeout=1)
        assert publication.done() is False
        publications = [publication] + [
            asyncio.create_task(manager.publish_reserved_run(record.run_id))
            for _ in range(11)
        ]
        await asyncio.sleep(0)
        assert all(task.done() is False for task in publications)
        assert manager.get_run(record.run_id) is None
        assert manager.list_runs() == []
        assert manager.list_active() == []
        with pytest.raises(RunNotFoundError):
            manager.read_events(record.run_id, 0)
        subscription = manager.subscribe(record.run_id, 0)
        with pytest.raises(RunNotFoundError):
            await anext(subscription)
        await subscription.aclose()
        release_flush.set()
        published_results = await asyncio.wait_for(
            asyncio.gather(*publications),
            timeout=1,
        )
        published = published_results[0]
        assert {item.run_id for item in published_results} == {record.run_id}
        duplicate = await manager.publish_reserved_run(record.run_id)
        public = manager.get_run(record.run_id)
        await manager.close()
        return repository, record, created, published, duplicate, public

    repository, record, created, published, duplicate, public = asyncio.run(run())
    assert created is True
    assert published.run_id == record.run_id == duplicate.run_id
    assert "idempotency_key" not in public
    assert "request_fingerprint" not in public
    events = repository.read_events(record.run_id, 0)
    assert [event["payload"]["type"] for event in events] == ["run_started"]


def test_repository_publish_failure_stays_hidden_then_interrupts_in_order(tmp_path, monkeypatch):
    async def run():
        manager, repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        record, _created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.SUBAGENT,
            anchor_node_id=node_id,
            idempotency_key="op_publish_failure",
            request_fingerprint="4" * 64,
        )
        original_flush = manager.flush_run_events

        async def fail_flush(_run_id):
            raise RuntimeError("forced flush failure")

        monkeypatch.setattr(manager, "flush_run_events", fail_flush)
        with pytest.raises(RuntimeError, match="forced flush failure"):
            await manager.publish_reserved_run(record.run_id)
        with pytest.raises(RuntimeError, match="forced flush failure"):
            await manager.publish_reserved_run(record.run_id)
        assert manager.get_run(record.run_id) is None
        assert manager.list_runs() == []
        monkeypatch.setattr(manager, "flush_run_events", original_flush)
        interrupted = await manager.interrupt_reserved_run(
            record.run_id,
            "forced flush failure",
        )
        public_events = manager.read_events(record.run_id, 0)
        persisted_events = repository.read_events(record.run_id, 0)
        await manager.close()
        return interrupted, public_events, persisted_events

    interrupted, public_events, persisted_events = asyncio.run(run())
    assert interrupted.status == RunStatus.INTERRUPTED
    assert [event["type"] for event in public_events] == [
        "run_started",
        "run_finished",
    ]
    assert [event["payload"]["type"] for event in persisted_events] == [
        "run_started",
        "run_finished",
    ]


def test_repository_target_admission_does_not_commit_different_key(tmp_path):
    async def run():
        manager, repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        first, created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            target_node_id=node_id,
            idempotency_key="op_repository_target",
            request_fingerprint="5" * 64,
        )
        same, same_created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            target_node_id=node_id,
            idempotency_key="op_repository_target",
            request_fingerprint="5" * 64,
        )
        with pytest.raises(RunWriterConflictError):
            await manager.reserve_or_get_run(
                conversation_id=conversation_id,
                kind=RunKind.CHAT,
                target_node_id=node_id,
                idempotency_key="op_repository_other",
                request_fingerprint="6" * 64,
            )
        rows = repository.list_runs(conversation_id)
        await manager.interrupt_reserved_run(first.run_id, "test cleanup")
        await manager.close()
        return first, created, same, same_created, rows

    first, created, same, same_created, rows = asyncio.run(run())
    assert created is True
    assert same_created is False
    assert first.run_id == same.run_id
    assert [row["run_id"] for row in rows] == [first.run_id]


def test_close_waits_for_inflight_publication_then_interrupts_before_writer_close(
    tmp_path,
    monkeypatch,
):
    async def run():
        manager, repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        record, _created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.WORKFLOW,
            anchor_node_id=node_id,
            idempotency_key="op_close_publish_race",
            request_fingerprint="7" * 64,
        )
        entered_flush = asyncio.Event()
        release_flush = asyncio.Event()
        original_flush = manager.flush_run_events

        async def blocked_flush(run_id):
            entered_flush.set()
            await release_flush.wait()
            await original_flush(run_id)

        monkeypatch.setattr(manager, "flush_run_events", blocked_flush)
        publication = asyncio.create_task(manager.publish_reserved_run(record.run_id))
        await asyncio.wait_for(entered_flush.wait(), timeout=1)
        close_task = asyncio.create_task(manager.close(timeout=1))
        await asyncio.sleep(0)
        assert close_task.done() is False
        with pytest.raises(RuntimeError, match="run manager is closing"):
            await manager.create_run(
                conversation_id=conversation_id,
                kind=RunKind.CHAT,
            )
        release_flush.set()
        with pytest.raises(RuntimeError, match="run manager is closing"):
            await publication
        result = await asyncio.wait_for(close_task, timeout=1)
        return manager, repository, record, result

    manager, repository, record, result = asyncio.run(run())
    assert result.pending_run_ids == (record.run_id,)
    assert result.exhausted_run_ids == ()
    assert manager.get_run(record.run_id)["status"] == RunStatus.INTERRUPTED.value
    assert manager._event_writer._closed is True
    assert [
        event["payload"]["type"]
        for event in repository.read_events(record.run_id, 0)
    ] == ["run_started", "run_finished"]


def test_close_reports_exhausted_pending_ids_without_closing_writer(tmp_path, monkeypatch):
    async def run():
        manager, _repository, conversation_id, node_id = _sqlite_run_manager(tmp_path)
        record, _created = await manager.reserve_or_get_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            anchor_node_id=node_id,
            idempotency_key="op_close_timeout",
            request_fingerprint="8" * 64,
        )
        release_interrupt = asyncio.Event()
        original_interrupt = manager.interrupt_reserved_run

        async def blocked_interrupt(run_id, error):
            await release_interrupt.wait()
            return await original_interrupt(run_id, error)

        monkeypatch.setattr(manager, "interrupt_reserved_run", blocked_interrupt)
        result = await manager.close(timeout=0.01)
        writer_closed_at_return = manager._event_writer._closed
        release_interrupt.set()
        for _ in range(100):
            if record.run_id not in manager._pending_reservations:
                break
            await asyncio.sleep(0.001)
        retry_result = await manager.close(timeout=1)
        return manager, record, result, retry_result, writer_closed_at_return

    manager, record, result, retry_result, writer_closed_at_return = asyncio.run(run())
    assert result.pending_run_ids == (record.run_id,)
    assert result.exhausted_run_ids == (record.run_id,)
    assert writer_closed_at_return is False
    assert retry_result.exhausted_run_ids == ()
    assert manager._event_writer._closed is True
