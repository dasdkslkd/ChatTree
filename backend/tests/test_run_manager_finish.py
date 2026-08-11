from __future__ import annotations

import asyncio

import pytest

from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.runs.repository import MemoryRunRepository
from backend.core.tasks import TaskLifecycleStatus, TaskOutcome


class FlakyFinishRepository:
    manages_task_bindings = False

    def __init__(self, run: dict) -> None:
        self.run = dict(run)
        self.calls = 0

    def finish_run(self, run_id: str, status: str, error=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("database write failed")
        self.run.update({
            "run_id": run_id,
            "status": status,
            "event_count": 2,
            "finished_at": 20.0,
            "updated_at": 20.0,
            "metadata": {},
        })
        return dict(self.run)

    def read_events(self, run_id: str, from_event: int):
        if self.calls < 2 or from_event > 1:
            return []
        return [{
            "event_index": 1,
            "payload": {
                "type": "run_finished",
                "run_id": run_id,
                "status": "completed",
            },
            "created_at": 20.0,
        }]


def test_finish_run_can_retry_after_repository_failure():
    async def scenario():
        manager = RunManager()
        run = await manager.create_run(conversation_id="conv-1", kind=RunKind.COMMAND)
        repository = FlakyFinishRepository(manager.get_run(run.run_id))
        manager.repository = repository

        with pytest.raises(RuntimeError, match="database write failed"):
            await manager.finish_run(run.run_id, RunStatus.COMPLETED)

        assert manager.get_run(run.run_id)["status"] == RunStatus.RUNNING.value
        finished = await manager.finish_run(run.run_id, RunStatus.COMPLETED)
        assert repository.calls == 2
        assert finished.status == RunStatus.COMPLETED

    asyncio.run(scenario())


def test_in_memory_finish_does_not_publish_terminal_before_task_outcome():
    class BlockingTaskService:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_run_finished(self, run):
            self.entered.set()
            await self.release.wait()
            return TaskOutcome(
                kind="run_finished",
                task_status=TaskLifecycleStatus.COMPLETED,
                step=1,
                step_status="completed",
                run_status="completed",
            )

    async def scenario():
        manager = RunManager()
        task_service = BlockingTaskService()
        manager.task_service = task_service
        run = await manager.create_run(conversation_id="conv-1", kind=RunKind.COMMAND)

        finishing = asyncio.create_task(manager.finish_run(run.run_id, RunStatus.COMPLETED))
        await task_service.entered.wait()

        in_flight = manager.get_run(run.run_id)
        assert in_flight["status"] == RunStatus.RUNNING.value
        assert "task_outcome" not in in_flight["metadata"]

        task_service.release.set()
        await finishing
        finished = manager.get_run(run.run_id)
        assert finished["status"] == RunStatus.COMPLETED.value
        assert finished["metadata"]["task_outcome"]["task_status"] == "completed"

    asyncio.run(scenario())


def test_create_run_cancellation_after_commit_interrupts_same_run():
    async def scenario():
        manager = RunManager()
        creation_committed = asyncio.Event()
        never_release = asyncio.Event()
        original_append_event = manager.append_event

        async def block_run_started(run_id, payload):
            if payload.get("type") == "run_started":
                creation_committed.set()
                await never_release.wait()
            return await original_append_event(run_id, payload)

        manager.append_event = block_run_started
        creating = asyncio.create_task(
            manager.create_run(
                conversation_id="conv-1",
                kind=RunKind.CHAT,
            )
        )
        await asyncio.wait_for(creation_committed.wait(), timeout=1)
        committed = manager.list_runs()
        assert len(committed) == 1

        creating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creating

        run = manager.get_run(committed[0]["run_id"])
        assert run["status"] == "interrupted"
        assert manager.read_events(run["run_id"], 0)[-1]["status"] == "interrupted"
        await manager.close()

    asyncio.run(scenario())


def test_create_run_uses_atomic_created_row_without_post_commit_lookup():
    class NoPostCommitLookupRepository(MemoryRunRepository):
        def __init__(self) -> None:
            super().__init__()
            self.get_run_calls = 0

        def get_run(self, run_id):
            self.get_run_calls += 1
            raise OSError(f"unexpected second lookup for {run_id}")

    async def scenario() -> None:
        repository = NoPostCommitLookupRepository()
        manager = RunManager(repository=repository)

        run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
        )

        assert run.status is RunStatus.RUNNING
        assert repository.get_run_calls == 0
        persisted = MemoryRunRepository.get_run(repository, run.run_id)
        assert persisted is not None
        assert persisted["status"] == "running"
        repository.get_run = lambda run_id: MemoryRunRepository.get_run(
            repository,
            run_id,
        )
        await manager.finish_run(run.run_id, RunStatus.INTERRUPTED)
        await manager.close()

    asyncio.run(scenario())


def test_create_run_hydration_failure_terminalizes_committed_row():
    class FailingHydrationManager(RunManager):
        def _hydrate_record(self, run):
            raise ValueError(f"cannot hydrate {run['run_id']}")

    async def scenario() -> None:
        repository = MemoryRunRepository()
        manager = FailingHydrationManager(repository=repository)

        with pytest.raises(ValueError, match="cannot hydrate"):
            await manager.create_run(
                conversation_id="conv-1",
                kind=RunKind.CHAT,
            )

        persisted = repository.list_runs()
        assert len(persisted) == 1
        assert persisted[0]["status"] == "interrupted"
        events = repository.read_events(persisted[0]["run_id"], 0)
        assert events[-1]["payload"]["status"] == "interrupted"
        await manager.close()

    asyncio.run(scenario())


def test_event_writer_failure_isolated_and_failed_run_can_terminalize(caplog):
    class FailOneRunOnceRepository(MemoryRunRepository):
        def __init__(self) -> None:
            super().__init__()
            self.fail_run_id: str | None = None
            self.failed = False

        def append_indexed_events(self, run_id, events):
            if run_id == self.fail_run_id and not self.failed:
                self.failed = True
                raise OSError("transient event write failure")
            return super().append_events(
                run_id,
                [dict(event["payload"]) for event in events],
            )

    async def scenario() -> None:
        repository = FailOneRunOnceRepository()
        manager = RunManager(repository=repository)
        failed_run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
        )
        healthy_run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.SUBAGENT,
        )
        repository.fail_run_id = failed_run.run_id

        await manager.append_event(
            failed_run.run_id,
            {"status": "content", "content": "not persisted"},
        )

        healthy = await manager.finish_run(
            healthy_run.run_id,
            RunStatus.COMPLETED,
        )
        recovered = await manager.finish_run(
            failed_run.run_id,
            RunStatus.COMPLETED,
        )

        assert healthy.status is RunStatus.COMPLETED
        assert recovered.status is RunStatus.FAILED
        assert repository.get_run(healthy_run.run_id)["status"] == "completed"
        persisted_failed = repository.get_run(failed_run.run_id)
        assert persisted_failed["status"] == "failed"
        assert persisted_failed["metadata"]["error"] == (
            "run event persistence failed"
        )
        assert [
            event["payload"]["status"]
            for event in repository.read_events(failed_run.run_id, 0)
            if event["payload"].get("type") == "run_finished"
        ] == ["failed"]
        assert sum(
            1
            for record in caplog.records
            if record.name == "backend.core.runs.run_manager"
            and failed_run.run_id in record.getMessage()
        ) == 1
        await manager.close()

    caplog.set_level("ERROR")
    asyncio.run(scenario())
