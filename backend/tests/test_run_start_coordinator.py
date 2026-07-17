from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import (
    RunBootstrapOutcome,
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


def _spec(
    *,
    key: str = "operation-1",
    fingerprint: str = "a" * 64,
    request_id: str = "request-1",
    conversation_id: str = "conversation-1",
) -> RunStartSpec:
    return RunStartSpec(
        conversation_id=conversation_id,
        kind=RunKind.CHAT,
        anchor_node_id=None,
        summary="hello",
        idempotency=RunIdempotency(key, fingerprint),
        request_id=request_id,
    )


async def _completed_producer(_run: Any) -> asyncio.Task[None]:
    return asyncio.create_task(asyncio.sleep(0))


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


def _sqlite_manager(tmp_path: Path) -> tuple[RunManager, str]:
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conversation_id = chat.create_conversation(title="Coordinator")
    return RunManager(repository=SQLiteRunRepository(persistence)), conversation_id


def test_bootstrap_outcome_requires_exactly_one_state():
    with pytest.raises(ValueError, match="exactly one state"):
        RunBootstrapOutcome()
    with pytest.raises(ValueError, match="exactly one state"):
        RunBootstrapOutcome(retry=True, error=RunStartSchedulingError("run-1"))
    with pytest.raises(ValueError, match="exactly one state"):
        RunBootstrapOutcome(retry=True, conflict_run_id="run-1")


def test_concurrent_same_fingerprint_waits_for_winner_bootstrap():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        entered = asyncio.Event()
        release = asyncio.Event()
        bootstrap_count = 0

        async def bootstrap(_run):
            nonlocal bootstrap_count
            bootstrap_count += 1
            entered.set()
            await release.wait()
            return asyncio.create_task(asyncio.sleep(0))

        spec = _spec()
        winner = asyncio.create_task(coordinator.start(spec, bootstrap))
        await entered.wait()
        loser = asyncio.create_task(coordinator.start(spec, bootstrap))
        await asyncio.sleep(0)
        assert not winner.done()
        assert not loser.done()

        release.set()
        first, second = await asyncio.wait_for(
            asyncio.gather(winner, loser), timeout=1
        )
        assert first.run.run_id == second.run.run_id
        assert {first.created, second.created} == {True, False}
        assert bootstrap_count == 1
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


def test_different_fingerprint_waits_then_conflicts_with_canonical_run():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def bootstrap(_run):
            entered.set()
            await release.wait()
            return asyncio.create_task(asyncio.sleep(0))

        winner = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await entered.wait()
        loser = asyncio.create_task(
            coordinator.start(
                _spec(fingerprint="b" * 64, request_id="request-conflict"),
                bootstrap,
            )
        )
        await asyncio.sleep(0)
        assert not loser.done()

        release.set()
        created = await winner
        with pytest.raises(RunIdempotencyConflictError) as raised:
            await loser
        assert raised.value.existing_run_id == created.run.run_id
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


def test_existing_canonical_conflict_coalesces_all_same_fingerprint_callers(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        canonical = await coordinator.start(_spec(), _completed_producer)
        original_reserve = manager.reserve_or_get_run
        reserve_calls = 0

        async def delayed_reserve(**kwargs):
            nonlocal reserve_calls
            reserve_calls += 1
            await asyncio.sleep(0.005)
            return await original_reserve(**kwargs)

        monkeypatch.setattr(manager, "reserve_or_get_run", delayed_reserve)
        conflict_spec = _spec(
            fingerprint="b" * 64,
            request_id="conflicting-request",
        )
        results = await asyncio.gather(
            *(
                coordinator.start(conflict_spec, _completed_producer)
                for _ in range(20)
            ),
            return_exceptions=True,
        )

        assert reserve_calls == 1
        assert all(
            isinstance(item, RunIdempotencyConflictError) for item in results
        )
        assert {
            item.existing_run_id
            for item in results
            if isinstance(item, RunIdempotencyConflictError)
        } == {canonical.run.run_id}
        assert not any(
            isinstance(item, RunStartReservationError) for item in results
        )
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


def test_caller_cancellation_does_not_cancel_post_reservation_work():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def bootstrap(_run):
            entered.set()
            await release.wait()
            return asyncio.create_task(asyncio.sleep(0))

        spec = _spec()
        caller = asyncio.create_task(coordinator.start(spec, bootstrap))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        release.set()
        await _wait_until(lambda: not coordinator._request_tasks)
        replay = await coordinator.start(spec, bootstrap)
        assert replay.created is False
        assert replay.run.status == RunStatus.RUNNING
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


def test_cancelled_caller_then_bootstrap_failure_has_no_loop_exception():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        entered = asyncio.Event()
        release = asyncio.Event()
        loop_errors: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )

        async def bootstrap(_run):
            entered.set()
            await release.wait()
            raise RuntimeError("late bootstrap failure")

        caller = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        release.set()
        await _wait_until(lambda: not coordinator._request_tasks)
        await asyncio.sleep(0)
        assert loop_errors == []
        assert coordinator._bootstrap_barriers == {}
        runs = manager.list_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == RunStatus.INTERRUPTED.value

    asyncio.run(run())


@pytest.mark.parametrize("return_kind", ["coroutine", "future", "value"])
def test_bootstrap_must_return_actual_asyncio_task(return_kind: str):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)

        async def bootstrap(_run):
            if return_kind == "coroutine":
                return asyncio.sleep(0)
            if return_kind == "future":
                future = asyncio.get_running_loop().create_future()
                future.set_result(None)
                return future
            return None

        with pytest.raises(RunStartSchedulingError) as raised:
            await coordinator.start(_spec(), bootstrap)
        recovered = await manager.get_run_for_recovery(raised.value.run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.INTERRUPTED
        assert coordinator._producer_tasks == {}
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


def test_bootstrap_failure_interrupts_published_run_and_replay_skips_bootstrap():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        bootstrap_calls = 0

        async def bootstrap(_run):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            raise RuntimeError("bootstrap failed")

        spec = _spec()
        with pytest.raises(RunStartSchedulingError) as raised:
            await coordinator.start(spec, bootstrap)
        run_id = raised.value.run_id
        recovered = await manager.get_run_for_recovery(run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.INTERRUPTED

        replay = await coordinator.start(spec, bootstrap)
        assert replay.created is False
        assert replay.run.run_id == run_id
        assert replay.run.status == RunStatus.INTERRUPTED
        assert bootstrap_calls == 1
        assert coordinator._bootstrap_barriers == {}
        assert sum(
            event.get("type") == "run_finished"
            for event in manager.read_events(run_id)
        ) == 1

    asyncio.run(run())


def test_pre_id_failure_retries_waiters_once_and_reaches_real_conflict(monkeypatch):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        original = manager.reserve_or_get_run
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def fail_first(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await release_first.wait()
                raise RuntimeError("pre-id failure")
            return await original(**kwargs)

        monkeypatch.setattr(manager, "reserve_or_get_run", fail_first)
        owner = asyncio.create_task(coordinator.start(_spec(), _completed_producer))
        await first_entered.wait()
        same = asyncio.create_task(
            coordinator.start(
                _spec(request_id="request-same"), _completed_producer
            )
        )
        await asyncio.sleep(0)
        different = asyncio.create_task(
            coordinator.start(
                _spec(fingerprint="b" * 64, request_id="request-different"),
                _completed_producer,
            )
        )
        await asyncio.sleep(0)
        release_first.set()

        with pytest.raises(RunStartReservationError):
            await owner
        winner = await asyncio.wait_for(same, timeout=1)
        with pytest.raises(RunIdempotencyConflictError) as raised:
            await asyncio.wait_for(different, timeout=1)
        assert raised.value.existing_run_id == winner.run.run_id
        assert calls == 2
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


def test_persistent_pre_id_failure_is_bounded_for_every_waiter(monkeypatch):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        entered = [asyncio.Event(), asyncio.Event()]
        releases = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def always_fail(**_kwargs):
            nonlocal calls
            index = calls
            calls += 1
            if index < len(entered):
                entered[index].set()
                await releases[index].wait()
            raise RuntimeError("persistent pre-id failure")

        monkeypatch.setattr(manager, "reserve_or_get_run", always_fail)
        first = asyncio.create_task(coordinator.start(_spec(), _completed_producer))
        await entered[0].wait()
        second = asyncio.create_task(
            coordinator.start(_spec(request_id="request-2"), _completed_producer)
        )
        third = asyncio.create_task(
            coordinator.start(_spec(request_id="request-3"), _completed_producer)
        )
        await asyncio.sleep(0)
        releases[0].set()
        await entered[1].wait()
        releases[1].set()

        results = await asyncio.wait_for(
            asyncio.gather(first, second, third, return_exceptions=True), timeout=1
        )
        assert all(isinstance(item, RunStartReservationError) for item in results)
        assert calls == 2
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


@pytest.mark.parametrize("fault_point", ["reserve", "publish"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("storage", ["memory", "sqlite"])
def test_post_id_fault_terminalizes_once_and_wakes_loser(
    monkeypatch,
    tmp_path,
    fault_point: str,
    cancelled: bool,
    storage: str,
):
    async def run() -> None:
        if storage == "sqlite":
            manager, conversation_id = _sqlite_manager(tmp_path / fault_point / str(cancelled))
        else:
            manager, conversation_id = RunManager(), "conversation-1"
        coordinator = RunStartCoordinator(manager)
        original_reserve = manager.reserve_or_get_run
        original_publish = manager.publish_reserved_run
        fault_entered = asyncio.Event()
        release_fault = asyncio.Event()
        reserved_id: str | None = None
        pending_seen = False
        producer_count = 0
        fault_injected = False

        def fault() -> BaseException:
            if cancelled:
                return asyncio.CancelledError()
            return RuntimeError(f"{fault_point} failed")

        async def reserve_fault(**kwargs):
            nonlocal reserved_id, pending_seen, fault_injected
            record, created = await original_reserve(**kwargs)
            reserved_id = record.run_id
            if fault_point == "reserve" and not fault_injected:
                fault_injected = True
                pending_seen = record.run_id in manager._pending_reservations
                fault_entered.set()
                await release_fault.wait()
                raise fault()
            return record, created

        async def publish_fault(run_id: str):
            nonlocal reserved_id, pending_seen
            reserved_id = run_id
            pending_seen = run_id in manager._pending_reservations
            fault_entered.set()
            await release_fault.wait()
            raise fault()

        monkeypatch.setattr(manager, "reserve_or_get_run", reserve_fault)
        if fault_point == "publish":
            monkeypatch.setattr(manager, "publish_reserved_run", publish_fault)

        async def bootstrap(_run):
            nonlocal producer_count
            producer_count += 1
            return asyncio.create_task(asyncio.sleep(0))

        spec = _spec(conversation_id=conversation_id)
        owner = asyncio.create_task(coordinator.start(spec, bootstrap))
        await fault_entered.wait()
        loser = asyncio.create_task(coordinator.start(spec, bootstrap))
        await asyncio.sleep(0)
        release_fault.set()

        owner_result, loser_result = await asyncio.wait_for(
            asyncio.gather(owner, loser, return_exceptions=True), timeout=1
        )
        if cancelled:
            assert isinstance(owner_result, asyncio.CancelledError)
        else:
            assert isinstance(owner_result, RunStartSchedulingError)
        assert isinstance(loser_result, RunStartSchedulingError)
        assert reserved_id is not None
        assert owner_result.run_id == reserved_id if not cancelled else True
        assert loser_result.run_id == reserved_id
        assert pending_seen is True
        assert reserved_id not in manager._pending_reservations
        assert producer_count == 0

        replay = await coordinator.start(spec, bootstrap)
        assert replay.created is False
        assert replay.run.run_id == reserved_id
        assert replay.run.status == RunStatus.INTERRUPTED
        events = manager.read_events(reserved_id)
        assert sum(item.get("type") == "run_finished" for item in events) == 1
        assert coordinator._bootstrap_barriers == {}
        await manager.close()

    asyncio.run(run())


def test_interruption_failure_keeps_tombstone_until_terminal_or_missing(
    monkeypatch, caplog
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        original_interrupt = manager.interrupt_reserved_run
        original_publish = manager.publish_reserved_run
        reserved = asyncio.Event()
        release = asyncio.Event()

        async def publish_failure(run_id: str):
            reserved.set()
            await release.wait()
            raise RuntimeError("publication failed")

        async def interruption_failure(_run_id: str, _error: str):
            raise RuntimeError("interruption failed")

        monkeypatch.setattr(manager, "publish_reserved_run", publish_failure)
        monkeypatch.setattr(manager, "interrupt_reserved_run", interruption_failure)
        spec = _spec()
        owner = asyncio.create_task(coordinator.start(spec, _completed_producer))
        await reserved.wait()
        loser = asyncio.create_task(coordinator.start(spec, _completed_producer))
        await asyncio.sleep(0)
        release.set()

        results = await asyncio.wait_for(
            asyncio.gather(owner, loser, return_exceptions=True), timeout=1
        )
        assert all(isinstance(item, RunStartSchedulingError) for item in results)
        run_id = results[0].run_id
        assert results[1].run_id == run_id
        assert run_id in coordinator._bootstrap_failures
        assert run_id in manager._pending_reservations
        assert coordinator._bootstrap_barriers == {}

        replay_error = await asyncio.gather(
            coordinator.start(spec, _completed_producer), return_exceptions=True
        )
        assert isinstance(replay_error[0], RunStartSchedulingError)
        assert replay_error[0].run_id == run_id

        monkeypatch.setattr(manager, "interrupt_reserved_run", original_interrupt)
        monkeypatch.setattr(manager, "publish_reserved_run", original_publish)
        await original_interrupt(run_id, "manual recovery")
        replay = await coordinator.start(spec, _completed_producer)
        assert replay.run.status == RunStatus.INTERRUPTED
        assert run_id not in coordinator._bootstrap_failures

        coordinator._bootstrap_failures["missing-run"] = RunStartSchedulingError(
            "missing-run"
        )
        await coordinator.start(
            _spec(key="unrelated", request_id="request-unrelated"),
            _completed_producer,
        )
        assert "missing-run" not in coordinator._bootstrap_failures

    with caplog.at_level("ERROR"):
        asyncio.run(run())
    assert "interruption failed" in caplog.text


@pytest.mark.parametrize("producer_failure", ["body", "finish"])
def test_producer_failure_is_retrieved_and_fail_closed(
    monkeypatch, producer_failure: str
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release = asyncio.Event()
        loop_errors: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        finish_calls = 0
        original_finish = manager.finish_run

        if producer_failure == "finish":
            async def fail_first_finish(run_id, status=RunStatus.COMPLETED, error=None, **kwargs):
                nonlocal finish_calls
                finish_calls += 1
                if finish_calls == 1:
                    raise RuntimeError("terminal finish failed")
                return await original_finish(run_id, status, error, **kwargs)

            monkeypatch.setattr(manager, "finish_run", fail_first_finish)

        async def bootstrap(run_record):
            async def produce() -> None:
                await release.wait()
                if producer_failure == "body":
                    raise RuntimeError("producer body failed")
                await manager.finish_run(run_record.run_id, RunStatus.COMPLETED)

            return asyncio.create_task(produce())

        result = await coordinator.start(_spec(), bootstrap)
        run_id = result.run.run_id
        assert coordinator._producer_tasks[run_id]
        release.set()
        await _wait_until(
            lambda: run_id not in coordinator._producer_tasks
            and not coordinator._producer_cleanup_tasks
        )
        recovered = await manager.get_run_for_recovery(run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.FAILED
        if producer_failure == "finish":
            assert finish_calls == 2
        await asyncio.sleep(0)
        assert loop_errors == []

    asyncio.run(run())


def test_producer_terminalization_failure_blocks_replay_and_close_retries(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release_producer = asyncio.Event()
        original_finish = manager.finish_run

        async def fail_finish(*_args, **_kwargs):
            raise RuntimeError("persistent finish failure")

        monkeypatch.setattr(manager, "finish_run", fail_finish)

        async def bootstrap(_run):
            async def producer() -> None:
                await release_producer.wait()
                raise RuntimeError("producer failed")

            return asyncio.create_task(producer())

        started = await coordinator.start(_spec(), bootstrap)
        run_id = started.run.run_id
        release_producer.set()
        await _wait_until(
            lambda: run_id not in coordinator._producer_tasks
            and not coordinator._producer_cleanup_tasks
        )
        assert run_id in coordinator._producer_terminalization_failures

        replay = await asyncio.gather(
            coordinator.start(_spec(), _completed_producer),
            return_exceptions=True,
        )
        assert isinstance(replay[0], RunStartSchedulingError)
        assert replay[0].run_id == run_id

        first_close = await coordinator.close(timeout=0.05)
        assert first_close.exhausted is True
        assert first_close.producer_run_ids == (run_id,)
        assert first_close.pending_run_ids == ()
        recovered = await manager.get_run_for_recovery(run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.RUNNING

        retry_entered = asyncio.Event()
        allow_retry = asyncio.Event()

        async def delayed_finish(*args, **kwargs):
            retry_entered.set()
            await allow_retry.wait()
            return await original_finish(*args, **kwargs)

        monkeypatch.setattr(manager, "finish_run", delayed_finish)
        second_close_task = asyncio.create_task(coordinator.close(timeout=0.01))
        await retry_entered.wait()
        second_close = await second_close_task
        assert second_close.exhausted is True
        assert second_close.producer_run_ids == (run_id,)
        assert coordinator._producer_cleanup_tasks

        allow_retry.set()
        await _wait_until(
            lambda: not coordinator._producer_cleanup_tasks
            and run_id not in coordinator._producer_terminalization_failures
        )
        third_close = await coordinator.close(timeout=1)
        assert third_close.exhausted is False
        assert coordinator._producer_terminalization_failures == {}
        recovered = await manager.get_run_for_recovery(run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.FAILED

    asyncio.run(run())


def test_replay_is_blocked_while_producer_terminalization_is_in_progress(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release_producer = asyncio.Event()
        finish_entered = asyncio.Event()
        allow_finish = asyncio.Event()
        original_finish = manager.finish_run

        async def blocked_finish(*args, **kwargs):
            finish_entered.set()
            await allow_finish.wait()
            return await original_finish(*args, **kwargs)

        monkeypatch.setattr(manager, "finish_run", blocked_finish)

        async def bootstrap(_run):
            async def producer() -> None:
                await release_producer.wait()
                raise RuntimeError("producer failed before blocked finish")

            return asyncio.create_task(producer())

        started = await coordinator.start(_spec(), bootstrap)
        run_id = started.run.run_id
        release_producer.set()
        await finish_entered.wait()
        assert run_id in coordinator._producer_terminalization_failures

        in_progress_replay = await asyncio.gather(
            coordinator.start(_spec(), _completed_producer),
            return_exceptions=True,
        )
        assert isinstance(in_progress_replay[0], RunStartSchedulingError)
        assert in_progress_replay[0].run_id == run_id

        allow_finish.set()
        await _wait_until(
            lambda: not coordinator._producer_cleanup_tasks
            and run_id not in coordinator._producer_terminalization_failures
        )
        replay = await coordinator.start(_spec(), _completed_producer)
        assert replay.created is False
        assert replay.run.run_id == run_id
        assert replay.run.status == RunStatus.FAILED

    asyncio.run(run())


def test_replay_claims_done_failed_producer_before_done_callback(monkeypatch):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release_producer = asyncio.Event()
        finish_entered = asyncio.Event()
        allow_finish = asyncio.Event()
        original_finish = manager.finish_run
        original_consume = coordinator._consume_producer_task
        deferred_callbacks: list[tuple[str, asyncio.Task[Any]]] = []
        loop_errors: list[dict[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )

        async def blocked_finish(*args, **kwargs):
            finish_entered.set()
            await allow_finish.wait()
            return await original_finish(*args, **kwargs)

        monkeypatch.setattr(manager, "finish_run", blocked_finish)

        def defer_done_callback(run_id: str, task: asyncio.Task[Any]) -> None:
            deferred_callbacks.append((run_id, task))

        monkeypatch.setattr(
            coordinator, "_consume_producer_task", defer_done_callback
        )

        async def bootstrap(_run):
            async def producer() -> None:
                await release_producer.wait()
                raise RuntimeError("producer callback-order failure")

            return asyncio.create_task(producer())

        started = await coordinator.start(_spec(), bootstrap)
        run_id = started.run.run_id

        # Queue producer completion before replay. The producer done callback is
        # then queued behind replay, reproducing the callback-order window.
        release_producer.set()
        replay_task = asyncio.create_task(
            coordinator.start(_spec(), _completed_producer)
        )
        replay = await asyncio.gather(replay_task, return_exceptions=True)
        assert isinstance(replay[0], RunStartSchedulingError)
        assert replay[0].run_id == run_id
        await finish_entered.wait()
        assert len(deferred_callbacks) == 1
        for callback_run_id, callback_task in deferred_callbacks:
            original_consume(callback_run_id, callback_task)

        allow_finish.set()
        await _wait_until(
            lambda: not coordinator._producer_cleanup_tasks
            and run_id not in coordinator._producer_terminalization_failures
        )
        terminal = await coordinator.start(_spec(), _completed_producer)
        assert terminal.run.status == RunStatus.FAILED
        await asyncio.sleep(0)
        assert loop_errors == []

    asyncio.run(run())


def test_done_success_replay_refreshes_stale_existing_snapshot(monkeypatch):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release_producer = asyncio.Event()
        snapshot_taken = asyncio.Event()
        allow_snapshot_return = asyncio.Event()

        async def bootstrap(run_record):
            async def producer() -> None:
                await release_producer.wait()
                # Keep this synchronous after release so replay runs before the
                # queued producer done callback but after canonical terminalization.
                manager._runs[run_record.run_id].status = RunStatus.COMPLETED

            return asyncio.create_task(producer())

        started = await coordinator.start(_spec(), bootstrap)
        run_id = started.run.run_id
        original_reserve = manager.reserve_or_get_run

        async def stale_existing_snapshot(**kwargs):
            record, created = await original_reserve(**kwargs)
            if not created:
                assert record.status == RunStatus.RUNNING
                snapshot_taken.set()
                await allow_snapshot_return.wait()
            return record, created

        monkeypatch.setattr(
            manager, "reserve_or_get_run", stale_existing_snapshot
        )
        replay_task = asyncio.create_task(
            coordinator.start(_spec(), _completed_producer)
        )
        await snapshot_taken.wait()

        release_producer.set()
        allow_snapshot_return.set()
        replay = await replay_task
        assert replay.created is False
        assert replay.run.run_id == run_id
        assert replay.run.status == RunStatus.COMPLETED
        await _wait_until(lambda: run_id not in coordinator._producer_tasks)

    asyncio.run(run())


def test_existing_snapshot_refreshes_after_success_callback_already_ran(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release_producer = asyncio.Event()
        snapshot_taken = asyncio.Event()
        allow_snapshot_return = asyncio.Event()

        async def bootstrap(run_record):
            async def producer() -> None:
                await release_producer.wait()
                manager._runs[run_record.run_id].status = RunStatus.COMPLETED

            return asyncio.create_task(producer())

        started = await coordinator.start(_spec(), bootstrap)
        run_id = started.run.run_id
        original_reserve = manager.reserve_or_get_run

        async def stale_existing_snapshot(**kwargs):
            record, created = await original_reserve(**kwargs)
            if not created:
                assert record.status == RunStatus.RUNNING
                snapshot_taken.set()
                await allow_snapshot_return.wait()
            return record, created

        monkeypatch.setattr(
            manager, "reserve_or_get_run", stale_existing_snapshot
        )
        replay_task = asyncio.create_task(
            coordinator.start(_spec(), _completed_producer)
        )
        await snapshot_taken.wait()

        release_producer.set()
        await _wait_until(lambda: run_id not in coordinator._producer_tasks)
        allow_snapshot_return.set()
        replay = await replay_task
        assert replay.run.status == RunStatus.COMPLETED

    asyncio.run(run())


def test_shared_producer_task_claims_are_scoped_to_exact_run_task_pair(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release_shared = asyncio.Event()
        original_consume = coordinator._consume_producer_task
        deferred_callbacks: list[tuple[str, asyncio.Task[Any]]] = []

        async def shared_producer() -> None:
            await release_shared.wait()
            raise RuntimeError("shared producer failed")

        shared_task = asyncio.create_task(shared_producer())

        async def bootstrap(_run):
            return shared_task

        def defer_done_callback(run_id: str, task: asyncio.Task[Any]) -> None:
            deferred_callbacks.append((run_id, task))

        monkeypatch.setattr(
            coordinator, "_consume_producer_task", defer_done_callback
        )
        spec_a = _spec(key="shared-a", request_id="shared-a")
        spec_b = _spec(key="shared-b", request_id="shared-b")
        started_a = await coordinator.start(spec_a, bootstrap)
        started_b = await coordinator.start(spec_b, bootstrap)

        release_shared.set()
        await asyncio.gather(shared_task, return_exceptions=True)
        await _wait_until(lambda: len(deferred_callbacks) == 2)

        # A replay claims (run_a, shared_task) before its deferred callback.
        replay_a = await asyncio.gather(
            coordinator.start(spec_a, _completed_producer),
            return_exceptions=True,
        )
        assert isinstance(replay_a[0], RunStartSchedulingError) or (
            replay_a[0].run.status == RunStatus.FAILED
        )

        callbacks_by_run = {
            run_id: task for run_id, task in deferred_callbacks
        }
        # Deliver B first. It must not consume A's exact claim.
        original_consume(
            started_b.run.run_id,
            callbacks_by_run[started_b.run.run_id],
        )
        original_consume(
            started_a.run.run_id,
            callbacks_by_run[started_a.run.run_id],
        )
        await _wait_until(
            lambda: not coordinator._producer_cleanup_tasks
            and not coordinator._producer_terminalization_failures
        )

        assert coordinator._producer_tasks == {}
        assert coordinator._producer_callback_claims == set()
        recovered_a = await manager.get_run_for_recovery(started_a.run.run_id)
        recovered_b = await manager.get_run_for_recovery(started_b.run.run_id)
        assert recovered_a is not None and recovered_a.status == RunStatus.FAILED
        assert recovered_b is not None and recovered_b.status == RunStatus.FAILED
        drain = await coordinator.close(timeout=1)
        assert drain.exhausted is False

    asyncio.run(run())


def test_missing_producer_terminalization_failure_is_pruned_on_later_start(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        release = asyncio.Event()
        original_finish = manager.finish_run
        original_recovery = manager.get_run_for_recovery

        async def fail_finish(*_args, **_kwargs):
            raise RuntimeError("finish failed before deletion")

        monkeypatch.setattr(manager, "finish_run", fail_finish)

        async def bootstrap(_run):
            async def producer() -> None:
                await release.wait()
                raise RuntimeError("producer failed before deletion")

            return asyncio.create_task(producer())

        started = await coordinator.start(_spec(), bootstrap)
        run_id = started.run.run_id
        release.set()
        await _wait_until(
            lambda: not coordinator._producer_cleanup_tasks
            and run_id in coordinator._producer_terminalization_failures
        )

        async def deleted_recovery(candidate_run_id: str):
            if candidate_run_id == run_id:
                return None
            return await original_recovery(candidate_run_id)

        monkeypatch.setattr(manager, "get_run_for_recovery", deleted_recovery)
        await coordinator.start(
            _spec(key="after-deletion", request_id="after-deletion"),
            _completed_producer,
        )
        assert run_id not in coordinator._producer_terminalization_failures

        monkeypatch.setattr(manager, "get_run_for_recovery", original_recovery)
        monkeypatch.setattr(manager, "finish_run", original_finish)
        await original_finish(run_id, RunStatus.INTERRUPTED, "test cleanup")

    asyncio.run(run())


def test_shutdown_terminalization_retry_preserves_interrupted_target(monkeypatch):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        producer_started = asyncio.Event()
        original_finish = manager.finish_run

        async def fail_finish(*_args, **_kwargs):
            raise RuntimeError("shutdown finish failed")

        monkeypatch.setattr(manager, "finish_run", fail_finish)

        async def bootstrap(_run):
            async def producer() -> None:
                producer_started.set()
                await asyncio.Event().wait()

            return asyncio.create_task(producer())

        started = await coordinator.start(_spec(), bootstrap)
        await producer_started.wait()
        run_id = started.run.run_id
        first = await coordinator.close(timeout=0.05)
        assert first.exhausted is True
        assert first.producer_run_ids == (run_id,)
        target = coordinator._producer_terminalization_failures[run_id]
        assert target.status == RunStatus.INTERRUPTED

        monkeypatch.setattr(manager, "finish_run", original_finish)
        second = await coordinator.close(timeout=1)
        assert second.exhausted is False
        recovered = await manager.get_run_for_recovery(run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.INTERRUPTED

    asyncio.run(run())


def test_close_cancels_registered_producer_and_drains_before_manager_close(caplog):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        producer_started = asyncio.Event()

        async def bootstrap(_run):
            async def produce() -> None:
                producer_started.set()
                await asyncio.Event().wait()

            return asyncio.create_task(produce())

        result = await coordinator.start(_spec(), bootstrap)
        await producer_started.wait()
        drain = await coordinator.close(timeout=1)
        assert drain.exhausted is False
        assert drain.request_task_ids == ()
        assert drain.producer_run_ids == ()
        assert drain.pending_run_ids == ()
        assert coordinator._bootstrap_barriers == {}
        assert coordinator._request_tasks == set()
        assert coordinator._producer_tasks == {}
        assert coordinator._producer_cleanup_tasks == set()
        recovered = await manager.get_run_for_recovery(result.run.run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.INTERRUPTED
        manager_drain = await manager.close()
        assert manager_drain.exhausted_run_ids == ()
        with caplog.at_level("ERROR"):
            with pytest.raises(RunStartReservationError, match="closing"):
                await coordinator.start(
                    _spec(key="after-close", request_id="after-close"),
                    _completed_producer,
                )
            with pytest.raises(RunStartReservationError, match="closing"):
                await coordinator.replay_existing(_spec().idempotency)
        assert "request_id=after-close" in caplog.text

    asyncio.run(run())


def test_close_cancels_blocked_owner_and_waiter_without_hanging():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        bootstrap_entered = asyncio.Event()

        async def bootstrap(_run):
            bootstrap_entered.set()
            await asyncio.Event().wait()
            return asyncio.create_task(asyncio.sleep(0))

        spec = _spec()
        owner = asyncio.create_task(coordinator.start(spec, bootstrap))
        await bootstrap_entered.wait()
        loser = asyncio.create_task(coordinator.start(spec, bootstrap))
        await asyncio.sleep(0)

        drain = await coordinator.close(timeout=1)
        assert drain.exhausted is False
        results = await asyncio.wait_for(
            asyncio.gather(owner, loser, return_exceptions=True), timeout=1
        )
        assert all(isinstance(item, asyncio.CancelledError) for item in results)
        assert coordinator._bootstrap_barriers == {}
        assert coordinator._request_tasks == set()
        assert coordinator._producer_tasks == {}
        assert coordinator._producer_cleanup_tasks == set()
        manager_drain = await manager.close()
        assert manager_drain.exhausted_run_ids == ()

    asyncio.run(run())


def test_close_timeout_reports_and_retains_outstanding_work():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        entered = asyncio.Event()
        allow_cancel = asyncio.Event()

        async def bootstrap(_run):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await allow_cancel.wait()
                raise
            raise AssertionError("unreachable")

        caller = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await entered.wait()
        drain = await coordinator.close(timeout=0.01)
        assert drain.exhausted is True
        assert drain.request_task_ids == ("request-1",)
        assert len(drain.pending_run_ids) == 1
        assert coordinator._request_tasks
        assert coordinator._bootstrap_barriers

        allow_cancel.set()
        result = await asyncio.wait_for(
            asyncio.gather(caller, return_exceptions=True), timeout=1
        )
        assert isinstance(result[0], asyncio.CancelledError)
        final = await coordinator.close(timeout=1)
        assert final.exhausted is False
        assert coordinator._bootstrap_barriers == {}

    asyncio.run(run())


def test_timed_out_close_recovery_is_strongly_held_and_clears_tombstone(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        original_interrupt = manager.interrupt_reserved_run
        bootstrap_entered = asyncio.Event()
        allow_bootstrap_cancel = asyncio.Event()
        recovery_entered = asyncio.Event()
        allow_recovery = asyncio.Event()

        async def delayed_interrupt(run_id: str, error: str):
            recovery_entered.set()
            await allow_recovery.wait()
            return await original_interrupt(run_id, error)

        monkeypatch.setattr(manager, "interrupt_reserved_run", delayed_interrupt)

        async def bootstrap(_run):
            bootstrap_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await allow_bootstrap_cancel.wait()
                raise
            raise AssertionError("unreachable")

        caller = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await bootstrap_entered.wait()
        close_task = asyncio.create_task(coordinator.close(timeout=0.01))
        await recovery_entered.wait()
        drain = await close_task
        assert drain.exhausted is True
        assert len(coordinator._close_recovery_tasks) == 1
        run_id = next(iter(coordinator._close_recovery_tasks))
        assert run_id in coordinator._bootstrap_failures

        allow_recovery.set()
        await _wait_until(lambda: not coordinator._close_recovery_tasks)
        assert run_id not in coordinator._bootstrap_failures

        allow_bootstrap_cancel.set()
        caller_result = await asyncio.wait_for(
            asyncio.gather(caller, return_exceptions=True), timeout=1
        )
        assert isinstance(caller_result[0], asyncio.CancelledError)
        final = await coordinator.close(timeout=1)
        assert final.exhausted is False
        assert coordinator._close_recovery_tasks == {}
        assert coordinator._bootstrap_failures == {}

    asyncio.run(run())


def test_failed_background_close_recovery_is_retained_then_retried(
    monkeypatch,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        record, created = await manager.reserve_or_get_run(
            conversation_id="conversation-1",
            kind=RunKind.CHAT,
            idempotency_key="recovery-retry",
            request_fingerprint="a" * 64,
        )
        assert created is True
        coordinator._bootstrap_failures[record.run_id] = RunStartSchedulingError(
            record.run_id
        )
        original_interrupt = manager.interrupt_reserved_run
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def fail_first(run_id: str, error: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await release_first.wait()
                raise RuntimeError("first close recovery failed")
            return await original_interrupt(run_id, error)

        monkeypatch.setattr(manager, "interrupt_reserved_run", fail_first)
        close_task = asyncio.create_task(coordinator.close(timeout=0.01))
        await first_entered.wait()
        first = await close_task
        assert first.exhausted is True
        assert first.pending_run_ids == (record.run_id,)
        assert coordinator._close_recovery_tasks[record.run_id]

        release_first.set()
        await _wait_until(lambda: not coordinator._close_recovery_tasks)
        assert record.run_id in coordinator._bootstrap_failures

        second = await coordinator.close(timeout=1)
        assert second.exhausted is False
        assert calls == 2
        assert coordinator._close_recovery_tasks == {}
        assert coordinator._bootstrap_failures == {}
        recovered = await manager.get_run_for_recovery(record.run_id)
        assert recovered is not None
        assert recovered.status == RunStatus.INTERRUPTED

    asyncio.run(run())


def test_close_reports_bootstrap_task_that_suppresses_detached_cancellation():
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        bootstrap_entered = asyncio.Event()
        detached_started = asyncio.Event()
        allow_detached_exit = asyncio.Event()

        async def bootstrap(_run):
            bootstrap_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

            async def detached_producer() -> None:
                detached_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await allow_detached_exit.wait()

            task = asyncio.create_task(detached_producer())
            await detached_started.wait()
            return task

        caller = asyncio.create_task(coordinator.start(_spec(), bootstrap))
        await bootstrap_entered.wait()
        close_task = asyncio.create_task(coordinator.close(timeout=0.05))
        await _wait_until(lambda: bool(coordinator._detached_producer_tasks))
        drain = await close_task
        assert drain.exhausted is True
        assert len(drain.producer_run_ids) == 1
        assert len(coordinator._detached_producer_tasks) == 1
        caller_result = await asyncio.wait_for(
            asyncio.gather(caller, return_exceptions=True), timeout=1
        )
        assert isinstance(caller_result[0], RunStartSchedulingError)

        allow_detached_exit.set()
        await _wait_until(lambda: not coordinator._detached_producer_tasks)
        final = await coordinator.close(timeout=1)
        assert final.exhausted is False

    asyncio.run(asyncio.wait_for(run(), timeout=3))
