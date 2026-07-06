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
from backend.core.runs import RunManager
from backend.core.runs.types import RunKind, RunStatus
from backend.core.workflows import WorkflowManager
from backend.core.workflows.js_runner import WorkflowJsRunner, WorkflowScriptError
from backend.core.workflows.runtime_bridge import WorkflowRuntimeBridge


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
                    parent_run_id=kwargs.get("parent_run_id"),
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
            parent_run_id=chat_run.run_id,
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
