from __future__ import annotations

import asyncio

import pytest

from backend.core.runs import (
    ProducerRegistry,
    ProducerRegistryClosingError,
    RunKind,
    RunManager,
    RunStatus,
)
from backend.core.runs.repository import MemoryRunRepository


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def test_registry_terminalizes_failed_and_cancelled_producers():
    async def scenario() -> None:
        manager = RunManager(repository=MemoryRunRepository())
        registry = ProducerRegistry(manager)
        failed = await manager.create_run(conversation_id="conv-1", kind=RunKind.CHAT)
        cancelled = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.SUBAGENT,
        )

        async def fail() -> None:
            raise RuntimeError("producer exploded")

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        failed_task = registry.create(failed.run_id, fail(), name="failed-producer")
        cancelled_task = registry.create(
            cancelled.run_id,
            wait_forever(),
            name="cancelled-producer",
        )
        await asyncio.gather(failed_task, return_exceptions=True)
        assert registry.cancel(cancelled.run_id) is True
        await asyncio.gather(cancelled_task, return_exceptions=True)
        await _wait_until(
            lambda: manager.get_run(failed.run_id)["status"] == "failed"
            and manager.get_run(cancelled.run_id)["status"] == "cancelled"
        )

        assert registry.task_for(failed.run_id) is None
        assert registry.task_for(cancelled.run_id) is None
        assert "producer exploded" in manager.get_run(failed.run_id)["metadata"]["error"]
        assert await registry.close() == ()
        await manager.close()

    asyncio.run(scenario())


def test_registry_does_not_overwrite_domain_terminal_status():
    async def scenario() -> None:
        manager = RunManager(repository=MemoryRunRepository())
        registry = ProducerRegistry(manager)
        run = await manager.create_run(conversation_id="conv-1", kind=RunKind.WORKFLOW)

        async def producer() -> None:
            await manager.finish_run(run.run_id, RunStatus.COMPLETED)

        task = registry.create(run.run_id, producer(), name="workflow-producer")
        await task
        await asyncio.sleep(0)

        assert manager.get_run(run.run_id)["status"] == "completed"
        assert registry.task_for(run.run_id) is None
        assert await registry.close() == ()
        await manager.close()

    asyncio.run(scenario())


def test_registry_close_drains_producer_and_background_tasks():
    async def scenario() -> None:
        manager = RunManager(repository=MemoryRunRepository())
        registry = ProducerRegistry(manager)
        run = await manager.create_run(conversation_id="conv-1", kind=RunKind.CHAT)
        producer_cancelled = asyncio.Event()
        background_cancelled = asyncio.Event()

        async def wait_for_cancel(event: asyncio.Event) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                event.set()

        registry.create(
            run.run_id,
            wait_for_cancel(producer_cancelled),
            name="message-producer",
        )
        registry.create_background(
            wait_for_cancel(background_cancelled),
            name="message-notification",
        )

        assert await registry.close(timeout=1) == ()
        assert producer_cancelled.is_set()
        assert background_cancelled.is_set()
        assert manager.get_run(run.run_id)["status"] == "cancelled"
        await manager.close()

    asyncio.run(scenario())


def test_begin_close_synchronously_cancels_and_rejects_new_work():
    async def scenario() -> None:
        manager = RunManager(repository=MemoryRunRepository())
        registry = ProducerRegistry(manager)
        run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
        )
        cancelled = asyncio.Event()

        async def pending() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = registry.create_background(pending(), name="pending-delivery")
        assert task is not None
        await asyncio.sleep(0)

        registry.begin_close()

        await asyncio.gather(task, return_exceptions=True)
        assert cancelled.is_set()

        async def late() -> None:
            raise AssertionError("late work must not start")

        assert registry.create_background(late(), name="late-background") is None
        with pytest.raises(ProducerRegistryClosingError):
            registry.create(run.run_id, late(), name="late-producer")

        assert await registry.close() == ()
        await manager.finish_run(run.run_id, RunStatus.INTERRUPTED)
        await manager.close()

    asyncio.run(scenario())


def test_registry_close_retries_terminalization_that_failed_before_close():
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
        run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
        )
        repository.fail_run_id = run.run_id

        async def fail() -> None:
            raise RuntimeError("producer exploded")

        producer = registry.create(run.run_id, fail(), name="failed-producer")
        await asyncio.gather(producer, return_exceptions=True)
        await _wait_until(
            lambda: run.run_id in registry._pending_terminalizations
            and run.run_id not in registry._terminalizers
        )

        assert await registry.close(timeout=1) == ()
        assert repository.get_run(run.run_id)["status"] == RunStatus.FAILED.value
        await manager.close()

    asyncio.run(scenario())


def test_registry_close_reports_persistent_terminalization_failure():
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
        run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
        )
        repository.fail_run_id = run.run_id

        async def fail() -> None:
            raise RuntimeError("producer exploded")

        producer = registry.create(run.run_id, fail(), name="failed-producer")
        await asyncio.gather(producer, return_exceptions=True)
        await _wait_until(
            lambda: run.run_id in registry._pending_terminalizations
            and run.run_id not in registry._terminalizers
        )

        assert await registry.close(timeout=0.2) == (run.run_id,)
        assert repository.get_run(run.run_id)["status"] == RunStatus.RUNNING.value
        repository.fail_run_id = None
        await manager.finish_run(run.run_id, RunStatus.INTERRUPTED)
        await manager.close()

    asyncio.run(scenario())


def test_explicit_terminalization_failure_remains_owned_until_close():
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
        run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.WORKFLOW,
        )
        repository.fail_run_id = run.run_id

        with pytest.raises(OSError, match="terminal persistence unavailable"):
            await registry.terminalize(
                run.run_id,
                RunStatus.INTERRUPTED,
                "producer scheduling failed",
            )

        assert await registry.close(timeout=0.2) == (run.run_id,)
        repository.fail_run_id = None
        await manager.finish_run(run.run_id, RunStatus.INTERRUPTED)
        await manager.close()

    asyncio.run(scenario())
