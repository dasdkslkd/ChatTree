import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.api.routes.run_control import stop_run_tree
from backend.core.command_runtime import CommandExecutor, CommandExecutorClosingError
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.runs.repository import MemoryRunRepository
from backend.core.tasks import ActiveTaskService, TaskContextDisabledError
from backend.core.tools.code_tools import CodeToolConfig, RunCommandTool
from backend.core.tools.tool_manager import ToolManager


class CommandExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_tool_descriptions_expose_shell_profile_and_background_boundaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            })
            run_tool = RunCommandTool(config)

            self.assertIn("auto-backgrounds", run_tool.description)
            self.assertIn("active shell", run_tool.description)
            self.assertNotIn("start_terminal", run_tool.description)

    @unittest.skip("legacy command control tools removed; shell auto-background is the only model-visible command API")
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

    async def test_shell_short_managed_command_truncates_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "shell_initial_wait_seconds": 5,
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

    async def test_shell_long_managed_command_auto_backgrounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
                "shell_initial_wait_seconds": 0.1,
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
            self.assertTrue((run.get("metadata") or {}).get("shell_auto_backgrounded"))

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
        self.assertTrue({"shell", "shell", "shell", "shell", "shell"} <= visible)
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

    @unittest.skip("legacy command control tools removed")
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

    async def test_command_binds_only_the_explicit_numbered_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
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

    async def test_shell_foreground_result_exposes_public_task_outcome(self):
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
                "shell_initial_wait_seconds": 5,
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

    @unittest.skip("legacy command control tools removed")
    async def test_shell_marks_stopped_result_observed(self):
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

    async def test_shell_foreground_marks_stopped_result_observed(self):
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
                "shell_initial_wait_seconds": 5,
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
            ("run_stopped_foreground", "run_parent", "shell"),
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

    async def test_stop_run_tree_continues_command_stop_after_event_writer_failure(self):
        class FailOneWriteRepository(MemoryRunRepository):
            def __init__(self):
                super().__init__()
                self.fail_run_id = None
                self.failed = False

            def append_indexed_events(self, run_id, events):
                if run_id == self.fail_run_id and not self.failed:
                    self.failed = True
                    raise OSError("transient event write failure")
                return super().append_events(
                    run_id,
                    [dict(event["payload"]) for event in events],
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = FailOneWriteRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            started = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "import time; time.sleep(30)"',
                cwd=tmpdir,
            )
            run_id = started["run_id"]
            async with asyncio.timeout(5):
                while not any(
                    event.get("event_type") == "command_started"
                    for event in run_manager.read_events(run_id, 0)
                ):
                    await asyncio.sleep(0.01)
            await run_manager.flush_events()
            repository.fail_run_id = run_id
            await run_manager.append_event(
                run_id,
                {"status": "content", "content": "not persisted"},
            )
            with self.assertRaisesRegex(RuntimeError, run_id):
                await run_manager.flush_run_events(run_id)

            stopped = await stop_run_tree(
                run_id,
                run_manager=run_manager,
                command_executor=command_executor,
            )
            await command_executor.wait(run_id, timeout=5)

            self.assertEqual(stopped, [run_id])
            self.assertEqual(
                run_manager.get_run(run_id)["status"],
                RunStatus.CANCELLED.value,
            )
            persisted = repository.get_run(run_id)
            self.assertEqual(persisted["status"], RunStatus.CANCELLED.value)
            self.assertEqual(
                persisted["metadata"]["event_persistence_error"],
                "run event persistence failed",
            )
            await run_manager.close()

    async def test_command_started_write_failure_kills_asyncio_process_immediately(self):
        class FailCommandStartedRepository(MemoryRunRepository):
            def append_indexed_events(self, run_id, events):
                if any(
                    event["payload"].get("event_type") == "command_started"
                    for event in events
                ):
                    raise OSError("command_started persistence failed")
                return super().append_events(
                    run_id,
                    [dict(event["payload"]) for event in events],
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = FailCommandStartedRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            spawned = []
            create_subprocess_exec = asyncio.create_subprocess_exec

            async def recording_create_subprocess_exec(*args, **kwargs):
                process = await create_subprocess_exec(*args, **kwargs)
                spawned.append(process)
                return process

            with patch(
                "backend.core.command_runtime.asyncio.create_subprocess_exec",
                new=recording_create_subprocess_exec,
            ):
                started = await command_executor.start(
                    conversation_id="conv_1",
                    command=f'{sys.executable} -c "import time; time.sleep(30)"',
                    cwd=tmpdir,
                )
                await command_executor.wait(started["run_id"], timeout=5)

            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].returncode)
            self.assertEqual(
                run_manager.get_run(started["run_id"])["status"],
                RunStatus.FAILED.value,
            )
            self.assertEqual(
                repository.get_run(started["run_id"])["metadata"]["error"],
                "run event persistence failed",
            )
            await run_manager.close()

    async def test_command_started_write_failure_kills_popen_fallback_immediately(self):
        class FailCommandStartedRepository(MemoryRunRepository):
            def append_indexed_events(self, run_id, events):
                if any(
                    event["payload"].get("event_type") == "command_started"
                    for event in events
                ):
                    raise OSError("command_started persistence failed")
                return super().append_events(
                    run_id,
                    [dict(event["payload"]) for event in events],
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = FailCommandStartedRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            spawned = []

            from backend.core import command_runtime

            popen = command_runtime.subprocess.Popen

            def recording_popen(*args, **kwargs):
                process = popen(*args, **kwargs)
                spawned.append(process)
                return process

            with (
                patch(
                    "backend.core.command_runtime.asyncio.create_subprocess_exec",
                    new=AsyncMock(side_effect=NotImplementedError),
                ),
                patch(
                    "backend.core.command_runtime.subprocess.Popen",
                    new=recording_popen,
                ),
            ):
                started = await command_executor.start(
                    conversation_id="conv_1",
                    command=f'{sys.executable} -c "import time; time.sleep(30)"',
                    cwd=tmpdir,
                )
                await command_executor.wait(started["run_id"], timeout=5)

            # Windows taskkill also uses the patched subprocess.Popen.
            self.assertGreaterEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].returncode)
            self.assertEqual(
                run_manager.get_run(started["run_id"])["status"],
                RunStatus.FAILED.value,
            )
            await run_manager.close()

    async def test_popen_spawn_cancellation_cannot_lose_process_handle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            spawned = []
            spawned_event = threading.Event()
            release_popen = threading.Event()

            from backend.core import command_runtime

            popen = command_runtime.subprocess.Popen

            def blocking_popen(*args, **kwargs):
                process = popen(*args, **kwargs)
                spawned.append(process)
                spawned_event.set()
                release_popen.wait(timeout=5)
                return process

            with (
                patch(
                    "backend.core.command_runtime.asyncio.create_subprocess_exec",
                    new=AsyncMock(side_effect=NotImplementedError),
                ),
                patch(
                    "backend.core.command_runtime.subprocess.Popen",
                    new=blocking_popen,
                ),
            ):
                started = await command_executor.start(
                    conversation_id="conv_1",
                    command=f'{sys.executable} -c "import time; time.sleep(30)"',
                    cwd=tmpdir,
                )
                run_id = started["run_id"]
                producer = command_executor._tasks[run_id]
                loop = asyncio.get_running_loop()

                def request_cancel_during_spawn():
                    self.assertTrue(spawned_event.wait(timeout=5))
                    loop.call_soon_threadsafe(producer.cancel)
                    release_popen.set()

                canceller = threading.Thread(
                    target=request_cancel_during_spawn,
                    daemon=True,
                )
                canceller.start()
                await asyncio.gather(producer, return_exceptions=True)
                canceller.join(timeout=5)

            self.assertGreaterEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].returncode)
            self.assertEqual(
                run_manager.repository.get_run(run_id)["status"],
                RunStatus.CANCELLED.value,
            )
            self.assertEqual(await command_executor.close(timeout=1), ())
            await run_manager.close()

    async def test_stop_run_tree_kills_command_when_all_stop_writes_fail(self):
        class PersistentStopFailureRepository(MemoryRunRepository):
            def __init__(self):
                super().__init__()
                self.fail_run_id = None
                self.event_failed = False
                self.stop_requested = 0

            def append_indexed_events(self, run_id, events):
                if run_id == self.fail_run_id and not self.event_failed:
                    self.event_failed = True
                    raise OSError("event persistence unavailable")
                return super().append_events(
                    run_id,
                    [dict(event["payload"]) for event in events],
                )

            def merge_metadata(self, run_id, patch):
                if run_id == self.fail_run_id and "event_persistence_error" in patch:
                    raise OSError("metadata persistence unavailable")
                return super().merge_metadata(run_id, patch)

            def request_stop(self, run_id):
                if run_id == self.fail_run_id:
                    self.stop_requested += 1
                    raise OSError("stop persistence unavailable")
                return super().request_stop(run_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = PersistentStopFailureRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            started = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "import time; time.sleep(30)"',
                cwd=tmpdir,
            )
            run_id = started["run_id"]
            async with asyncio.timeout(5):
                while not any(
                    event.get("event_type") == "command_started"
                    for event in run_manager.read_events(run_id, 0)
                ):
                    await asyncio.sleep(0.01)
            await run_manager.flush_run_events(run_id)
            process = command_executor._processes[run_id]
            repository.fail_run_id = run_id
            await run_manager.append_event(
                run_id,
                {"status": "content", "content": "not persisted"},
            )
            with self.assertRaisesRegex(RuntimeError, run_id):
                await run_manager.flush_run_events(run_id)

            with self.assertRaisesRegex(OSError, "metadata persistence unavailable"):
                await stop_run_tree(
                    run_id,
                    run_manager=run_manager,
                    command_executor=command_executor,
                )
            await command_executor.wait(run_id, timeout=5)

            self.assertIsNotNone(process.returncode)
            self.assertGreaterEqual(repository.stop_requested, 1)
            self.assertEqual(
                run_manager.get_run(run_id)["status"],
                RunStatus.CANCELLED.value,
            )
            await run_manager.close()

    async def test_close_kills_asyncio_process_and_terminalizes_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            started = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "import time; time.sleep(30)"',
                cwd=tmpdir,
            )
            run_id = started["run_id"]
            async with asyncio.timeout(5):
                while run_id not in command_executor._processes:
                    await asyncio.sleep(0.01)
            process = command_executor._processes[run_id]

            self.assertEqual(await command_executor.close(timeout=5), ())

            self.assertIsNotNone(process.returncode)
            self.assertEqual(
                run_manager.get_run(run_id)["status"],
                RunStatus.CANCELLED.value,
            )
            with self.assertRaises(CommandExecutorClosingError):
                await command_executor.start(
                    conversation_id="conv_1",
                    command="echo rejected",
                    cwd=tmpdir,
                )
            await run_manager.close()

    async def test_close_kills_popen_fallback_and_terminalizes_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            command_executor = CommandExecutor(run_manager)
            with patch(
                "backend.core.command_runtime.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=NotImplementedError),
            ):
                started = await command_executor.start(
                    conversation_id="conv_1",
                    command=f'{sys.executable} -c "import time; time.sleep(30)"',
                    cwd=tmpdir,
                )
                run_id = started["run_id"]
                async with asyncio.timeout(5):
                    while run_id not in command_executor._processes:
                        await asyncio.sleep(0.01)
                process = command_executor._processes[run_id]

                self.assertEqual(await command_executor.close(timeout=5), ())

            self.assertIsNotNone(process.returncode)
            self.assertEqual(
                run_manager.get_run(run_id)["status"],
                RunStatus.CANCELLED.value,
            )
            await run_manager.close()

    async def test_close_kills_process_when_stop_persistence_fails(self):
        class FailingStopRepository(MemoryRunRepository):
            def __init__(self):
                super().__init__()
                self.fail_run_id = None

            def request_stop(self, run_id):
                if run_id == self.fail_run_id:
                    raise OSError("stop persistence unavailable")
                return super().request_stop(run_id)

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = FailingStopRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            started = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "import time; time.sleep(30)"',
                cwd=tmpdir,
            )
            run_id = started["run_id"]
            async with asyncio.timeout(5):
                while run_id not in command_executor._processes:
                    await asyncio.sleep(0.01)
            process = command_executor._processes[run_id]
            repository.fail_run_id = run_id

            self.assertEqual(await command_executor.close(timeout=5), ())

            self.assertIsNotNone(process.returncode)
            self.assertEqual(
                run_manager.get_run(run_id)["status"],
                RunStatus.CANCELLED.value,
            )
            await run_manager.close()

    async def test_close_reports_unresolved_when_terminal_persistence_fails(self):
        class FailingFinishRepository(MemoryRunRepository):
            def __init__(self):
                super().__init__()
                self.fail_run_id = None

            def finish_run(self, run_id, status, error=None):
                if run_id == self.fail_run_id:
                    raise OSError("finish persistence unavailable")
                return super().finish_run(run_id, status, error)

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = FailingFinishRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            started = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "import time; time.sleep(30)"',
                cwd=tmpdir,
            )
            run_id = started["run_id"]
            async with asyncio.timeout(5):
                while run_id not in command_executor._processes:
                    await asyncio.sleep(0.01)
            process = command_executor._processes[run_id]
            producer = command_executor._tasks[run_id]
            repository.fail_run_id = run_id

            self.assertEqual(
                await command_executor.close(timeout=0.5),
                (run_id,),
            )

            await asyncio.wait_for(process.wait(), timeout=5)
            self.assertIsNotNone(process.returncode)
            self.assertTrue(producer.done())
            self.assertIsInstance(producer.exception(), OSError)
            self.assertNotIn(run_id, command_executor._processes)
            self.assertNotIn(run_id, command_executor._tasks)
            self.assertEqual(
                repository.get_run(run_id)["status"],
                RunStatus.STOPPING.value,
            )
            repository.fail_run_id = None
            await run_manager.finish_run(run_id, RunStatus.INTERRUPTED)
            await run_manager.close()

    async def test_close_remembers_terminal_failure_after_command_task_is_gone(self):
        class FailingFinishRepository(MemoryRunRepository):
            def __init__(self):
                super().__init__()
                self.fail_run_id = None

            def finish_run(self, run_id, status, error=None):
                if run_id == self.fail_run_id:
                    raise OSError("finish persistence unavailable")
                return super().finish_run(run_id, status, error)

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = FailingFinishRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            started = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "import time; time.sleep(0.2)"',
                cwd=tmpdir,
            )
            run_id = started["run_id"]
            repository.fail_run_id = run_id
            producer = command_executor._tasks[run_id]
            await asyncio.gather(producer, return_exceptions=True)
            await asyncio.sleep(0)

            self.assertNotIn(run_id, command_executor._processes)
            self.assertNotIn(run_id, command_executor._tasks)
            self.assertIn(run_id, command_executor._undrained_run_ids)
            self.assertEqual(
                await command_executor.close(timeout=0.2),
                (run_id,),
            )
            repository.fail_run_id = None
            await run_manager.finish_run(run_id, RunStatus.INTERRUPTED)
            await run_manager.close()

    async def test_close_retries_cleanup_when_initial_process_kill_fails(self):
        class FailCommandStartedRepository(MemoryRunRepository):
            def append_indexed_events(self, run_id, events):
                if any(
                    event["payload"].get("event_type") == "command_started"
                    for event in events
                ):
                    raise OSError("command_started persistence failed")
                return super().append_events(
                    run_id,
                    [dict(event["payload"]) for event in events],
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = FailCommandStartedRepository()
            run_manager = RunManager(repository=repository)
            command_executor = CommandExecutor(run_manager)
            kill_process_tree = command_executor._kill_process_tree
            kill_calls = 0

            async def fail_first_kill(process):
                nonlocal kill_calls
                kill_calls += 1
                if kill_calls == 1:
                    raise OSError("initial physical cleanup unavailable")
                await kill_process_tree(process)

            command_executor._kill_process_tree = fail_first_kill
            started = await command_executor.start(
                conversation_id="conv_1",
                command=f'{sys.executable} -c "import time; time.sleep(30)"',
                cwd=tmpdir,
            )
            run_id = started["run_id"]
            producer = command_executor._tasks[run_id]
            async with asyncio.timeout(5):
                while run_id not in command_executor._processes:
                    await asyncio.sleep(0.01)
            process = command_executor._processes[run_id]
            await asyncio.gather(producer, return_exceptions=True)

            self.assertIsNone(process.returncode)
            self.assertIn(run_id, command_executor._processes)
            self.assertIn(run_id, command_executor._undrained_run_ids)
            self.assertEqual(await command_executor.close(timeout=5), ())
            self.assertIsNotNone(process.returncode)
            self.assertGreaterEqual(kill_calls, 2)
            await run_manager.close()

    @unittest.skip("legacy command control tools removed; shell auto-background is the only model-visible command API")
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
