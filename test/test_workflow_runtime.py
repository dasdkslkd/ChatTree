import asyncio

import pytest

from backend.core.workflows.js_runner import WorkflowJsRunner, WorkflowScriptError


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
