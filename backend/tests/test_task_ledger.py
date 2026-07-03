from __future__ import annotations

import asyncio

from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.tasks import TaskLedger, TaskOwnerType, TaskStatus


def run(coro):
    return asyncio.run(coro)


async def _create_list_and_open_status_case():
    ledger = TaskLedger()

    task = await ledger.create_task(
        conversation_id="conv-1",
        title="Inspect failing checks",
        detail="Run the focused backend tests",
        created_by_run_id="run-parent",
        metadata={"priority": "high"},
    )

    assert task.task_id.startswith("task_")
    assert task.status == TaskStatus.PENDING
    assert task.created_by_run_id == "run-parent"
    assert task.metadata == {"priority": "high"}

    all_tasks = await ledger.list_tasks("conv-1")
    open_tasks = await ledger.list_open_tasks("conv-1")

    assert [record.task_id for record in all_tasks] == [task.task_id]
    assert [record.task_id for record in open_tasks] == [task.task_id]


def test_create_list_and_open_status():
    run(_create_list_and_open_status_case())


async def _bind_run_moves_task_to_in_progress_case():
    ledger = TaskLedger()
    task = await ledger.create_task(conversation_id="conv-1", title="Review API shape")

    updated = await ledger.bind_run(
        conversation_id="conv-1",
        task_id=task.task_id,
        run_id="run-child",
        owner_type=TaskOwnerType.SUBAGENT,
    )

    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.owner_run_id == "run-child"
    assert updated.owner_type == TaskOwnerType.SUBAGENT
    assert (await ledger.find_by_owner_run("conv-1", "run-child")).task_id == task.task_id


def test_bind_run_moves_task_to_in_progress():
    run(_bind_run_moves_task_to_in_progress_case())


async def _completed_run_marks_task_completed_with_evidence_case():
    ledger = TaskLedger()
    run_manager = RunManager()
    task = await ledger.create_task(conversation_id="conv-1", title="Run verifier")
    run_record = await run_manager.create_run(
        conversation_id="conv-1",
        kind=RunKind.SUBAGENT,
        summary="Verifier",
    )
    await ledger.bind_run(
        conversation_id="conv-1",
        task_id=task.task_id,
        run_id=run_record.run_id,
        owner_type=TaskOwnerType.SUBAGENT,
    )

    finished = await run_manager.finish_run(run_record.run_id, RunStatus.COMPLETED)
    await ledger.handle_run_finished(finished.to_dict())

    updated = await ledger.get_task("conv-1", task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.COMPLETED
    assert updated.evidence_run_id == run_record.run_id
    assert updated.evidence_summary == "Run completed: Verifier"
    assert updated.finished_at is not None
    assert await ledger.list_open_tasks("conv-1") == []


def test_completed_run_marks_task_completed_with_evidence():
    run(_completed_run_marks_task_completed_with_evidence_case())


async def _failed_run_marks_task_blocked_and_keeps_open_case():
    ledger = TaskLedger()
    task = await ledger.create_task(conversation_id="conv-1", title="Run migration")
    await ledger.bind_run(
        conversation_id="conv-1",
        task_id=task.task_id,
        run_id="run-failed",
        owner_type=TaskOwnerType.WORKFLOW,
    )

    await ledger.handle_run_finished(
        {
            "conversation_id": "conv-1",
            "run_id": "run-failed",
            "status": "failed",
            "summary": "Migration workflow",
            "metadata": {"error": "dependency unavailable"},
        }
    )

    updated = await ledger.get_task("conv-1", task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.BLOCKED
    assert updated.evidence_run_id == "run-failed"
    assert updated.evidence_summary == "Run failed: dependency unavailable"
    assert [record.task_id for record in await ledger.list_open_tasks("conv-1")] == [task.task_id]


def test_failed_run_marks_task_blocked_and_keeps_open():
    run(_failed_run_marks_task_blocked_and_keeps_open_case())


async def _cancelled_run_marks_task_cancelled_and_removes_from_open_case():
    ledger = TaskLedger()
    task = await ledger.create_task(conversation_id="conv-1", title="Run cancelled branch")
    await ledger.bind_run(
        conversation_id="conv-1",
        task_id=task.task_id,
        run_id="run-cancelled",
        owner_type=TaskOwnerType.COMMAND,
    )

    await ledger.handle_run_finished(
        {
            "conversation_id": "conv-1",
            "run_id": "run-cancelled",
            "status": "cancelled",
            "summary": "Long command",
        }
    )

    updated = await ledger.get_task("conv-1", task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.CANCELLED
    assert updated.evidence_run_id == "run-cancelled"
    assert updated.evidence_summary == "Run cancelled: Long command"
    assert await ledger.list_open_tasks("conv-1") == []


def test_cancelled_run_marks_task_cancelled_and_removes_from_open():
    run(_cancelled_run_marks_task_cancelled_and_removes_from_open_case())


async def _finished_task_requires_evidence_case():
    ledger = TaskLedger()
    task = await ledger.create_task(conversation_id="conv-1", title="Needs evidence")

    for status in (TaskStatus.COMPLETED, TaskStatus.BLOCKED):
        try:
            await ledger.update_task(
                conversation_id="conv-1",
                task_id=task.task_id,
                status=status,
            )
        except ValueError as exc:
            assert "evidence" in str(exc)
        else:
            raise AssertionError(f"{status.value} task without evidence should fail")
        unchanged = await ledger.get_task("conv-1", task.task_id)
        assert unchanged is not None
        assert unchanged.status == TaskStatus.PENDING
        assert [record.task_id for record in await ledger.list_open_tasks("conv-1")] == [task.task_id]


def test_finished_task_requires_evidence():
    run(_finished_task_requires_evidence_case())


async def _snapshot_round_trip_preserves_status_and_evidence_case():
    ledger = TaskLedger()
    task = await ledger.create_task(conversation_id="conv-1", title="Persist me")
    await ledger.update_task(
        conversation_id="conv-1",
        task_id=task.task_id,
        status=TaskStatus.COMPLETED,
        evidence_run_id="run-123",
        evidence_summary="Verified by test",
    )
    snapshot = await ledger.snapshot("conv-1")

    restored = TaskLedger()
    await restored.load_snapshot("conv-1", snapshot)

    restored_task = await restored.get_task("conv-1", task.task_id)
    assert restored_task is not None
    assert restored_task.status == TaskStatus.COMPLETED
    assert restored_task.evidence_run_id == "run-123"
    assert restored_task.evidence_summary == "Verified by test"


def test_snapshot_round_trip_preserves_status_and_evidence():
    run(_snapshot_round_trip_preserves_status_and_evidence_case())


async def _installed_run_finish_listener_updates_bound_task_case():
    ledger = TaskLedger()
    run_manager = RunManager()
    assert ledger.install_run_finish_listener(run_manager) is True
    assert ledger.install_run_finish_listener(run_manager) is False
    task = await ledger.create_task(conversation_id="conv-1", title="Listener task")
    run_record = await run_manager.create_run(
        conversation_id="conv-1",
        kind=RunKind.COMMAND,
        summary="Listener command",
    )
    await ledger.bind_run(
        conversation_id="conv-1",
        task_id=task.task_id,
        run_id=run_record.run_id,
        owner_type=TaskOwnerType.COMMAND,
    )

    await run_manager.finish_run(run_record.run_id, RunStatus.COMPLETED)
    await asyncio.sleep(0)

    updated = await ledger.get_task("conv-1", task.task_id)
    assert updated is not None
    assert updated.status == TaskStatus.COMPLETED
    assert updated.evidence_run_id == run_record.run_id


def test_installed_run_finish_listener_updates_bound_task():
    run(_installed_run_finish_listener_updates_bound_task_case())
