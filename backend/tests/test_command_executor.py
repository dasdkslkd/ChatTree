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
from backend.core.tasks import TaskLedger, TaskStatus
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

    async def test_background_command_auto_creates_task_for_standalone_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            notifications = install_notification_service(run_manager)
            task_ledger = TaskLedger()
            task_ledger.install_run_finish_listener(run_manager)
            command_executor = CommandExecutor(run_manager, task_ledger=task_ledger)

            run = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('task-command')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                summary="task command",
            )
            await command_executor.wait(run["run_id"], timeout=5)

            tasks = await task_ledger.list_tasks("conv_1")
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].owner_run_id, run["run_id"])
            self.assertEqual(tasks[0].status, TaskStatus.COMPLETED)
            record = run_manager.get_run(run["run_id"])
            self.assertEqual(record["metadata"]["task_id"], tasks[0].task_id)
            notification = notifications.items[run["run_id"]]
            self.assertEqual(notification["task_id"], tasks[0].task_id)

    async def test_command_binds_existing_task_id_from_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            task_ledger = TaskLedger()
            task_ledger.install_run_finish_listener(run_manager)
            command_executor = CommandExecutor(run_manager, task_ledger=task_ledger)
            task = await task_ledger.create_task(
                conversation_id="conv_1",
                title="已有命令任务",
                created_by_run_id="chat-run-1",
            )

            run = await command_executor.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('bound-command')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                metadata={"task_id": task.task_id},
            )
            await command_executor.wait(run["run_id"], timeout=5)

            updated = await task_ledger.get_task("conv_1", task.task_id)
            self.assertEqual(updated.owner_run_id, run["run_id"])
            self.assertEqual(updated.status, TaskStatus.COMPLETED)

    async def test_command_rejects_missing_task_id_before_creating_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            task_ledger = TaskLedger()
            command_executor = CommandExecutor(run_manager, task_ledger=task_ledger)

            with self.assertRaises(Exception):
                await command_executor.start(
                    conversation_id="conv_1",
                    command=f"{sys.executable} -c \"print('no-run')\"",
                    cwd=tmpdir,
                    anchor_node_id="node_1",
                    metadata={"task_id": "task_missing"},
                )

            self.assertEqual(run_manager.list_runs("conv_1"), [])

    async def test_command_tools_pass_explicit_task_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            task_ledger = TaskLedger()
            task_ledger.install_run_finish_listener(run_manager)
            command_executor = CommandExecutor(run_manager, task_ledger=task_ledger)
            task = await task_ledger.create_task(conversation_id="conv_1", title="命令任务")
            config = CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "run_command_initial_wait_seconds": 5,
            })

            result = json.loads(await RunCommandTool(config).execute(
                command=f"{sys.executable} -c \"print('explicit-task')\"",
                task_id=task.task_id,
                _runtime_context={
                    "conversation_id": "conv_1",
                    "anchor_node_id": "node_anchor",
                    "node_id": "node_1",
                    "run_id": "chat-run-1",
                    "command_executor": command_executor,
                },
            ))

            updated = await task_ledger.get_task("conv_1", task.task_id)
            self.assertEqual(updated.owner_run_id, result["run_id"])
            self.assertEqual(updated.status, TaskStatus.COMPLETED)
            self.assertEqual(run_manager.get_run(result["run_id"])["anchor_node_id"], "node_anchor")

            other = await task_ledger.create_task(conversation_id="conv_1", title="后台命令任务")
            started = json.loads(await StartBackgroundCommandTool(config).execute(
                command=f"{sys.executable} -c \"import time; time.sleep(0.1); print('background-task')\"",
                task_id=other.task_id,
                _runtime_context={
                    "conversation_id": "conv_1",
                    "anchor_node_id": "node_anchor",
                    "node_id": "node_1",
                    "run_id": "chat-run-1",
                    "command_executor": command_executor,
                },
            ))
            await command_executor.wait(started["run_id"], timeout=5)
            other_updated = await task_ledger.get_task("conv_1", other.task_id)
            self.assertEqual(other_updated.owner_run_id, started["run_id"])
            self.assertEqual(run_manager.get_run(started["run_id"])["anchor_node_id"], "node_anchor")

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
                parent_run_id=parent.run_id,
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

    async def test_stop_run_tree_recursively_stops_command_children(self):
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
                parent_run_id=parent.run_id,
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


if __name__ == "__main__":
    unittest.main()
