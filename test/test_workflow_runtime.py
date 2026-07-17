import asyncio
import os
import subprocess
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, ".")

from backend.api.dependencies import (
    get_chat_manager,
    get_run_manager,
    get_subagent_executor,
    get_workflow_manager,
)
from backend.api.routes import runs as runs_route
from backend.core.agents import AgentMailbox, AgentRuntime
from backend.core.agents.types import AgentSource
from backend.core.runs import (
    RunIdempotency,
    RunManager,
    RunStartCoordinator,
    RunStartValidationError,
)
from backend.core.runs.types import RunKind, RunStatus
from backend.core.workflows import WorkflowManager, normalize_workflow_budget
from backend.core.workflows.js_runner import WorkflowJsRunner, WorkflowScriptError
from backend.core.workflows.runtime_bridge import WorkflowRuntimeBridge


VALID_WORKFLOW_SCRIPT = (
    "export default async function workflow(ctx) { return 'done'; }"
)


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


def process_exists(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_workflow_js_runner_executes_host_calls():
    class Bridge:
        async def handle_call(self, method, params):
            if method == "log":
                return {"ok": True}
            if method == "agent":
                return {"content": "agent-data", "status": "completed"}
            raise RuntimeError(method)

    async def run():
        result = await WorkflowJsRunner().run(
            script="""
export default async function workflow(ctx) {
  await ctx.log('hello');
  const data = await ctx.agent('inspect', { agentType: 'workflow-worker' });
  return data.content;
}
""",
            args={},
            budget={"max_host_calls": 5, "max_seconds": 10},
            bridge=Bridge(),
        )
        assert result == "agent-data"

    asyncio.run(run())


def test_workflow_js_runner_rejects_legacy_workflow_object():
    class Bridge:
        async def handle_call(self, method, params):
            return {"ok": True}

    async def run():
        with pytest.raises(WorkflowScriptError):
            await WorkflowJsRunner().run(
                script="const workflow = { async run(ctx) { return 'ok'; } }; return workflow;",
                args={},
                budget={"max_host_calls": 5, "max_seconds": 10},
                bridge=Bridge(),
            )

    asyncio.run(run())


def test_normalize_workflow_budget_defaults_are_canonical_and_input_is_unchanged():
    explicit = {
        "max_seconds": 600,
        "max_host_calls": 200,
        "max_parallel": 8,
    }
    supplied = {"max_seconds": 12, "custom": {"mode": "strict"}}
    original = {"max_seconds": 12, "custom": {"mode": "strict"}}

    defaults = normalize_workflow_budget(None)

    assert defaults == explicit
    assert normalize_workflow_budget({}) == explicit
    assert normalize_workflow_budget(explicit) == explicit
    assert normalize_workflow_budget(defaults) == explicit
    assert normalize_workflow_budget(supplied) == {
        "max_seconds": 12,
        "max_host_calls": 200,
        "max_parallel": 8,
        "custom": {"mode": "strict"},
    }
    assert supplied == original


@pytest.mark.parametrize("field", ["max_seconds", "max_host_calls", "max_parallel"])
@pytest.mark.parametrize("value", ["5", True, 1.5, 0, -1, 2_147_483_648])
def test_normalize_workflow_budget_rejects_invalid_control_values(field, value):
    with pytest.raises(RunStartValidationError) as raised:
        normalize_workflow_budget({field: value})

    assert str(raised.value) == (
        f"budget.{field} must be an integer between 1 and 2147483647"
    )


def test_workflow_idempotent_start_runs_winner_side_effects_once(monkeypatch):
    class DummySubagent:
        pass

    class BlockingRunner:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.parent_node_ids = []

        def validate_script(self, script):
            assert script == VALID_WORKFLOW_SCRIPT

        async def run(self, **kwargs):
            self.calls += 1
            self.parent_node_ids.append(kwargs["bridge"].parent_node_id)
            self.started.set()
            await self.release.wait()
            return "done"

    class TaskService:
        def __init__(self):
            self.calls = []

        async def bind_in_memory_run(self, run_id, binding):
            self.calls.append((run_id, dict(binding)))

        async def handle_run_finished(self, _run):
            return None

    async def run():
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        run_manager = RunManager()
        task_service = TaskService()
        run_manager.task_service = task_service
        coordinator = RunStartCoordinator(run_manager)
        runner = BlockingRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
            run_start_coordinator=coordinator,
        )
        notification_started = asyncio.Event()
        notification_release = asyncio.Event()
        notification_calls = 0
        anchor_calls = 0

        async def notification(_run_id):
            nonlocal notification_calls
            notification_calls += 1
            notification_started.set()
            await notification_release.wait()
            raise RuntimeError("notification failed")

        async def anchor_factory(_run):
            nonlocal anchor_calls
            anchor_calls += 1
            return "node-winner"

        monkeypatch.setattr(workflow, "_register_task_notification", notification)
        idempotency = RunIdempotency("op_workflow", "b" * 64)
        task_binding = {
            "task_generation_id": "generation-1",
            "step_position": 2,
        }
        first, second = await asyncio.wait_for(
            asyncio.gather(
                workflow.start_idempotent(
                    conversation_id="conv-workflow",
                    script=VALID_WORKFLOW_SCRIPT,
                    parent_node_id="node-original",
                    task_binding=task_binding,
                    idempotency=idempotency,
                    request_id="request-workflow-1",
                    winner_anchor_factory=anchor_factory,
                ),
                workflow.start_idempotent(
                    conversation_id="conv-workflow",
                    script=VALID_WORKFLOW_SCRIPT,
                    parent_node_id="node-original",
                    task_binding=task_binding,
                    idempotency=idempotency,
                    request_id="request-workflow-2",
                    winner_anchor_factory=anchor_factory,
                ),
            ),
            timeout=1,
        )
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        await asyncio.wait_for(notification_started.wait(), timeout=1)

        run_id = first.run.run_id
        assert second.run.run_id == run_id
        assert {first.created, second.created} == {True, False}
        assert runner.calls == 1
        assert runner.parent_node_ids == ["node-winner"]
        assert anchor_calls == 1
        assert notification_calls == 1
        assert list(workflow._tasks) == [run_id]
        assert list(coordinator._producer_tasks) == [run_id]
        assert run_manager.get_run(run_id)["anchor_node_id"] == "node-winner"
        assert len(task_service.calls) == 1
        assert task_service.calls[0] == (run_id, task_binding)
        assert [
            event.get("type")
            for event in run_manager.read_events(run_id)
            if event.get("type") == "run_anchor_bound"
        ] == ["run_anchor_bound"]

        notification_release.set()
        await _wait_until(lambda: not workflow._notification_tasks)
        await asyncio.sleep(0)
        assert loop_errors == []

        runner.release.set()
        await _wait_until(
            lambda: run_manager.get_run(run_id)["status"] == RunStatus.COMPLETED.value
        )
        await _wait_until(lambda: run_id not in coordinator._producer_tasks)
        assert (await coordinator.close()).exhausted is False
        await run_manager.close()

    asyncio.run(run())


def test_workflow_internal_start_remains_non_idempotent():
    class DummySubagent:
        pass

    class BlockingRunner:
        def __init__(self):
            self.calls = 0
            self.all_started = asyncio.Event()
            self.release = asyncio.Event()

        def validate_script(self, script):
            assert script == VALID_WORKFLOW_SCRIPT

        async def run(self, **_kwargs):
            self.calls += 1
            if self.calls == 2:
                self.all_started.set()
            await self.release.wait()
            return "done"

    async def run():
        run_manager = RunManager()
        runner = BlockingRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
        )

        first = await workflow.start(
            conversation_id="conv-internal",
            script=VALID_WORKFLOW_SCRIPT,
        )
        second = await workflow.start(
            conversation_id="conv-internal",
            script=VALID_WORKFLOW_SCRIPT,
        )
        await asyncio.wait_for(runner.all_started.wait(), timeout=1)

        assert first["run_id"] != second["run_id"]
        assert runner.calls == 2
        tasks = list(workflow._tasks.values())
        runner.release.set()
        await asyncio.gather(*tasks)
        await run_manager.close()

    asyncio.run(run())


def test_workflow_validation_failure_precedes_idempotent_reservation():
    class DummySubagent:
        pass

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            run_start_coordinator=RunStartCoordinator(run_manager),
        )

        with pytest.raises(RunStartValidationError):
            await workflow.start_idempotent(
                conversation_id="conv-invalid",
                script="return 1",
                idempotency=RunIdempotency("op-invalid", "c" * 64),
                request_id="request-invalid",
            )

        assert run_manager.list_runs("conv-invalid") == []

    asyncio.run(run())


def test_workflow_replay_skips_changed_validator_and_new_key_still_validates():
    class DummySubagent:
        pass

    class MutableRunner:
        def __init__(self):
            self.reject = False
            self.validation_calls = 0
            self.run_calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def validate_script(self, _script):
            self.validation_calls += 1
            if self.reject:
                raise WorkflowScriptError("validator changed")

        async def run(self, **_kwargs):
            self.run_calls += 1
            self.started.set()
            await self.release.wait()
            return "done"

    async def run():
        run_manager = RunManager()
        runner = MutableRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
            run_start_coordinator=RunStartCoordinator(run_manager),
        )
        idempotency = RunIdempotency("op-workflow-replay", "6" * 64)
        first = await workflow.start_idempotent(
            conversation_id="conv-workflow-replay",
            script=VALID_WORKFLOW_SCRIPT,
            idempotency=idempotency,
            request_id="request-workflow-first",
        )
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        run_id = first.run.run_id
        events_before = list(run_manager.read_events(run_id))
        validation_calls_before = runner.validation_calls
        runner.reject = True

        replay = await workflow.start_idempotent(
            conversation_id="conv-workflow-replay",
            script=VALID_WORKFLOW_SCRIPT,
            idempotency=idempotency,
            request_id="request-workflow-replay",
        )

        assert replay.created is False
        assert replay.run.run_id == run_id
        assert runner.validation_calls == validation_calls_before
        assert runner.run_calls == 1
        assert list(run_manager.read_events(run_id)) == events_before

        with pytest.raises(RunStartValidationError, match="validator changed"):
            await workflow.start_idempotent(
                conversation_id="conv-workflow-replay",
                script=VALID_WORKFLOW_SCRIPT,
                idempotency=RunIdempotency("op-workflow-new", "7" * 64),
                request_id="request-workflow-new",
            )
        assert [item["run_id"] for item in run_manager.list_runs("conv-workflow-replay")] == [
            run_id
        ]
        assert list(run_manager.read_events(run_id)) == events_before

        runner.release.set()
        await asyncio.gather(*workflow._tasks.values())
        await run_manager.close()

    asyncio.run(run())


def test_workflow_coordinator_shutdown_interrupts_owned_producer():
    class DummySubagent:
        pass

    class BlockingRunner:
        def __init__(self):
            self.started = asyncio.Event()

        def validate_script(self, script):
            assert script == VALID_WORKFLOW_SCRIPT

        async def run(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    async def run():
        run_manager = RunManager()
        coordinator = RunStartCoordinator(run_manager)
        runner = BlockingRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
            run_start_coordinator=coordinator,
        )
        started = await workflow.start_idempotent(
            conversation_id="conv-shutdown",
            script=VALID_WORKFLOW_SCRIPT,
            idempotency=RunIdempotency("op-workflow-shutdown", "d" * 64),
            request_id="request-workflow-shutdown",
        )
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        drained = await coordinator.close(timeout=1)

        run_id = started.run.run_id
        state = run_manager.get_run(run_id)
        assert drained.exhausted is False
        assert state["status"] == RunStatus.INTERRUPTED.value
        finished = [
            event
            for event in run_manager.read_events(run_id)
            if event.get("type") == "run_finished"
        ]
        assert len(finished) == 1
        assert finished[0]["status"] == RunStatus.INTERRUPTED.value
        assert not [
            event
            for event in run_manager.read_events(run_id)
            if event.get("event_type") == "workflow_cancelled"
        ]

    asyncio.run(run())


def test_workflow_user_stop_keeps_owned_producer_cancelled():
    class DummySubagent:
        pass

    class BlockingRunner:
        def __init__(self):
            self.started = asyncio.Event()

        def validate_script(self, script):
            assert script == VALID_WORKFLOW_SCRIPT

        async def run(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    async def run():
        run_manager = RunManager()
        coordinator = RunStartCoordinator(run_manager)
        runner = BlockingRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
            run_start_coordinator=coordinator,
        )
        started = await workflow.start_idempotent(
            conversation_id="conv-stop-owned",
            script=VALID_WORKFLOW_SCRIPT,
            idempotency=RunIdempotency("op-workflow-stop", "e" * 64),
            request_id="request-workflow-stop",
        )
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        assert await workflow.stop(started.run.run_id) is True
        await _wait_until(
            lambda: run_manager.get_run(started.run.run_id)["status"]
            == RunStatus.CANCELLED.value
        )

        assert (await coordinator.close(timeout=1)).exhausted is False
        assert (
            run_manager.get_run(started.run.run_id)["status"]
            == RunStatus.CANCELLED.value
        )

    asyncio.run(run())


def test_workflow_close_serializes_with_internal_start_and_rejects_new_work(monkeypatch):
    class DummySubagent:
        pass

    class BlockingRunner:
        def validate_script(self, _script):
            return None

        async def run(self, **_kwargs):
            await asyncio.Event().wait()

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=BlockingRunner(),
        )
        create_entered = asyncio.Event()
        release_create = asyncio.Event()
        real_create_run = run_manager.create_run

        async def blocking_create_run(**kwargs):
            create_entered.set()
            await release_create.wait()
            return await real_create_run(**kwargs)

        monkeypatch.setattr(run_manager, "create_run", blocking_create_run)
        start_task = asyncio.create_task(
            workflow.start(
                conversation_id="conv-close-race",
                script=VALID_WORKFLOW_SCRIPT,
            )
        )
        await asyncio.wait_for(create_entered.wait(), timeout=1)

        close_task = asyncio.create_task(workflow.close(timeout=1))
        await asyncio.sleep(0)
        assert not close_task.done()

        release_create.set()
        started = await asyncio.wait_for(start_task, timeout=1)
        assert await asyncio.wait_for(close_task, timeout=1) == ()
        run_id = started["run_id"]
        assert run_manager.get_run(run_id)["status"] == RunStatus.CANCELLED.value
        assert workflow._tasks == {}

        with pytest.raises(RuntimeError, match="workflow manager is closing"):
            await workflow.start(
                conversation_id="conv-close-rejected",
                script=VALID_WORKFLOW_SCRIPT,
            )

        unscheduled = await real_create_run(
            conversation_id="conv-schedule-rejected",
            kind="workflow",
        )
        with pytest.raises(RuntimeError, match="workflow manager is closing"):
            await workflow.schedule_existing(
                run=unscheduled,
                conversation_id="conv-schedule-rejected",
                script=VALID_WORKFLOW_SCRIPT,
                args={},
                parent_node_id=None,
                created_by_run_id=None,
                cancellation_parent_run_id=None,
                budget={"max_seconds": 600, "max_steps": 100, "max_parallel": 8},
            )
        await run_manager.finish_run(unscheduled.run_id, RunStatus.CANCELLED)

    asyncio.run(run())


def test_workflow_close_deadline_covers_lifecycle_lock(monkeypatch):
    class DummySubagent:
        pass

    class Runner:
        def validate_script(self, _script):
            return None

        async def run(self, **_kwargs):
            await asyncio.Event().wait()

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=Runner(),
        )
        create_entered = asyncio.Event()
        release_create = asyncio.Event()
        real_create_run = run_manager.create_run

        async def blocking_create_run(**kwargs):
            create_entered.set()
            await release_create.wait()
            return await real_create_run(**kwargs)

        monkeypatch.setattr(run_manager, "create_run", blocking_create_run)
        start_task = asyncio.create_task(
            workflow.start(
                conversation_id="conv-close-lock-deadline",
                script=VALID_WORKFLOW_SCRIPT,
            )
        )
        await asyncio.wait_for(create_entered.wait(), timeout=1)
        try:
            assert await asyncio.wait_for(workflow.close(timeout=0.01), timeout=0.2) == (
                "workflow-lifecycle-lock",
            )
        finally:
            release_create.set()

        started = await asyncio.wait_for(start_task, timeout=1)
        with pytest.raises(RuntimeError, match="workflow manager is closing"):
            await workflow.start(
                conversation_id="conv-close-lock-rejected",
                script=VALID_WORKFLOW_SCRIPT,
            )

        assert await workflow.close(timeout=1) == ()
        assert (
            run_manager.get_run(started["run_id"])["status"]
            == RunStatus.CANCELLED.value
        )

    asyncio.run(run())


def test_workflow_close_terminalizes_producer_done_before_snapshot_callback(monkeypatch):
    class DummySubagent:
        pass

    class Runner:
        def validate_script(self, _script):
            return None

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=Runner(),
        )
        record = await run_manager.create_run(
            conversation_id="conv-producer-done-before-close",
            kind=RunKind.WORKFLOW,
        )

        async def failed_producer():
            raise RuntimeError("producer failed before close snapshot")

        producer_task = asyncio.create_task(
            failed_producer(),
            name=f"workflow-producer:{record.run_id}",
        )
        with pytest.raises(RuntimeError, match="producer failed before close snapshot"):
            await producer_task
        assert producer_task.done()
        workflow._tasks[record.run_id] = producer_task

        assert await workflow.close(timeout=1) == ()
        assert workflow._tasks == {}
        assert (
            run_manager.get_run(record.run_id)["status"]
            == RunStatus.CANCELLED.value
        )

    asyncio.run(run())


def test_workflow_close_sees_nonterminal_producer_after_done_callback(monkeypatch):
    class DummySubagent:
        pass

    class Runner:
        def validate_script(self, _script):
            return None

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=Runner(),
        )
        producer_callback_ran = asyncio.Event()
        original_consume = workflow._consume_producer_task

        async def producer(**_kwargs):
            raise RuntimeError("producer failed without terminalizing")

        def consume(run_id, task):
            original_consume(run_id, task)
            producer_callback_ran.set()

        monkeypatch.setattr(workflow, "_produce", producer)
        monkeypatch.setattr(workflow, "_consume_producer_task", consume)
        started = await workflow.start(
            conversation_id="conv-nonterminal-done-callback",
            script=VALID_WORKFLOW_SCRIPT,
        )
        run_id = started["run_id"]
        producer_task = workflow._tasks[run_id]
        await asyncio.wait_for(producer_callback_ran.wait(), timeout=1)
        assert producer_task.done()
        assert workflow._tasks.get(run_id) is producer_task
        assert (
            run_manager.get_run(run_id)["status"]
            == RunStatus.RUNNING.value
        )

        assert await workflow.close(timeout=1) == ()
        assert workflow._tasks == {}
        assert (
            run_manager.get_run(run_id)["status"]
            == RunStatus.CANCELLED.value
        )

    asyncio.run(run())


def test_workflow_close_reports_producer_that_swallows_cancellation():
    class DummySubagent:
        pass

    class StubbornRunner:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancellation_swallowed = asyncio.Event()
            self.release = asyncio.Event()

        def validate_script(self, _script):
            return None

        async def run(self, **_kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_swallowed.set()
                await self.release.wait()
                raise

    async def run():
        run_manager = RunManager()
        runner = StubbornRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
        )
        started = await workflow.start(
            conversation_id="conv-producer-stubborn",
            script=VALID_WORKFLOW_SCRIPT,
        )
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        run_id = started["run_id"]
        producer_task = workflow._tasks[run_id]
        task_name = f"workflow-producer:{run_id}"
        try:
            assert await workflow.close(timeout=0.01) == (task_name,)
            assert runner.cancellation_swallowed.is_set()

            runner.release.set()
            await _wait_until(producer_task.done)
            assert await workflow.close(timeout=1) == ()
            assert workflow._tasks == {}
            assert (
                run_manager.get_run(run_id)["status"]
                == RunStatus.CANCELLED.value
            )
        finally:
            runner.release.set()

    asyncio.run(run())


def test_workflow_close_retries_late_internal_producer_terminalization(monkeypatch):
    class DummySubagent:
        pass

    class Runner:
        def validate_script(self, _script):
            return None

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=Runner(),
        )
        producer_started = asyncio.Event()
        cancellation_swallowed = asyncio.Event()
        producer_release = asyncio.Event()
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )

        async def producer(**_kwargs):
            producer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_swallowed.set()
                await producer_release.wait()
                raise RuntimeError("producer failed after cancellation")

        monkeypatch.setattr(workflow, "_produce", producer)
        started = await workflow.start(
            conversation_id="conv-producer-late",
            script=VALID_WORKFLOW_SCRIPT,
        )
        run_id = started["run_id"]
        producer_task = workflow._tasks[run_id]
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        task_name = f"workflow-producer:{run_id}"
        try:
            assert await workflow.close(timeout=0.01) == (task_name,)
            assert cancellation_swallowed.is_set()

            producer_release.set()
            await _wait_until(producer_task.done)
            assert workflow._tasks.get(run_id) is producer_task
            assert run_manager.get_run(run_id)["status"] == RunStatus.RUNNING.value

            assert await workflow.close(timeout=1) == ()
            assert workflow._tasks == {}
            assert (
                run_manager.get_run(run_id)["status"]
                == RunStatus.CANCELLED.value
            )
            await asyncio.sleep(0)
            assert loop_errors == []
        finally:
            producer_release.set()

    asyncio.run(run())


def test_workflow_close_deadline_covers_retryable_late_terminalization(monkeypatch):
    class DummySubagent:
        pass

    class Runner:
        def validate_script(self, _script):
            return None

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=Runner(),
        )
        producer_started = asyncio.Event()
        producer_release = asyncio.Event()
        finish_entered = asyncio.Event()
        finish_release = asyncio.Event()
        finish_attempts = 0
        real_finish_run = run_manager.finish_run

        async def producer(**_kwargs):
            producer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await producer_release.wait()
                raise RuntimeError("producer failed after cancellation")

        async def blocking_then_successful_finish_run(*args, **kwargs):
            nonlocal finish_attempts
            finish_attempts += 1
            if finish_attempts == 1:
                finish_entered.set()
                await finish_release.wait()
                raise RuntimeError("transient terminalization failure")
            return await real_finish_run(*args, **kwargs)

        monkeypatch.setattr(workflow, "_produce", producer)
        started = await workflow.start(
            conversation_id="conv-terminalization-deadline",
            script=VALID_WORKFLOW_SCRIPT,
        )
        run_id = started["run_id"]
        producer_task = workflow._tasks[run_id]
        producer_name = f"workflow-producer:{run_id}"
        terminalize_name = f"workflow-terminalize:{run_id}"
        await asyncio.wait_for(producer_started.wait(), timeout=1)
        try:
            assert await workflow.close(timeout=0.01) == (producer_name,)
            producer_release.set()
            await _wait_until(producer_task.done)

            monkeypatch.setattr(run_manager, "finish_run", blocking_then_successful_finish_run)
            assert await asyncio.wait_for(workflow.close(timeout=0.01), timeout=0.2) == (
                terminalize_name,
            )
            await asyncio.wait_for(finish_entered.wait(), timeout=1)
            assert await workflow.close(timeout=0.01) == (terminalize_name,)
            assert finish_attempts == 1

            finish_release.set()
            await _wait_until(
                lambda: workflow._shutdown_terminalization_tasks[run_id].done()
            )

            assert await workflow.close(timeout=1) == ()
            assert finish_attempts == 2
            assert workflow._tasks == {}
            assert (
                run_manager.get_run(run_id)["status"]
                == RunStatus.CANCELLED.value
            )
        finally:
            producer_release.set()
            finish_release.set()

    asyncio.run(run())


def test_workflow_close_drains_blocked_notification_and_consumes_error(monkeypatch):
    class DummySubagent:
        pass

    class BlockingRunner:
        def __init__(self):
            self.release = asyncio.Event()

        def validate_script(self, _script):
            return None

        async def run(self, **_kwargs):
            await self.release.wait()
            return "done"

    async def run():
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        run_manager = RunManager()
        runner = BlockingRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
        )
        notification_started = asyncio.Event()
        notification_cancelled = asyncio.Event()

        async def notification(_run_id):
            notification_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                notification_cancelled.set()
                raise RuntimeError("notification failed after cancellation")

        monkeypatch.setattr(workflow, "_register_task_notification", notification)
        started = await workflow.start(
            conversation_id="conv-notification-close",
            script=VALID_WORKFLOW_SCRIPT,
        )
        await asyncio.wait_for(notification_started.wait(), timeout=1)
        assert [task.get_name() for task in workflow._notification_tasks] == [
            f"workflow-notification:{started['run_id']}"
        ]

        assert await workflow.close(timeout=1) == ()
        assert notification_cancelled.is_set()
        assert workflow._notification_tasks == set()
        await asyncio.sleep(0)
        assert loop_errors == []

        tasks = list(workflow._tasks.values())
        runner.release.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())


def test_workflow_close_reports_notification_that_swallows_cancellation(monkeypatch):
    class DummySubagent:
        pass

    class BlockingRunner:
        def __init__(self):
            self.release = asyncio.Event()

        def validate_script(self, _script):
            return None

        async def run(self, **_kwargs):
            await self.release.wait()
            return "done"

    async def run():
        run_manager = RunManager()
        runner = BlockingRunner()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=DummySubagent(),
            runner=runner,
        )
        notification_started = asyncio.Event()
        cancellation_swallowed = asyncio.Event()
        notification_release = asyncio.Event()

        async def notification(_run_id):
            notification_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_swallowed.set()
                await notification_release.wait()

        monkeypatch.setattr(workflow, "_register_task_notification", notification)
        started = await workflow.start(
            conversation_id="conv-notification-stubborn",
            script=VALID_WORKFLOW_SCRIPT,
        )
        await asyncio.wait_for(notification_started.wait(), timeout=1)
        task_name = f"workflow-notification:{started['run_id']}"
        try:
            assert [task.get_name() for task in workflow._notification_tasks] == [
                task_name
            ]
            producer_name = f"workflow-producer:{started['run_id']}"
            drain = await workflow.close(timeout=0.01)
            assert task_name in drain
            assert set(drain).issubset({task_name, producer_name})
            assert cancellation_swallowed.is_set()

            notification_release.set()
            await _wait_until(lambda: not workflow._notification_tasks)
            assert await workflow.close(timeout=1) == ()
        finally:
            notification_release.set()
            tasks = list(workflow._tasks.values())
            runner.release.set()
            await asyncio.gather(*tasks)

    asyncio.run(run())


def test_workflow_runtime_bridge_agent_result_uses_content_only():
    class FakeSubagentExecutor:
        async def start(self, **kwargs):
            return {"run_id": "child-1"}

    class FakeRunManager:
        def __init__(self):
            self.appended = []

        async def append_event(self, *args, **kwargs):
            self.appended.append((args, kwargs))
            return None

        async def wait_for_terminal_result(self, run_id, **kwargs):
            return {"status": "completed", "content": "RESULT=0.5"}

    async def run():
        run_manager = FakeRunManager()
        bridge = WorkflowRuntimeBridge(
            workflow_run_id="workflow-1",
            conversation_id="conversation-1",
            parent_node_id="node-1",
            run_manager=run_manager,
            subagent_executor=FakeSubagentExecutor(),
        )
        result = await bridge._agent({"input": "calculate"})
        assert result["content"] == "RESULT=0.5"
        assert "result" not in result
        assert not [
            args[1]
            for args, _ in run_manager.appended
            if len(args) > 1 and args[1].get("event_type") == "workflow_child_event"
        ]

    asyncio.run(run())


def test_wait_agent_reads_completed_workflow_result():
    async def run():
        run_manager = RunManager()

        class FakeSubagentExecutor:
            async def start(self, **kwargs):
                record = await run_manager.create_run(
                    conversation_id=kwargs["conversation_id"],
                    kind=RunKind.SUBAGENT,
                    created_by_run_id=kwargs.get("created_by_run_id"),
                    cancellation_parent_run_id=kwargs.get("cancellation_parent_run_id"),
                    anchor_node_id=kwargs.get("parent_node_id"),
                    summary=str(kwargs.get("input_data") or "")[:80],
                    metadata={"agent_name": kwargs.get("agent_name")},
                )
                asyncio.create_task(self._complete(record.run_id, str(kwargs.get("input_data") or "")))
                return {"run_id": record.run_id}

            async def _complete(self, run_id, prompt):
                await asyncio.sleep(0.01)
                if "x^2" in prompt:
                    content = "0.3333333333"
                elif "和" in prompt:
                    content = "和 = 0.8333333333"
                else:
                    content = "0.5"
                await run_manager.append_event(
                    run_id,
                    {"status": "complete", "event_type": "subagent_result", "content": content},
                )
                await run_manager.finish_run(run_id, RunStatus.COMPLETED)

        subagent_executor = FakeSubagentExecutor()
        workflow = WorkflowManager(
            run_manager=run_manager,
            subagent_executor=subagent_executor,
        )
        runtime = AgentRuntime(
            run_manager=run_manager,
            mailbox=AgentMailbox(),
            subagent_executor=subagent_executor,
            workflow_manager=workflow,
            capability_registry=object(),
        )
        chat_run = await run_manager.create_run(
            conversation_id="conv-workflow-read",
            kind=RunKind.CHAT,
            anchor_node_id="node-1",
            summary="chat",
        )
        workflow_run = await workflow.start(
            conversation_id="conv-workflow-read",
            script="""
export default async function workflow(ctx) {
  const first = await ctx.parallel([
    () => ctx.agent('计算 x 从 0 到 1 的积分', { agentType: 'workflow-worker' }),
    () => ctx.agent('计算 x^2 从 0 到 1 的积分', { agentType: 'workflow-worker' })
  ]);
  const sum = await ctx.agent(`计算两个结果的和: ${first[0].content}, ${first[1].content}`, { agentType: 'workflow-worker' });
  return { integral_x: first[0].content, integral_x2: first[1].content, sum: sum.content };
}
""",
            parent_node_id="node-1",
            created_by_run_id=chat_run.run_id,
            cancellation_parent_run_id=None,
            delivery_policy="silent",
        )

        result = await runtime.wait_agent(
            source=AgentSource(
                conversation_id="conv-workflow-read",
                run_id=chat_run.run_id,
                run_kind=RunKind.CHAT.value,
                anchor_node_id="node-1",
            ),
            run_ids=[str(workflow_run["run_id"])],
            timeout_seconds=3,
        )

        assert result["status"] == "completed"
        workflow_result = result["runs"][0]
        assert workflow_result["status"] == "completed"
        assert workflow_result["message_type"] == "result"
        assert workflow_result["event_type"] == "workflow_result"
        assert workflow_result["result"]["integral_x"] == "0.5"
        assert workflow_result["result"]["integral_x2"] == "0.3333333333"
        assert workflow_result["result"]["sum"] == "和 = 0.8333333333"
        assert '"integral_x"' in workflow_result["content"]

    asyncio.run(run())


def test_workflow_js_runner_rejects_forbidden_terms():
    runner = WorkflowJsRunner()
    with pytest.raises(WorkflowScriptError):
        runner.validate_script("return require('fs')")


def test_workflow_js_runner_phase_emits_start_and_end_events():
    class Bridge:
        def __init__(self):
            self.methods = []

        async def handle_call(self, method, params):
            self.methods.append((method, params))
            return {"ok": True}

    async def run():
        bridge = Bridge()
        result = await WorkflowJsRunner().run(
            script=(
                "export default async function workflow(ctx) {"
                "await ctx.phase('检查', async () => 'inner');"
                "return 'ok';"
                "}"
            ),
            args={},
            budget={"max_host_calls": 5, "max_seconds": 10},
            bridge=bridge,
        )
        assert result == "ok"
        assert [method for method, _ in bridge.methods] == ["phase_start", "phase_end"]
        assert bridge.methods[0][1] == {"name": "检查"}

    asyncio.run(run())


def test_workflow_js_runner_processes_parallel_host_calls_concurrently():
    class Bridge:
        def __init__(self):
            self.calls = []

        async def handle_call(self, method, params):
            self.calls.append((method, params, time.perf_counter()))
            if method == "agent":
                await asyncio.sleep(0.35)
                return {"name": params["name"]}
            raise RuntimeError(method)

    async def run():
        bridge = Bridge()
        started = time.perf_counter()
        result = await WorkflowJsRunner().run(
            script="""
export default async function workflow(ctx) {
  const result = await ctx.parallel([
    () => ctx.agent('one', { agentType: 'a' }),
    () => ctx.agent('two', { agentType: 'b' })
  ]);
  return result.map((item) => item.name).join(',');
}
""",
            args={},
            budget={"max_host_calls": 10, "max_seconds": 5},
            bridge=bridge,
        )
        elapsed = time.perf_counter() - started
        call_offsets = [call[2] - started for call in bridge.calls]
        assert result == "a,b"
        assert elapsed < 0.6
        assert max(call_offsets) - min(call_offsets) < 0.2

    asyncio.run(run())


def test_workflow_js_runner_kills_worker_when_cancelled(tmp_path):
    pid_file = tmp_path / "worker.pid"
    worker = tmp_path / "worker.mjs"
    worker.write_text(
        f"""
import fs from 'node:fs';
fs.writeFileSync({str(pid_file)!r}, String(process.pid));
setInterval(() => {{}}, 1000);
""",
        encoding="utf-8",
    )

    async def run():
        runner = WorkflowJsRunner(worker)
        task = asyncio.create_task(
            runner.run(
                script="""
export default async function workflow(ctx) {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  return 'done';
}
""",
                args={},
                budget={"max_host_calls": 5, "max_seconds": 10},
                bridge=object(),
            )
        )
        for _ in range(50):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_file.exists()
        pid = int(pid_file.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(50):
            if not process_exists(pid):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"workflow worker process {pid} was not terminated")

    asyncio.run(run())


def test_workflow_manager_stop_cancels_running_workflow():
    class DummySubagent:
        pass

    async def run():
        run_manager = RunManager()
        workflow = WorkflowManager(run_manager=run_manager, subagent_executor=DummySubagent())
        record = await workflow.start(
            conversation_id="conv-stop",
            script="""
export default async function workflow(ctx) {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  return 'done';
}
""",
            budget={"max_seconds": 5, "max_host_calls": 5},
        )
        await asyncio.sleep(0.05)
        assert await workflow.stop(record["run_id"]) is True
        for _ in range(50):
            state = run_manager.get_run(record["run_id"])
            if state["status"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.05)
        state = run_manager.get_run(record["run_id"])
        events = [
            event["payload"]
            for event in run_manager.journal.read_events("conv-stop", record["run_id"])
        ]
        assert state["status"] == "cancelled"
        assert "workflow_result" not in {event.get("event_type") for event in events}

    asyncio.run(run())


def test_stop_run_route_dispatches_workflow_stop():
    class DummyChat:
        async def stop_stream(self, node_id):
            raise AssertionError("chat stop should not be called for workflow")

    class DummySubagent:
        async def stop(self, run_id):
            raise AssertionError("subagent stop should not be called for workflow")

    class DummyWorkflow:
        def __init__(self):
            self.stopped = []

        async def stop(self, run_id):
            self.stopped.append(run_id)
            return True

    async def create_workflow_run(run_manager):
        return await run_manager.create_run(
            conversation_id="conv",
            kind=RunKind.WORKFLOW,
        )

    run_manager = RunManager()
    record = asyncio.run(create_workflow_run(run_manager))
    workflow = DummyWorkflow()
    app = FastAPI()
    app.dependency_overrides[get_run_manager] = lambda: run_manager
    app.dependency_overrides[get_chat_manager] = lambda: DummyChat()
    app.dependency_overrides[get_subagent_executor] = lambda: DummySubagent()
    app.dependency_overrides[get_workflow_manager] = lambda: workflow
    app.include_router(runs_route.router)

    response = TestClient(app).post(f"/runs/{record.run_id}/stop")

    assert response.status_code == 200
    assert workflow.stopped == [record.run_id]
