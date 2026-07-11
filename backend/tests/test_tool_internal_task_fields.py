import json
import tempfile
import unittest

from backend.core.agents.runtime import AgentRuntime
from backend.core.runs import RunKind, RunManager
from backend.core.tasks import ActiveTaskService
from backend.core.tools.agent_tools import SpawnAgentTool, StartSubagentTool, StartWorkflowTool
from backend.core.tools.code_tools import CodeToolConfig, RunCommandTool
from backend.core.tools.command_tools import StartBackgroundCommandTool
from backend.core.tools.task_contract import TASK_STEP_BINDING_DESCRIPTION


REMOVED_FIELDS = {"task_id", "task_step_id", "auto_create_task"}


class FakeCommandExecutor:
    def __init__(self) -> None:
        self.started = None

    async def start(self, **kwargs):
        self.started = kwargs
        return {"run_id": "run_command_1", "metadata": dict(kwargs.get("metadata") or {})}

    async def wait(self, run_id, timeout):
        return None

    def snapshot(self, run_id):
        return {"status": "completed", "exit_code": 0, "stdout": "", "stderr": "", "shell": {"id": "test"}}

    async def mark_observed(self, run_id, *, observer_run_id=None, via=""):
        return self.snapshot(run_id)


class FakeAgentRuntime:
    def __init__(self) -> None:
        self.spawned = None
        self.workflow = None

    async def spawn_agent(self, **kwargs):
        self.spawned = kwargs
        return {"run_id": "agent_run_1", "kind": "subagent", "status": "running", "step": kwargs.get("step")}

    async def start_workflow(self, **kwargs):
        self.workflow = kwargs
        return {"run_id": "workflow_run_1", "kind": "workflow", "status": "running", "step": kwargs.get("step")}


class FakeAgentRunManager:
    def __init__(self) -> None:
        self.run = {
            "run_id": "agent_run_1",
            "conversation_id": "conv_1",
            "kind": "subagent",
            "status": "running",
            "summary": "Implement the feature",
            "event_count": 2,
            "created_at": 10.0,
            "updated_at": 11.0,
            "finished_at": None,
            "created_by_run_id": "parent_run_1",
            "metadata": {
                "agent_name": "implementer",
                "delivery_policy": "auto",
                "delegated_task": "Implement the feature",
                "task_generation_id": "generation-internal",
                "task_revision": 4,
                "task_step_position": 2,
            },
        }

    def get_run(self, run_id):
        return dict(self.run) if run_id == self.run["run_id"] else None

    def read_events(self, run_id):
        return []

    def list_runs(self, conversation_id):
        return [dict(self.run)] if conversation_id == self.run["conversation_id"] else []


class ToolInternalTaskFieldTests(unittest.IsolatedAsyncioTestCase):
    def test_background_run_schemas_expose_step_but_no_internal_task_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CodeToolConfig.from_dict({"workspace_roots": [tmpdir]})
            tools = [
                RunCommandTool(config),
                StartBackgroundCommandTool(config),
                SpawnAgentTool(agent_runtime=FakeAgentRuntime()),
                StartSubagentTool(agent_runtime=FakeAgentRuntime()),
                StartWorkflowTool(agent_runtime=FakeAgentRuntime()),
            ]

            for tool in tools:
                with self.subTest(tool=tool.name):
                    properties = set(tool.parameters_schema().get("properties", {}))
                    self.assertIn("step", properties)
                    self.assertFalse(properties & REMOVED_FIELDS)
                    self.assertEqual(
                        tool.parameters_schema()["properties"]["step"]["description"],
                        TASK_STEP_BINDING_DESCRIPTION,
                    )

    async def test_command_tools_forward_only_step_and_runtime_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CodeToolConfig.from_dict({"workspace_roots": [tmpdir], "run_command_initial_wait_seconds": 5})
            context = {
                "conversation_id": "conv_1",
                "node_id": "node_1",
                "run_id": "parent_run_1",
                "task_context_mode": "attached",
                "task_generation_id": "generation-internal",
                "task_revision": 4,
            }

            for tool_class in (RunCommandTool, StartBackgroundCommandTool):
                executor = FakeCommandExecutor()
                raw = await tool_class(config).execute(
                    command="echo ok",
                    cwd=".",
                    step=2,
                    task_id="removed",
                    task_step_id="removed",
                    _runtime_context={**context, "command_executor": executor},
                )
                self.assertNotIn("error", json.loads(raw))
                self.assertEqual(executor.started["step"], 2)
                self.assertEqual(executor.started["task_context_mode"], "attached")
                self.assertEqual(executor.started["task_generation_id"], "generation-internal")
                self.assertEqual(executor.started["task_revision"], 4)
                self.assertFalse(REMOVED_FIELDS & set(executor.started["metadata"]))

    async def test_agent_tools_forward_numbered_step_without_internal_ids(self):
        context = {
            "conversation_id": "conv_1",
            "node_id": "node_1",
            "run_id": "parent_run_1",
            "task_context_mode": "attached",
            "task_generation_id": "generation-internal",
            "task_revision": 4,
        }

        runtime = FakeAgentRuntime()
        await SpawnAgentTool(agent_runtime=runtime).execute(
            agent_name="implementer",
            task="do work",
            step=3,
            task_id="removed",
            _runtime_context=context,
        )
        self.assertEqual(runtime.spawned["step"], 3)
        self.assertEqual(runtime.spawned["task_generation_id"], "generation-internal")
        self.assertEqual(runtime.spawned["task_revision"], 4)
        self.assertFalse(REMOVED_FIELDS & set(runtime.spawned))

        runtime = FakeAgentRuntime()
        await StartSubagentTool(agent_runtime=runtime).execute(
            task="do work",
            step=3,
            task_step_id="removed",
            _runtime_context=context,
        )
        self.assertEqual(runtime.spawned["step"], 3)
        self.assertFalse(REMOVED_FIELDS & set(runtime.spawned))

        runtime = FakeAgentRuntime()
        await StartWorkflowTool(agent_runtime=runtime).execute(
            script="export default async function workflow(ctx) { return {}; }",
            step=3,
            auto_create_task=False,
            _runtime_context=context,
        )
        self.assertEqual(runtime.workflow["step"], 3)
        self.assertFalse(REMOVED_FIELDS & set(runtime.workflow))

    async def test_agent_status_results_expose_step_without_internal_task_metadata(self):
        run_manager = FakeAgentRunManager()
        runtime = AgentRuntime(
            run_manager=run_manager,
            mailbox=None,
            subagent_executor=None,
            capability_registry=None,
        )

        progress = runtime._run_progress_snapshot("agent_run_1")
        listed = await runtime.list_agents(conversation_id="conv_1")

        self.assertEqual(progress["step"], 2)
        self.assertNotIn("task_step", progress)
        self.assertEqual(listed["runs"][0]["step"], 2)
        serialized = json.dumps(listed)
        self.assertNotIn("task_generation_id", serialized)
        self.assertNotIn("task_revision", serialized)
        self.assertNotIn("task_step_position", serialized)

    async def test_run_events_expose_step_without_internal_task_metadata(self):
        run_manager = RunManager()
        task_service = ActiveTaskService(run_manager=run_manager)
        run_manager.task_service = task_service
        task = await task_service.create_task(
            conversation_id="conv_1",
            title="Bound event",
            steps=[{"title": "Execute"}],
        )
        parent = await run_manager.create_run(
            conversation_id="conv_1",
            kind=RunKind.CHAT,
        )
        binding = await task_service.prepare_run_binding(
            conversation_id="conv_1",
            step=1,
            context_mode="attached",
            expected_generation=task.generation_id,
            expected_revision=task.revision,
        )
        child = await run_manager.create_run(
            conversation_id="conv_1",
            kind=RunKind.COMMAND,
            created_by_run_id=parent.run_id,
            task_binding=binding,
        )

        child_events = run_manager.read_events(child.run_id)
        parent_events = run_manager.read_events(parent.run_id)
        serialized = json.dumps({"child": child_events, "parent": parent_events})

        self.assertNotIn("task_generation_id", serialized)
        self.assertNotIn("task_revision", serialized)
        self.assertNotIn("task_step_position", serialized)
        self.assertEqual(child_events[0]["step"], 1)
        self.assertEqual(parent_events[1]["payload"]["step"], 1)
