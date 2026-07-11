import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.api.routes.run_control import stop_run_tree
from backend.core.command_runtime import CommandExecutor
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.notifications import TaskNotificationService
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.tasks import ActiveTaskService, TaskContextDisabledError
from backend.core.tools.code_tools import CodeToolConfig, RunCommandTool
from backend.core.tools.command_tools import (
    ReadCommandTool,
    StartBackgroundCommandTool,
    StopCommandTool,
    WaitCommandTool,
)
from backend.core.tools.tool_manager import ToolManager


class MemoryNotificationRepository:
    def __init__(self):
        self.items = {}

    def upsert_for_run(self, **kwargs):
        source_run_id = kwargs["source_run_id"]
        item = self.items.get(source_run_id) or {
            "id": f"notification-{source_run_id}",
            "status": "unbound",
        }
        item.update(kwargs)
        self.items[source_run_id] = item
        return dict(item)

    def mark_observed_by_source(self, source_run_id):
        item = self.items.get(source_run_id)
        if item:
            item["status"] = "observed"
        return dict(item) if item else None


def install_notification_service(run_manager: RunManager) -> MemoryNotificationRepository:
    repository = MemoryNotificationRepository()
    run_manager.notification_service = TaskNotificationService(
        repository=repository,
        run_manager=run_manager,
    )
    return repository


class FailingNotificationService:
    async def register_run_notification(self, **kwargs):
        raise RuntimeError("notification store unavailable")

    async def publish_run_notification(self, **kwargs):
        raise RuntimeError("notification store unavailable")


class CommandExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_tool_descriptions_expose_shell_profile_and_background_boundaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            })
            run_tool = RunCommandTool(config)
            start_tool = StartBackgroundCommandTool(config)

            self.assertIn("auto-backgrounds", run_tool.description)
            self.assertIn("active shell", run_tool.description)
            self.assertNotIn("start_terminal", run_tool.description)
            self.assertIn("true background command", start_tool.description)
            self.assertIn("active shell", start_tool.description)
            self.assertIn("Do not report completion", start_tool.description)

    async def test_background_start_result_is_explicitly_launch_only(self):
        class StartOnlyExecutor:
            async def start(self, **kwargs):
                return {
                    "run_id": "run-background",
                    "metadata": {"shell": {"id": "powershell"}},
                }

            def snapshot(self, run_id):
                return {"status": "running", "shell": {"id": "powershell"}}

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = StartBackgroundCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            }))
            payload = json.loads(await tool.execute(
                command="Write-Output 1",
                cwd=".",
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": "parent_run_1",
                    "command_executor": StartOnlyExecutor(),
                },
            ))

        self.assertEqual(payload["status"], "running")
        self.assertIs(payload["result_observed"], False)
        self.assertIn("does not contain the command result", payload["message"])

    async def test_run_command_short_managed_command_returns_sync_output_and_suppresses_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            command_executor = CommandExecutor(run_manager)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "run_command_initial_wait_seconds": 5,
            }))

            raw = await tool.execute(
                command=f"{sys.executable} -c \"print('managed-short')\"",
                cwd=".",
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": parent.run_id,
                    "command_executor": command_executor,
                },
            )
            payload = json.loads(raw)
            await run_manager.finish_run(parent.run_id, RunStatus.COMPLETED)

            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["timed_out"], False)
            self.assertEqual(payload["background"], False)
            self.assertEqual(payload["kind"], RunKind.COMMAND.value)
            self.assertIn("managed-short", payload["stdout"])
            self.assertIn("command_run_id", payload)
            self.assertIn("shell", payload)
            self.assertNotIn("terminal_run_id", payload)
            self.assertEqual(notifications.items[payload["command_run_id"]]["status"], "observed")

    async def test_workflow_worker_run_command_does_not_create_task_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            command_executor = CommandExecutor(run_manager)
            worker = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.SUBAGENT,
                anchor_node_id="node_1",
                summary="workflow worker",
                metadata={"agent_name": "workflow-worker", "delivery_policy": "silent"},
            )
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "run_command_initial_wait_seconds": 5,
            }))

            raw = await tool.execute(
                command=f"{sys.executable} -c \"print('workflow-worker-managed')\"",
                cwd=".",
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": worker.run_id,
                    "run_kind": RunKind.SUBAGENT.value,
                    "agent_name": "workflow-worker",
                    "delivery_policy": "silent",
                    "command_executor": command_executor,
                },
            )
            payload = json.loads(raw)

            self.assertEqual(payload["exit_code"], 0)
            run = run_manager.get_run(payload["command_run_id"]) or {}
            self.assertIs((run.get("metadata") or {}).get("suppress_task_notification"), True)
            self.assertEqual(notifications.items, {})

    async def test_run_command_short_managed_command_truncates_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "run_command_initial_wait_seconds": 5,
                "max_output_chars": 20,
            }))

            raw = await tool.execute(
                command=f"{sys.executable} -c \"import sys; print('x' * 200); sys.stderr.write('y' * 200)\"",
                cwd=".",
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": "parent_run_1",
                    "command_executor": command_executor,
                },
            )
            payload = json.loads(raw)

            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(len(payload["stdout"]), 20)
            self.assertEqual(len(payload["stderr"]), 20)

    async def test_run_command_long_managed_command_auto_backgrounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "run_command_initial_wait_seconds": 0.1,
            }))

            raw = await tool.execute(
                command=f"{sys.executable} -c \"import time; print('managed-started', flush=True); time.sleep(30)\"",
                cwd=".",
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": "parent_run_1",
                    "command_executor": command_executor,
                },
            )
            payload = json.loads(raw)

            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["kind"], RunKind.COMMAND.value)
            self.assertEqual(payload["background"], True)
            self.assertEqual(payload["auto_backgrounded"], True)
            self.assertIn("command_run_id", payload)
            self.assertNotIn("terminal_run_id", payload)
            run = run_manager.get_run(payload["command_run_id"])
            self.assertTrue((run.get("metadata") or {}).get("run_command_auto_backgrounded"))

            try:
                await command_executor.stop(payload["command_run_id"])
                await command_executor.wait(payload["command_run_id"], timeout=5)
            finally:
                await command_executor.stop(payload["command_run_id"])
            self.assertEqual(run_manager.get_run(payload["command_run_id"])["status"], RunStatus.CANCELLED.value)

    async def test_command_named_tools_are_model_visible_and_legacy_names_are_removed(self):
        manager = ToolManager({
            "tools": {
                "builtin": {
                    "code": {
                        "enabled": True,
                        "workspace_roots": [tempfile.gettempdir()],
                    },
                },
            },
        })

        visible = set(manager.list_tools())
        self.assertTrue({"run_command", "start_background_command", "read_command", "wait_command", "stop_command"} <= visible)
        for legacy_name in {"start_command", "start_terminal", "read_terminal", "wait_terminal", "stop_terminal"}:
            self.assertNotIn(legacy_name, visible)
            self.assertIsNone(manager.get_tool(legacy_name))

    async def test_builtin_web_search_uses_top_level_web_search_config(self):
        manager = ToolManager({
            "tools": {
                "builtin": {
                    "code": {"enabled": False},
                },
                "web_search": {
                    "enabled": True,
                    "searxng": {
                        "searxng_url": "http://searxng.example.test",
                    },
                },
            },
        })

        web_search = manager.get_tool("web_search")
        self.assertIsNotNone(web_search)
        self.assertEqual(web_search.searxng_url, "http://searxng.example.test")

    async def test_command_control_tools_wait_read_and_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            start_tool = StartBackgroundCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            }))

            started = json.loads(await start_tool.execute(
                command=f"{sys.executable} -c \"import sys; print('out'); print('err', file=sys.stderr)\"",
                cwd=".",
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": "parent_run_1",
                    "command_executor": command_executor,
                },
            ))
            waited = json.loads(await WaitCommandTool().execute(
                command_run_id=started["command_run_id"],
                timeout_seconds=5,
                _runtime_context={
                    "command_executor": command_executor,
                    "run_id": "parent_run_1",
                },
            ))
            read = json.loads(await ReadCommandTool().execute(
                command_run_id=started["command_run_id"],
                _runtime_context={
                    "command_executor": command_executor,
                    "run_id": "parent_run_1",
                },
            ))
            stopped = json.loads(await StopCommandTool().execute(
                command_run_id=started["command_run_id"],
                _runtime_context={"command_executor": command_executor},
            ))

            self.assertEqual(waited["status"], RunStatus.COMPLETED.value)
            self.assertIn("out", read["stdout"])
            self.assertIn("err", read["stderr"])
            self.assertFalse(stopped["stopped"])

    async def test_background_command_streams_events_and_enqueues_completion_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            command_executor = CommandExecutor(run_manager)

            run = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('hello-command')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                summary="say hello",
            )
            await command_executor.wait(run["run_id"], timeout=5)

            record = run_manager.get_run(run["run_id"])
            self.assertEqual(record["kind"], RunKind.COMMAND.value)
            self.assertEqual(record["status"], RunStatus.COMPLETED.value)

            events = [
                event["payload"]
                for event in run_manager.journal.read_from_index("conv_1", run["run_id"], 0)
            ]
            event_types = [event.get("event_type") or event.get("type") for event in events]
            self.assertIn("command_started", event_types)
            self.assertIn("command_stdout", event_types)
            self.assertIn("command_exited", event_types)

            self.assertEqual(len(notifications.items), 1)
            notification = list(notifications.items.values())[0]
            self.assertEqual(notification["source_run_kind"], RunKind.COMMAND.value)
            payload = json.loads(notification["content"])
            self.assertEqual(payload["command_run_id"], run["run_id"])
            self.assertNotIn("terminal_run_id", payload)
            self.assertIn("hello-command", payload["stdout_tail"])

    async def test_notification_failure_does_not_block_or_fail_command_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            run_manager.notification_service = FailingNotificationService()
            command_executor = CommandExecutor(run_manager)

            run = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "print(\'still-runs\')"',
                cwd=tmpdir,
                anchor_node_id="node_1",
            )
            await command_executor.wait(run["run_id"], timeout=5)

            record = run_manager.get_run(run["run_id"])
            self.assertEqual(record["status"], RunStatus.COMPLETED.value)

    async def test_workflow_child_command_does_not_create_task_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            command_executor = CommandExecutor(run_manager)
            workflow = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.WORKFLOW,
                anchor_node_id="node_1",
                summary="workflow",
            )
            worker = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.SUBAGENT,
                anchor_node_id="node_1",
                created_by_run_id=workflow.run_id,
                cancellation_parent_run_id=workflow.run_id,
                summary="workflow worker",
                metadata={"agent_name": "workflow-worker", "delivery_policy": "silent"},
            )

            run = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('workflow-command')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                created_by_run_id=worker.run_id,
                cancellation_parent_run_id=worker.run_id,
                summary="workflow command",
            )
            await command_executor.wait(run["run_id"], timeout=5)

            self.assertEqual(notifications.items, {})

    async def test_workflow_worker_command_tool_marks_notification_suppressed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            command_executor = CommandExecutor(run_manager)
            worker = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.SUBAGENT,
                anchor_node_id="node_1",
                summary="workflow worker",
                metadata={"agent_name": "workflow-worker"},
            )
            tool = StartBackgroundCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            }))

            raw = await tool.execute(
                command=f"{sys.executable} -c \"print('workflow-worker-command')\"",
                cwd=tmpdir,
                _runtime_context={
                    "conversation_id": "conv_1",
                    "run_id": worker.run_id,
                    "run_kind": RunKind.SUBAGENT.value,
                    "anchor_node_id": "node_1",
                    "agent_name": "workflow-worker",
                    "delivery_policy": "silent",
                    "command_executor": command_executor,
                },
            )
            result = json.loads(raw)
            await command_executor.wait(result["run_id"], timeout=5)

            run = run_manager.get_run(result["run_id"]) or {}
            self.assertIs((run.get("metadata") or {}).get("suppress_task_notification"), True)
            self.assertEqual(notifications.items, {})

    async def test_command_snapshot_reads_repository_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence = SQLitePersistence(tmpdir)
            persistence.initialize()
            chat = ChatRepository(persistence)
            runs = SQLiteRunRepository(persistence)
            conversation_id = chat.create_conversation(title="repo command")
            node_id = chat.create_node(conversation_id, parent_id=None)
            run_manager = RunManager(repository=runs)
            command_executor = CommandExecutor(run_manager)
            record = await run_manager.create_run(
                conversation_id=conversation_id,
                kind=RunKind.COMMAND,
                anchor_node_id=node_id,
                summary="repo snapshot",
                metadata={"command": "echo repo"},
            )

            await run_manager.append_event(record.run_id, {
                "event_type": "command_stdout",
                "status": "content",
                "content": "repo-out\n",
            })
            await run_manager.append_event(record.run_id, {
                "event_type": "command_stderr",
                "status": "content",
                "content": "repo-err\n",
            })
            await run_manager.append_event(record.run_id, {
                "event_type": "command_exited",
                "status": "content",
                "exit_code": 0,
                "duration_seconds": 0.1,
                "command_status": RunStatus.COMPLETED.value,
            })
            await run_manager.finish_run(record.run_id, RunStatus.COMPLETED)

            snapshot = command_executor.snapshot(record.run_id)

            self.assertEqual(snapshot["stdout"], "repo-out\n")
            self.assertEqual(snapshot["stderr"], "repo-err\n")
            self.assertEqual(snapshot["exit_code"], 0)
            self.assertEqual(snapshot["status"], RunStatus.COMPLETED.value)

    async def test_background_command_notification_does_not_create_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            task_service = ActiveTaskService(run_manager=run_manager)
            run_manager.task_service = task_service
            command_executor = CommandExecutor(run_manager, task_service=task_service)

            run = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('task-command')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                summary="task command",
            )
            await command_executor.wait(run["run_id"], timeout=5)

            self.assertIsNone(await task_service.get_active_task("conv_1"))
            record = run_manager.get_run(run["run_id"])
            notification = notifications.items[run["run_id"]]
            self.assertNotIn("task_id", notification)
            self.assertNotIn("task_id", record["metadata"])

    async def test_command_binds_only_the_explicit_numbered_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            install_notification_service(run_manager)
            task_service = ActiveTaskService(run_manager=run_manager)
            run_manager.task_service = task_service
            command_executor = CommandExecutor(run_manager, task_service=task_service)
            task = await task_service.create_task(
                conversation_id="conv_1",
                title="三步任务",
                steps=[{"title": "输出 1"}, {"title": "输出 2"}],
            )

            run = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('active-step')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                summary="active step command",
                step=1,
                task_context_mode="attached",
                task_generation_id=task.generation_id,
                task_revision=task.revision,
            )
            await command_executor.wait(run["run_id"], timeout=5)

            updated = await task_service.get_active_task("conv_1")
            self.assertEqual(updated.steps[0].evidence_run_id, run["run_id"])
            self.assertEqual(updated.steps[0].status.value, "completed")
            self.assertEqual(updated.steps[1].status.value, "pending")
            record = run_manager.get_run(run["run_id"])
            self.assertNotIn("task_id", record["metadata"])
            self.assertNotIn("task_step_id", record["metadata"])
            self.assertEqual(record["metadata"]["task_step_position"], 1)
            self.assertEqual(record["metadata"]["task_outcome"]["step"], 1)
            self.assertEqual(record["metadata"]["task_outcome"]["step_status"], "completed")
            self.assertEqual(record["metadata"]["task_outcome"]["task_status"], "active")
            snapshot = command_executor.snapshot(run["run_id"])
            self.assertEqual(snapshot["step"], 1)
            self.assertEqual(
                snapshot["task_outcome"]["task_snapshot"]["steps"][0]["status"],
                "completed",
            )
            self.assertNotIn("task_outcome", snapshot["metadata"])
            self.assertNotIn("task_generation_id", json.dumps(snapshot))
            self.assertNotIn("task_step_position", json.dumps(snapshot))

    async def test_run_command_foreground_result_exposes_public_task_outcome(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            task_service = ActiveTaskService(run_manager=run_manager)
            run_manager.task_service = task_service
            command_executor = CommandExecutor(run_manager, task_service=task_service)
            task = await task_service.create_task(
                conversation_id="conv_1",
                title="前台命令任务",
                steps=[{"title": "输出结果"}],
            )
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "run_command_initial_wait_seconds": 5,
            }))

            payload = json.loads(await tool.execute(
                command=f"{sys.executable} -c \"print('task-finished')\"",
                cwd=".",
                step=1,
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_2",
                    "anchor_node_id": "node_1",
                    "run_id": parent.run_id,
                    "task_context_mode": "attached",
                    "task_generation_id": task.generation_id,
                    "task_revision": task.revision,
                    "command_executor": command_executor,
                },
            ))

            self.assertEqual(payload["status"], RunStatus.COMPLETED.value)
            self.assertEqual(payload["task_outcome"]["task_status"], "completed")
            self.assertEqual(payload["task_outcome"]["step"], 1)
            self.assertEqual(payload["task_outcome"]["task_snapshot"]["steps"][0]["status"], "completed")
            self.assertEqual(payload["step"], 1)
            self.assertNotIn("task_generation_id", json.dumps(payload))
            self.assertNotIn("task_step_position", json.dumps(payload))

    async def test_detached_command_step_is_rejected_before_run_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            task_service = ActiveTaskService(run_manager=run_manager)
            run_manager.task_service = task_service
            command_executor = CommandExecutor(run_manager, task_service=task_service)
            task = await task_service.create_task(
                conversation_id="conv_1",
                title="命令任务",
                steps=[{"title": "执行"}],
            )

            with self.assertRaises(TaskContextDisabledError):
                await command_executor.start(
                    conversation_id="conv_1",
                    command=f"{sys.executable} -c \"print('no-run')\"",
                    cwd=tmpdir,
                    anchor_node_id="node_1",
                    step=1,
                    task_context_mode="detached",
                    task_generation_id=task.generation_id,
                    task_revision=task.revision,
                )

            self.assertEqual(run_manager.list_runs("conv_1"), [])

    async def test_wait_command_marks_final_result_observed_and_suppresses_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            command_executor = CommandExecutor(run_manager)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )
            run = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('joined-command')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                created_by_run_id=parent.run_id,
                cancellation_parent_run_id=parent.run_id,
            )

            waited = json.loads(await WaitCommandTool().execute(
                command_run_id=run["run_id"],
                timeout_seconds=5,
                _runtime_context={
                    "command_executor": command_executor,
                    "run_id": parent.run_id,
                },
            ))
            await run_manager.finish_run(parent.run_id, RunStatus.COMPLETED)

            self.assertEqual(waited["status"], RunStatus.COMPLETED.value)
            self.assertIn("joined-command", waited["stdout"])
            updated = run_manager.get_run(run["run_id"])
            self.assertEqual(updated["metadata"]["result_observed_by_run_id"], parent.run_id)
            self.assertEqual(updated["metadata"]["result_observed_via"], "wait_command")
            self.assertEqual(notifications.items[run["run_id"]]["status"], "observed")

    async def test_wait_command_marks_stopped_result_observed(self):
        class StoppedExecutor:
            def __init__(self):
                self.observed = None

            async def wait(self, run_id, timeout=None):
                return None

            def snapshot(self, run_id):
                return {"command_run_id": run_id, "status": RunStatus.STOPPED.value}

            async def mark_observed(self, run_id, *, observer_run_id, via):
                self.observed = (run_id, observer_run_id, via)

        executor = StoppedExecutor()
        result = json.loads(await WaitCommandTool().execute(
            command_run_id="run_stopped",
            timeout_seconds=5,
            _runtime_context={
                "command_executor": executor,
                "run_id": "run_parent",
            },
        ))

        self.assertEqual(result["status"], RunStatus.STOPPED.value)
        self.assertEqual(executor.observed, ("run_stopped", "run_parent", "wait_command"))

    async def test_run_command_foreground_marks_stopped_result_observed(self):
        class StoppedForegroundExecutor:
            def __init__(self):
                self.observed = None

            async def start(self, **kwargs):
                return {"run_id": "run_stopped_foreground", "metadata": {}}

            async def wait(self, run_id, timeout=None):
                return None

            def snapshot(self, run_id):
                return {
                    "command_run_id": run_id,
                    "status": RunStatus.STOPPED.value,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "shell": {"id": "powershell"},
                }

            async def mark_observed(self, run_id, *, observer_run_id, via):
                self.observed = (run_id, observer_run_id, via)

        with tempfile.TemporaryDirectory() as tmpdir:
            executor = StoppedForegroundExecutor()
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "run_command_initial_wait_seconds": 5,
            }))

            result = json.loads(await tool.execute(
                command="echo stopped",
                cwd=".",
                _runtime_context={
                    "command_executor": executor,
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": "run_parent",
                },
            ))

        self.assertEqual(result["status"], RunStatus.STOPPED.value)
        self.assertEqual(
            executor.observed,
            ("run_stopped_foreground", "run_parent", "run_command"),
        )

    async def test_command_start_exception_records_actionable_error_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)

            with patch(
                "backend.core.command_runtime.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                run = await command_executor.start(
                    conversation_id="conv_1",
                    command="failing command",
                    cwd=tmpdir,
                    anchor_node_id="node_1",
                )
                await command_executor.wait(run["run_id"], timeout=5)

            snapshot = command_executor.snapshot(run["run_id"])
            self.assertEqual(snapshot["status"], RunStatus.FAILED.value)
            self.assertIn("RuntimeError", snapshot["error"])
            self.assertIn("boom", snapshot["error"])
            self.assertIn("failing command", snapshot["error"])
            self.assertIn(str(Path(tmpdir).resolve()), snapshot["error"])

    async def test_stop_run_tree_recursively_stops_cancellation_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )
            child = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"import time; time.sleep(30)\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                created_by_run_id=parent.run_id,
                cancellation_parent_run_id=parent.run_id,
            )

            stopped = await stop_run_tree(
                parent.run_id,
                run_manager=run_manager,
                command_executor=command_executor,
            )
            await command_executor.wait(child["run_id"], timeout=5)

            self.assertIn(parent.run_id, stopped)
            self.assertIn(child["run_id"], stopped)
            self.assertEqual(run_manager.get_run(child["run_id"])["status"], RunStatus.CANCELLED.value)

    async def test_model_started_background_command_is_not_stopped_with_creator_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )

            config = CodeToolConfig(workspace_roots=[Path(tmpdir)], protected_paths=[])
            started = json.loads(await StartBackgroundCommandTool(config).execute(
                command=f"{sys.executable} -c \"import time; time.sleep(0.2); print('still-running')\"",
                cwd=tmpdir,
                _runtime_context={
                    "conversation_id": "conv_1",
                    "anchor_node_id": "node_1",
                    "node_id": "node_2",
                    "run_id": parent.run_id,
                    "run_kind": "chat",
                    "command_executor": command_executor,
                },
            ))

            child = run_manager.get_run(started["run_id"])
            self.assertEqual(child["created_by_run_id"], parent.run_id)
            self.assertIsNone(child["cancellation_parent_run_id"])

            stopped = await stop_run_tree(
                parent.run_id,
                run_manager=run_manager,
                command_executor=command_executor,
            )
            await command_executor.wait(started["run_id"], timeout=5)

            self.assertIn(parent.run_id, stopped)
            self.assertNotIn(started["run_id"], stopped)
            self.assertEqual(run_manager.get_run(started["run_id"])["status"], RunStatus.COMPLETED.value)


if __name__ == "__main__":
    unittest.main()
