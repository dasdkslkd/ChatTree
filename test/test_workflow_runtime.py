import asyncio
import os
import subprocess
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import (
    get_chat_manager,
    get_run_manager,
    get_subagent_executor,
    get_workflow_manager,
)
from backend.api.routes import runs as runs_route
from backend.core.runs import RunManager
from backend.core.runs.types import RunKind
from backend.core.workflows import WorkflowManager
from backend.core.workflows.js_runner import WorkflowJsRunner, WorkflowScriptError


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
            if method == "workflow":
                return {"run_id": "run-test"}
            raise RuntimeError(method)

    async def run():
        result = await WorkflowJsRunner().run(
            script="await log('hello'); const info = await workflow(); return info.run_id;",
            args={},
            budget={"max_host_calls": 5, "max_seconds": 10},
            bridge=Bridge(),
        )
        assert result == "run-test"

    asyncio.run(run())


def test_workflow_js_runner_rejects_forbidden_terms():
    runner = WorkflowJsRunner()
    with pytest.raises(WorkflowScriptError):
        runner.validate_script("return require('fs')")


def test_workflow_js_runner_exposes_phase_start_and_phase_end_aliases():
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
                "await phase_start('检查', { step: 1 });"
                "await phase_end('检查', { ok: true });"
                "return 'ok';"
            ),
            args={},
            budget={"max_host_calls": 5, "max_seconds": 10},
            bridge=bridge,
        )
        assert result == "ok"
        assert [method for method, _ in bridge.methods] == ["phase_start", "phase_end"]
        assert bridge.methods[0][1] == {"name": "检查", "data": {"step": 1}}

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
const result = await parallel([
  () => agent('a', 'one'),
  () => agent('b', 'two')
]);
return result.map((item) => item.name).join(',');
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
                script="return 'never';",
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
            script="await new Promise((resolve) => setTimeout(resolve, 1000)); return 'done';",
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
