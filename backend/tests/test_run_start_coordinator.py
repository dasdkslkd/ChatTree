from __future__ import annotations

import asyncio

import pytest

from backend.core.runs import (
    ProducerRegistry,
    RunIdempotency,
    RunIdempotencyConflictError,
    RunKind,
    RunManager,
    RunStartCoordinator,
    RunStartReservationError,
    RunStartSchedulingError,
    RunStartSpec,
    RunStatus,
)
from backend.core.runs.repository import MemoryRunRepository


def _spec(
    key: str = "message-key",
    fingerprint: str = "fingerprint-a",
    *,
    request_id: str = "request-tree",
) -> RunStartSpec:
    return RunStartSpec(
        conversation_id="conv-1",
        kind=RunKind.CHAT,
        anchor_node_id="node-1",
        summary="hello",
        idempotency=RunIdempotency(key, fingerprint),
        request_id=request_id,
    )


def _runtime() -> tuple[RunManager, ProducerRegistry, RunStartCoordinator]:
    manager = RunManager(repository=MemoryRunRepository())
    registry = ProducerRegistry(manager)
    return manager, registry, RunStartCoordinator(manager, registry)


async def _close(
    manager: RunManager,
    registry: ProducerRegistry,
    coordinator: RunStartCoordinator,
) -> None:
    await coordinator.close()
    await registry.close()
    await manager.close()


def test_concurrent_same_key_runs_one_bootstrap_and_returns_canonical_run():
    async def scenario() -> None:
        manager, registry, coordinator = _runtime()
        bootstrap_entered = asyncio.Event()
        release_bootstrap = asyncio.Event()
        release_producer = asyncio.Event()
        bootstrap_calls = 0

        async def bootstrap(record):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            bootstrap_entered.set()
            await release_bootstrap.wait()

            async def producer() -> None:
                await release_producer.wait()
                await manager.finish_run(record.run_id, RunStatus.COMPLETED)

            return asyncio.create_task(producer())

        first = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await bootstrap_entered.wait()
        second = asyncio.create_task(
            coordinator.start(_spec(request_id="retry-tree"), bootstrap)
        )
        await asyncio.sleep(0)
        assert not second.done()

        release_bootstrap.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result.run.run_id == second_result.run.run_id
        assert sorted([first_result.created, second_result.created]) == [False, True]
        assert bootstrap_calls == 1
        assert registry.task_for(first_result.run.run_id) is not None

        release_producer.set()
        await registry.task_for(first_result.run.run_id)
        assert manager.get_run(first_result.run.run_id)["status"] == "completed"
        await _close(manager, registry, coordinator)

    asyncio.run(scenario())


def test_same_key_different_fingerprint_returns_conflict_with_winner_run():
    async def scenario() -> None:
        manager, registry, coordinator = _runtime()
        bootstrap_entered = asyncio.Event()
        release_bootstrap = asyncio.Event()
        release_producer = asyncio.Event()

        async def bootstrap(record):
            bootstrap_entered.set()
            await release_bootstrap.wait()

            async def producer() -> None:
                await release_producer.wait()
                await manager.finish_run(record.run_id, RunStatus.COMPLETED)

            return asyncio.create_task(producer())

        winner = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await bootstrap_entered.wait()
        conflicting = asyncio.create_task(
            coordinator.start(_spec(fingerprint="fingerprint-b"), bootstrap)
        )
        release_bootstrap.set()
        winner_result = await winner

        with pytest.raises(RunIdempotencyConflictError) as exc_info:
            await conflicting
        assert exc_info.value.existing_run_id == winner_result.run.run_id

        release_producer.set()
        await registry.task_for(winner_result.run.run_id)
        await _close(manager, registry, coordinator)

    asyncio.run(scenario())


def test_http_request_cancellation_does_not_cancel_accepted_bootstrap():
    async def scenario() -> None:
        manager, registry, coordinator = _runtime()
        bootstrap_entered = asyncio.Event()
        release_bootstrap = asyncio.Event()
        release_producer = asyncio.Event()
        bootstrap_calls = 0

        async def bootstrap(record):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            bootstrap_entered.set()
            await release_bootstrap.wait()

            async def producer() -> None:
                await release_producer.wait()
                await manager.finish_run(record.run_id, RunStatus.COMPLETED)

            return asyncio.create_task(producer())

        request = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await bootstrap_entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        release_bootstrap.set()
        replay = await coordinator.replay_existing(_spec().idempotency)
        assert replay is not None
        assert replay.created is False
        assert bootstrap_calls == 1
        assert registry.task_for(replay.run.run_id) is not None

        release_producer.set()
        await registry.task_for(replay.run.run_id)
        await _close(manager, registry, coordinator)

    asyncio.run(scenario())


def test_bootstrap_failure_terminalizes_same_run_and_never_retries_side_effect():
    async def scenario() -> None:
        manager, registry, coordinator = _runtime()
        calls = 0

        async def bootstrap(_record):
            nonlocal calls
            calls += 1
            raise RuntimeError("schedule failed")

        with pytest.raises(RunStartSchedulingError) as exc_info:
            await coordinator.start(_spec(), bootstrap)

        run_id = exc_info.value.run_id
        assert manager.get_run(run_id)["status"] == "interrupted"
        replay = await coordinator.start(
            _spec(request_id="retry-tree"),
            bootstrap,
        )
        assert replay.created is False
        assert replay.run.run_id == run_id
        assert replay.run.status is RunStatus.INTERRUPTED
        assert calls == 1
        assert registry.task_for(run_id) is None
        await _close(manager, registry, coordinator)

    asyncio.run(scenario())


def test_replay_retries_terminalization_that_failed_after_bootstrap():
    class FailFirstFinishRepository(MemoryRunRepository):
        def __init__(self) -> None:
            super().__init__()
            self.fail_run_id: str | None = None
            self.failed = False

        def finish_run(self, run_id, status, error=None):
            if run_id == self.fail_run_id and not self.failed:
                self.failed = True
                raise OSError("terminal persistence unavailable")
            return super().finish_run(run_id, status, error)

    async def scenario() -> None:
        repository = FailFirstFinishRepository()
        manager = RunManager(repository=repository)
        registry = ProducerRegistry(manager)
        coordinator = RunStartCoordinator(manager, registry)
        calls = 0

        async def bootstrap(record):
            nonlocal calls
            calls += 1
            repository.fail_run_id = record.run_id
            raise RuntimeError("schedule failed")

        with pytest.raises(RunStartSchedulingError) as exc_info:
            await coordinator.start(_spec(), bootstrap)

        replay = await coordinator.start(
            _spec(request_id="retry-tree"),
            bootstrap,
        )
        assert replay.run.run_id == exc_info.value.run_id
        assert replay.run.status is RunStatus.INTERRUPTED
        assert replay.created is False
        assert calls == 1
        assert await coordinator.close() == ()
        assert await registry.close() == ()
        await manager.close()

    asyncio.run(scenario())


def test_close_reports_bootstrap_terminalization_failure_after_request_is_gone():
    class FailingFinishRepository(MemoryRunRepository):
        def __init__(self) -> None:
            super().__init__()
            self.fail_run_id: str | None = None

        def finish_run(self, run_id, status, error=None):
            if run_id == self.fail_run_id:
                raise OSError("terminal persistence unavailable")
            return super().finish_run(run_id, status, error)

    async def scenario() -> None:
        repository = FailingFinishRepository()
        manager = RunManager(repository=repository)
        registry = ProducerRegistry(manager)
        coordinator = RunStartCoordinator(manager, registry)
        calls = 0

        async def bootstrap(record):
            nonlocal calls
            calls += 1
            repository.fail_run_id = record.run_id
            raise RuntimeError("schedule failed")

        with pytest.raises(RunStartSchedulingError) as exc_info:
            await coordinator.start(_spec(), bootstrap)
        await asyncio.sleep(0)
        run_id = exc_info.value.run_id
        assert coordinator._request_tasks == {}

        with pytest.raises(RunStartSchedulingError):
            await coordinator.start(_spec(request_id="retry-tree"), bootstrap)
        assert calls == 1
        assert await coordinator.close(timeout=0.2) == (run_id,)
        assert repository.get_run(run_id)["status"] == RunStatus.RUNNING.value
        repository.fail_run_id = None
        await manager.finish_run(run_id, RunStatus.INTERRUPTED)
        assert await registry.close() == ()
        await manager.close()

    asyncio.run(scenario())


def test_reservation_failure_has_no_canonical_run_or_bootstrap(monkeypatch):
    async def scenario() -> None:
        manager, registry, coordinator = _runtime()
        bootstrap_calls = 0

        async def fail_reservation(**_kwargs):
            raise OSError("database unavailable")

        async def bootstrap(_record):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            raise AssertionError("bootstrap must not run")

        monkeypatch.setattr(manager, "reserve_or_get_run", fail_reservation)
        with pytest.raises(RunStartReservationError):
            await coordinator.start(_spec(), bootstrap)

        assert bootstrap_calls == 0
        assert manager.list_runs() == []
        await _close(manager, registry, coordinator)

    asyncio.run(scenario())


def test_close_cancels_bootstrap_and_terminalizes_reserved_run():
    async def scenario() -> None:
        manager, registry, coordinator = _runtime()
        bootstrap_entered = asyncio.Event()
        never_release = asyncio.Event()

        async def bootstrap(_record):
            bootstrap_entered.set()
            await never_release.wait()
            raise AssertionError("unreachable")

        start = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await bootstrap_entered.wait()
        unresolved = await coordinator.close(timeout=1)
        await asyncio.gather(start, return_exceptions=True)

        runs = manager.list_runs()
        assert unresolved == ()
        assert len(runs) == 1
        assert runs[0]["status"] == "interrupted"
        assert registry.task_for(runs[0]["run_id"]) is None
        await registry.close()
        await manager.close()

    asyncio.run(scenario())


def test_close_during_post_commit_reservation_terminalizes_canonical_run():
    async def scenario() -> None:
        manager, registry, coordinator = _runtime()
        reservation_committed = asyncio.Event()
        never_release = asyncio.Event()
        original_append_event = manager.append_event

        async def block_run_started(run_id, payload):
            if payload.get("type") == "run_started":
                reservation_committed.set()
                await never_release.wait()
            return await original_append_event(run_id, payload)

        manager.append_event = block_run_started

        async def bootstrap(_record):
            raise AssertionError("bootstrap must not run before reservation publishes")

        start = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await asyncio.wait_for(reservation_committed.wait(), timeout=1)
        reserved = manager.list_runs()
        assert len(reserved) == 1
        assert reserved[0]["status"] == "running"

        assert await coordinator.close(timeout=1) == ()
        await asyncio.gather(start, return_exceptions=True)

        canonical = manager.get_run(reserved[0]["run_id"])
        assert canonical["status"] == "interrupted"
        assert manager.read_events(canonical["run_id"], 0)[-1]["status"] == "interrupted"
        assert await registry.close() == ()
        await manager.close()

    asyncio.run(scenario())


def test_event_writer_failure_still_terminalizes_committed_reservation():
    class FailingIndexedRepository(MemoryRunRepository):
        def append_indexed_events(self, run_id, events):
            raise OSError("event store unavailable")

    async def scenario() -> None:
        repository = FailingIndexedRepository()
        manager = RunManager(repository=repository)
        registry = ProducerRegistry.for_run_manager(manager)
        coordinator = RunStartCoordinator(manager, registry)
        bootstrap_calls = 0

        async def bootstrap(_record):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            raise AssertionError("bootstrap must not run")

        with pytest.raises(RunStartSchedulingError) as raised:
            await coordinator.start(_spec(), bootstrap)

        run_id = raised.value.run_id
        assert bootstrap_calls == 0
        assert manager.get_run(run_id)["status"] == "interrupted"
        persisted = repository.get_run(run_id)
        assert persisted["status"] == "interrupted"
        assert persisted["event_count"] == 1
        assert repository.read_events(run_id, 0)[0]["payload"]["status"] == "interrupted"
        assert await coordinator.close() == ()
        assert await registry.close() == ()
        await manager.close()

    asyncio.run(scenario())


def test_repository_loser_load_failure_does_not_interrupt_running_winner():
    class RacingRepository(MemoryRunRepository):
        def __init__(self):
            super().__init__()
            self.hide_lookup_once = True
            self.fail_read_once = True

        def get_run_by_idempotency_key(self, idempotency_key):
            if self.hide_lookup_once:
                self.hide_lookup_once = False
                return None
            return super().get_run_by_idempotency_key(idempotency_key)

        def read_events(self, run_id, from_event=0):
            if self.fail_read_once:
                self.fail_read_once = False
                raise OSError("transient replay failure")
            return super().read_events(run_id, from_event)

    async def scenario() -> None:
        repository = RacingRepository()
        winner, created = repository.create_or_get_run(
            "conv-1",
            kind=RunKind.CHAT.value,
            idempotency_key="message-key",
            request_fingerprint="fingerprint-a",
            anchor_node_id="node-1",
        )
        assert created is True
        manager = RunManager(repository=repository)
        registry = ProducerRegistry.for_run_manager(manager)
        coordinator = RunStartCoordinator(manager, registry)
        bootstrap_calls = 0

        async def bootstrap(_record):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            raise AssertionError("repository loser must not bootstrap")

        with pytest.raises(RunStartReservationError):
            await coordinator.start(_spec(), bootstrap)

        assert bootstrap_calls == 0
        assert repository.get_run(winner["run_id"])["status"] == "running"
        assert manager.get_run(winner["run_id"])["status"] == "running"
        await manager.finish_run(winner["run_id"], RunStatus.INTERRUPTED)
        assert await coordinator.close() == ()
        assert await registry.close() == ()
        await manager.close()

    asyncio.run(scenario())
