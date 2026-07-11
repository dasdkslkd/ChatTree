from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core.persistence import ChatRepository, SQLitePersistence, SQLiteRunRepository
from backend.core.persistence.content import INLINE_TEXT_LIMIT
from backend.core.persistence.task_repository import SQLiteTaskRepository
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.tasks import (
    ActiveTaskConflictError,
    ActiveTaskService,
    TaskContextDisabledError,
    TaskStepStatus,
)


def run(coro):
    return asyncio.run(coro)


def sqlite_services(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conversation_id = chat.create_conversation(title="Tasks")
    root_id = f"root-{uuid.uuid4().hex}"
    chat.create_node(conversation_id, None, node_id=root_id)
    task_repository = SQLiteTaskRepository(persistence)
    run_repository = SQLiteRunRepository(persistence, task_repository=task_repository)
    task_service = ActiveTaskService(repository=task_repository)
    run_manager = RunManager(repository=run_repository)
    run_manager.task_service = task_service
    task_service.run_manager = run_manager
    return conversation_id, root_id, task_service, run_manager


class CancelGenerationRepository:
    def __init__(self) -> None:
        self.expected_generation = None

    def get_active_task(self, conversation_id):
        return {
            "conversation_id": conversation_id,
            "generation_id": "generation-old",
            "revision": 0,
            "title": "Old task",
            "detail": "",
            "steps": [{
                "position": 1,
                "title": "Step",
                "detail": "",
                "status": "pending",
                "evidence_summary": "",
            }],
            "active_run_id": None,
            "active_step": None,
            "execution_state": "idle",
            "created_at": 1,
            "updated_at": 1,
        }

    def cancel_task(self, conversation_id, *, expected_generation, expected_revision):
        self.expected_generation = expected_generation
        return True


async def _conversation_has_one_active_task_and_public_shape_hides_internal_tokens_case():
    service = ActiveTaskService()
    created = await service.create_task(
        conversation_id="conv-1",
        title="Implement feature",
        detail="Keep one shared task",
        steps=[{"title": "Inspect"}, {"title": "Implement"}],
        tool_call_id="call-1",
    )

    current = await service.get_active_task("conv-1")
    replay = await service.create_task(
        conversation_id="conv-1",
        title="Implement feature",
        detail="Keep one shared task",
        steps=[{"title": "Inspect"}, {"title": "Implement"}],
        tool_call_id="call-1",
    )
    with pytest.raises(ActiveTaskConflictError):
        await service.create_task(
            conversation_id="conv-1",
            title="Rejected duplicate",
            steps=[{"title": "Other"}],
            tool_call_id="call-2",
        )

    assert current == created
    assert replay == created
    assert [step.position for step in created.steps] == [1, 2]
    assert created.public_dict() == {
        "title": "Implement feature",
        "detail": "Keep one shared task",
        "status": "pending",
        "execution_state": "idle",
        "active_run_id": None,
        "active_step": None,
        "steps": [
            {
                "position": 1,
                "title": "Inspect",
                "detail": "",
                "status": "pending",
                "evidence_summary": "",
            },
            {
                "position": 2,
                "title": "Implement",
                "detail": "",
                "status": "pending",
                "evidence_summary": "",
            },
        ],
    }


async def _manual_step_completion_deletes_task_only_after_last_step_case():
    service = ActiveTaskService()
    task = await service.create_task(
        conversation_id="conv-1",
        title="Two steps",
        steps=[{"title": "One"}, {"title": "Two"}],
    )

    first = await service.set_step_result(
        conversation_id="conv-1",
        step=1,
        status=TaskStepStatus.COMPLETED,
        evidence_summary="One done",
        expected_generation=task.generation_id,
    )
    final = await service.set_step_result(
        conversation_id="conv-1",
        step=2,
        status=TaskStepStatus.COMPLETED,
        evidence_summary="Two done",
        expected_generation=task.generation_id,
    )

    assert first.completed is False
    assert first.task is not None
    assert first.task.steps[0].status == TaskStepStatus.COMPLETED
    assert final.completed is True
    assert final.task is None
    assert [step.status for step in final.task_snapshot.steps] == [
        TaskStepStatus.COMPLETED,
        TaskStepStatus.COMPLETED,
    ]
    assert await service.get_active_task("conv-1") is None


async def _cancel_deletes_active_task_and_supersedes_binding_case(tmp_path):
    conversation_id, root_id, service, run_manager = sqlite_services(tmp_path)
    task = await service.create_task(
        conversation_id=conversation_id,
        title="Cancelable",
        steps=[{"title": "Wait"}],
    )
    binding = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    bound_run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.COMMAND,
        anchor_node_id=root_id,
        summary="long command",
        task_binding=binding,
    )

    cancelled = await service.cancel_task(
        conversation_id=conversation_id,
        reason="No longer needed",
        expected_generation=task.generation_id,
    )
    assert run_manager.get_run(bound_run.run_id)["status"] == RunStatus.STOPPING.value
    replacement = await service.create_task(
        conversation_id=conversation_id,
        title="Replacement",
        steps=[{"title": "Keep replacement"}],
    )
    await run_manager.finish_run(bound_run.run_id, RunStatus.COMPLETED)

    assert cancelled is True
    current = await service.get_active_task(conversation_id)
    assert current is not None
    assert current.generation_id == replacement.generation_id
    assert current.steps[0].status == TaskStepStatus.PENDING


async def _detached_context_cannot_bind_a_step_case():
    service = ActiveTaskService()
    task = await service.create_task(
        conversation_id="conv-1",
        title="Attached only",
        steps=[{"title": "Execute"}],
    )

    with pytest.raises(TaskContextDisabledError):
        await service.prepare_run_binding(
            conversation_id="conv-1",
            step=1,
            context_mode="detached",
            expected_generation=task.generation_id,
            expected_revision=task.revision,
        )


async def _in_memory_task_snapshot_reflects_stopping_run_case():
    run_manager = RunManager()
    service = ActiveTaskService(run_manager=run_manager)
    run_manager.task_service = service
    task = await service.create_task(
        conversation_id="conv-1",
        title="Stopping",
        steps=[{"title": "Wait"}],
    )
    binding = await service.prepare_run_binding(
        conversation_id="conv-1",
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    child = await run_manager.create_run(
        conversation_id="conv-1",
        kind=RunKind.COMMAND,
        task_binding=binding,
    )

    await run_manager.request_stop(child.run_id)

    current = await service.get_active_task("conv-1")
    snapshot = service.get_active_task_snapshot("conv-1")
    assert current is not None and current.execution_state == "stopping"
    assert snapshot is not None and snapshot.execution_state == "stopping"


async def _stale_generation_cannot_mutate_replacement_task_case():
    service = ActiveTaskService()
    first = await service.create_task(
        conversation_id="conv-1",
        title="First",
        steps=[{"title": "Finish"}],
    )
    await service.cancel_task(
        conversation_id="conv-1",
        reason="Replace it",
        expected_generation=first.generation_id,
    )
    second = await service.create_task(
        conversation_id="conv-1",
        title="Second",
        steps=[{"title": "Keep"}],
    )

    with pytest.raises(ActiveTaskConflictError):
        await service.set_step_result(
            conversation_id="conv-1",
            step=1,
            status=TaskStepStatus.COMPLETED,
            evidence_summary="stale",
            expected_generation=first.generation_id,
        )

    assert await service.get_active_task("conv-1") == second


async def _cancel_uses_the_generation_it_read_case():
    repository = CancelGenerationRepository()
    service = ActiveTaskService(repository=repository)

    await service.cancel_task(conversation_id="conv-1", reason="Cancel safely")

    assert repository.expected_generation == "generation-old"


async def _run_binding_rejects_unseen_or_stale_task_versions_case():
    service = ActiveTaskService()
    task = await service.create_task(
        conversation_id="conv-1",
        title="Versioned",
        steps=[{"title": "Run"}],
    )

    with pytest.raises(ActiveTaskConflictError):
        await service.prepare_run_binding(
            conversation_id="conv-1",
            step=1,
            context_mode="attached",
            expected_generation=None,
            expected_revision=None,
        )

    updated = await service.set_step_result(
        conversation_id="conv-1",
        step=1,
        status=TaskStepStatus.BLOCKED,
        evidence_summary="Needs retry",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    assert updated.task is not None
    with pytest.raises(ActiveTaskConflictError):
        await service.prepare_run_binding(
            conversation_id="conv-1",
            step=1,
            context_mode="attached",
            expected_generation=task.generation_id,
            expected_revision=task.revision,
        )


async def _bound_run_completion_is_atomic_and_final_step_deletes_task_case(tmp_path):
    conversation_id, root_id, service, run_manager = sqlite_services(tmp_path)
    task = await service.create_task(
        conversation_id=conversation_id,
        title="Bound task",
        steps=[{"title": "First"}, {"title": "Second"}],
    )
    first_binding = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    first_run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.SUBAGENT,
        anchor_node_id=root_id,
        summary="first",
        task_binding=first_binding,
    )

    running = await service.get_active_task(conversation_id)
    assert running is not None
    assert running.execution_state == "running"
    assert running.active_run_id == first_run.run_id
    assert running.active_step == 1

    await run_manager.finish_run(first_run.run_id, RunStatus.COMPLETED)
    first_record = run_manager.get_run(first_run.run_id)
    assert first_record["metadata"]["task_outcome"] == {
        "kind": "run_finished",
        "task_status": "active",
        "step": 1,
        "step_status": "completed",
        "run_status": "completed",
        "task_snapshot": {
            "title": "Bound task",
            "steps": [
                {
                    "position": 1,
                    "title": "First",
                    "status": "completed",
                },
                {
                    "position": 2,
                    "title": "Second",
                    "status": "pending",
                },
            ],
        },
    }
    after_first = await service.get_active_task(conversation_id)
    assert after_first is not None
    assert after_first.steps[0].status == TaskStepStatus.COMPLETED
    assert after_first.execution_state == "idle"

    second_binding = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=2,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=after_first.revision,
    )
    second_run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.WORKFLOW,
        anchor_node_id=root_id,
        summary="second",
        task_binding=second_binding,
    )
    await run_manager.finish_run(second_run.run_id, RunStatus.COMPLETED)

    assert await service.get_active_task(conversation_id) is None
    second_record = run_manager.get_run(second_run.run_id)
    assert second_record["metadata"]["task_outcome"] == {
        "kind": "run_finished",
        "task_status": "completed",
        "step": 2,
        "step_status": "completed",
        "run_status": "completed",
        "task_snapshot": {
            "title": "Bound task",
            "steps": [
                {
                    "position": 1,
                    "title": "First",
                    "status": "completed",
                },
                {
                    "position": 2,
                    "title": "Second",
                    "status": "completed",
                },
            ],
        },
    }


async def _cancelled_run_releases_binding_without_changing_step_case(tmp_path):
    conversation_id, root_id, service, run_manager = sqlite_services(tmp_path)
    task = await service.create_task(
        conversation_id=conversation_id,
        title="Retryable",
        steps=[{"title": "Long step"}],
    )
    binding = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    child = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.COMMAND,
        anchor_node_id=root_id,
        summary="cancel me",
        task_binding=binding,
    )
    await run_manager.request_stop(child.run_id)
    stopping = await service.get_active_task(conversation_id)
    assert stopping is not None
    assert stopping.execution_state == "stopping"
    await run_manager.finish_run(child.run_id, RunStatus.CANCELLED)

    current = await service.get_active_task(conversation_id)
    assert current is not None
    assert current.steps[0].status == TaskStepStatus.PENDING
    assert current.execution_state == "idle"
    record = run_manager.get_run(child.run_id)
    assert record["metadata"]["task_outcome"]["step_status"] == "released"
    assert record["metadata"]["task_outcome"]["task_status"] == "active"


async def _failed_run_blocks_step_and_allows_retry_case(tmp_path):
    conversation_id, root_id, service, run_manager = sqlite_services(tmp_path)
    task = await service.create_task(
        conversation_id=conversation_id,
        title="Retry failure",
        steps=[{"title": "Build"}],
    )
    first_binding = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    failed = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.COMMAND,
        anchor_node_id=root_id,
        summary="build",
        task_binding=first_binding,
    )
    await run_manager.finish_run(failed.run_id, RunStatus.FAILED, "compile failed")

    blocked = await service.get_active_task(conversation_id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.steps[0].status == TaskStepStatus.BLOCKED
    record = run_manager.get_run(failed.run_id)
    assert record["metadata"]["task_outcome"]["step_status"] == "blocked"
    assert record["metadata"]["task_outcome"]["task_status"] == "active"

    retry = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=blocked.revision,
    )
    assert retry["step_position"] == 1


async def _startup_interruption_persists_released_task_outcome_case(tmp_path):
    conversation_id, root_id, service, run_manager = sqlite_services(tmp_path)
    task = await service.create_task(
        conversation_id=conversation_id,
        title="Restartable",
        steps=[{"title": "Resume later"}],
    )
    binding = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    child = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.COMMAND,
        anchor_node_id=root_id,
        summary="interrupted command",
        task_binding=binding,
    )

    interrupted = run_manager.repository.mark_unfinished_as_interrupted()

    assert child.run_id in interrupted
    record = run_manager.repository.get_run(child.run_id)
    assert record["metadata"]["task_outcome"] == {
        "kind": "run_finished",
        "task_status": "active",
        "step": 1,
        "step_status": "released",
        "run_status": "interrupted",
        "task_snapshot": {
            "title": "Restartable",
            "steps": [{
                "position": 1,
                "title": "Resume later",
                "status": "pending",
            }],
        },
    }
    current = await service.get_active_task(conversation_id)
    assert current is not None
    assert current.steps[0].status == TaskStepStatus.PENDING
    assert current.execution_state == "idle"


async def _long_task_content_round_trips_without_locking_case(tmp_path):
    conversation_id, root_id, service, run_manager = sqlite_services(tmp_path)
    long_detail = "d" * (INLINE_TEXT_LIMIT + 1)
    task = await service.create_task(
        conversation_id=conversation_id,
        title="Long content",
        detail=long_detail,
        steps=[{"title": "Long step", "detail": long_detail}],
    )

    assert task.detail == long_detail
    assert task.steps[0].detail == long_detail

    binding = await service.prepare_run_binding(
        conversation_id=conversation_id,
        step=1,
        context_mode="attached",
        expected_generation=task.generation_id,
        expected_revision=task.revision,
    )
    child = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.COMMAND,
        anchor_node_id=root_id,
        summary="complete long task",
        task_binding=binding,
    )
    await run_manager.finish_run(child.run_id, RunStatus.COMPLETED)

    outcome = run_manager.get_run(child.run_id)["metadata"]["task_outcome"]
    assert outcome["task_snapshot"] == {
        "title": "Long content",
        "steps": [{
            "position": 1,
            "title": "Long step",
            "status": "completed",
        }],
    }


def test_active_task_service_contracts():
    run(_conversation_has_one_active_task_and_public_shape_hides_internal_tokens_case())
    run(_manual_step_completion_deletes_task_only_after_last_step_case())
    run(_detached_context_cannot_bind_a_step_case())
    run(_in_memory_task_snapshot_reflects_stopping_run_case())
    run(_stale_generation_cannot_mutate_replacement_task_case())
    run(_cancel_uses_the_generation_it_read_case())
    run(_run_binding_rejects_unseen_or_stale_task_versions_case())


def test_active_task_sqlite_lifecycle(tmp_path):
    run(_cancel_deletes_active_task_and_supersedes_binding_case(tmp_path / "cancel"))
    run(_bound_run_completion_is_atomic_and_final_step_deletes_task_case(tmp_path / "complete"))
    run(_cancelled_run_releases_binding_without_changing_step_case(tmp_path / "stop"))
    run(_failed_run_blocks_step_and_allows_retry_case(tmp_path / "failed"))
    run(_startup_interruption_persists_released_task_outcome_case(tmp_path / "startup"))
    run(_long_task_content_round_trips_without_locking_case(tmp_path / "long-content"))
