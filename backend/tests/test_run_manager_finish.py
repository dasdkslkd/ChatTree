from __future__ import annotations

import asyncio

import pytest

from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.tasks import TaskLifecycleStatus, TaskOutcome


class FlakyFinishRepository:
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
