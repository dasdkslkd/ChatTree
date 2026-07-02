import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.terminal import TerminalExecutor
from backend.core.tools.code_tools import CodeToolConfig, RunCommandTool
from backend.core.tools.terminal_tools import ReadTerminalTool, StartTerminalTool, StopTerminalTool, WaitTerminalTool
from backend.api.routes.run_control import stop_run_tree


class TerminalExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_tool_descriptions_keep_sync_and_background_boundaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            })
            run_tool = RunCommandTool(config)
            start_tool = StartTerminalTool(config)

            background_description = run_tool.parameters_schema()["properties"]["background"]["description"]

            self.assertIn("synchronous", run_tool.description)
            self.assertIn("compatibility alias", background_description)
            self.assertIn("start_terminal", background_description)
            self.assertIn("true background terminal", start_tool.description)

    async def test_run_command_background_starts_terminal_run_and_returns_handle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            tool = RunCommandTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            }))

            raw = await tool.execute(
                command=f"{sys.executable} -c \"print('from-tool')\"",
                cwd=".",
                background=True,
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": "parent_run_1",
                    "terminal_executor": terminal,
                },
            )
            payload = json.loads(raw)

            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["kind"], RunKind.TERMINAL.value)
            self.assertIn("terminal_run_id", payload)
            terminal_run_id = payload["terminal_run_id"]
            await terminal.wait(terminal_run_id, timeout=5)

            record = run_manager.get_run(terminal_run_id)
            self.assertEqual(record["parent_run_id"], "parent_run_1")
            self.assertEqual(record["anchor_node_id"], "node_1")

    async def test_start_terminal_tool_uses_workspace_and_returns_handle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            tool = StartTerminalTool(CodeToolConfig.from_dict({
                "workspace_roots": [tmpdir],
                "command_timeout_seconds": 10,
            }))

            raw = await tool.execute(
                command=f"{sys.executable} -c \"print('from-start-terminal')\"",
                cwd=".",
                _runtime_context={
                    "conversation_id": "conv_1",
                    "node_id": "node_1",
                    "run_id": "parent_run_1",
                    "terminal_executor": terminal,
                },
            )
            payload = json.loads(raw)
            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["kind"], RunKind.TERMINAL.value)
            await terminal.wait(payload["terminal_run_id"], timeout=5)
            self.assertEqual(run_manager.get_run(payload["terminal_run_id"])["status"], RunStatus.COMPLETED.value)

    async def test_terminal_control_tools_wait_read_and_stop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            run = await terminal.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"import sys; print('out'); print('err', file=sys.stderr)\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
            )
            runtime_context = {"terminal_executor": terminal}

            waited = json.loads(await WaitTerminalTool().execute(
                terminal_run_id=run["run_id"],
                timeout_seconds=5,
                _runtime_context=runtime_context,
            ))
            self.assertEqual(waited["status"], RunStatus.COMPLETED.value)
            self.assertEqual(waited["exit_code"], 0)
            self.assertIn("out", waited["stdout_tail"])
            self.assertIn("err", waited["stderr_tail"])

            read = json.loads(await ReadTerminalTool().execute(
                terminal_run_id=run["run_id"],
                _runtime_context=runtime_context,
            ))
            self.assertIn("out", read["stdout"])
            self.assertIn("err", read["stderr"])

            stopped = json.loads(await StopTerminalTool().execute(
                terminal_run_id=run["run_id"],
                _runtime_context=runtime_context,
            ))
            self.assertFalse(stopped["stopped"])

    async def test_background_terminal_streams_events_and_enqueues_completion_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            seen_notifications: list[str] = []
            run_manager.synthetic_inputs.set_pending_listener(seen_notifications.append)

            run = await terminal.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('hello-terminal')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                summary="say hello",
            )
            await terminal.wait(run["run_id"], timeout=5)

            record = run_manager.get_run(run["run_id"])
            self.assertEqual(record["kind"], RunKind.TERMINAL.value)
            self.assertEqual(record["status"], RunStatus.COMPLETED.value)

            events = [
                event["payload"]
                for event in run_manager.journal.read_from_index("conv_1", run["run_id"], 0)
            ]
            event_types = [event.get("event_type") or event.get("type") for event in events]
            self.assertIn("terminal_started", event_types)
            self.assertIn("terminal_stdout", event_types)
            self.assertIn("terminal_exited", event_types)
            self.assertTrue(any(
                str(event.get("content") or "").replace("\r\n", "\n") == "hello-terminal\n"
                for event in events
            ))

            pending = run_manager.synthetic_inputs.list_pending("conv_1")
            self.assertEqual(len(pending), 1)
            notification = pending[0]
            self.assertEqual(notification["kind"], "task_notification")
            self.assertEqual(notification["source_run_kind"], RunKind.TERMINAL.value)
            self.assertIn("completed", notification["summary"])
            payload = json.loads(notification["content"])
            self.assertEqual(payload["terminal_run_id"], run["run_id"])
            self.assertEqual(payload["exit_code"], 0)
            self.assertIn("hello-terminal", payload["stdout_tail"])
            self.assertEqual(seen_notifications, ["conv_1"])

    async def test_parented_terminal_defers_notification_until_parent_finishes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            seen_notifications: list[str] = []
            run_manager.synthetic_inputs.set_pending_listener(seen_notifications.append)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )

            run = await terminal.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('deferred-terminal')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                parent_run_id=parent.run_id,
            )
            await terminal.wait(run["run_id"], timeout=5)

            self.assertEqual(run_manager.synthetic_inputs.list_pending("conv_1"), [])
            self.assertEqual(seen_notifications, [])

            await run_manager.finish_run(parent.run_id, RunStatus.COMPLETED)

            pending = run_manager.synthetic_inputs.list_pending("conv_1")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["source_run_id"], run["run_id"])
            self.assertEqual(pending[0]["source_run_kind"], RunKind.TERMINAL.value)
            self.assertEqual(seen_notifications, ["conv_1"])

    async def test_wait_terminal_marks_final_result_observed_and_suppresses_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )
            run = await terminal.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('joined-terminal')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                parent_run_id=parent.run_id,
            )

            waited = json.loads(await WaitTerminalTool().execute(
                terminal_run_id=run["run_id"],
                timeout_seconds=5,
                _runtime_context={
                    "terminal_executor": terminal,
                    "run_id": parent.run_id,
                },
            ))
            await run_manager.finish_run(parent.run_id, RunStatus.COMPLETED)

            self.assertEqual(waited["status"], RunStatus.COMPLETED.value)
            self.assertIn("joined-terminal", waited["stdout"])
            updated = run_manager.get_run(run["run_id"])
            self.assertEqual(updated["metadata"]["terminal_observed_by_run_id"], parent.run_id)
            self.assertEqual(updated["metadata"]["terminal_observed_via"], "wait_terminal")
            self.assertEqual(run_manager.synthetic_inputs.list_pending("conv_1"), [])

    async def test_read_terminal_marks_final_model_read_observed_and_suppresses_notification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )
            run = await terminal.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"print('read-terminal')\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                parent_run_id=parent.run_id,
            )
            await terminal.wait(run["run_id"], timeout=5)

            read = json.loads(await ReadTerminalTool().execute(
                terminal_run_id=run["run_id"],
                _runtime_context={
                    "terminal_executor": terminal,
                    "run_id": parent.run_id,
                },
            ))
            await run_manager.finish_run(parent.run_id, RunStatus.COMPLETED)

            self.assertEqual(read["status"], RunStatus.COMPLETED.value)
            self.assertIn("read-terminal", read["stdout"])
            updated = run_manager.get_run(run["run_id"])
            self.assertEqual(updated["metadata"]["terminal_observed_by_run_id"], parent.run_id)
            self.assertEqual(updated["metadata"]["terminal_observed_via"], "read_terminal")
            self.assertEqual(run_manager.synthetic_inputs.list_pending("conv_1"), [])

    async def test_terminal_start_exception_records_actionable_error_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)

            with patch(
                "backend.core.terminal.asyncio.create_subprocess_shell",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                run = await terminal.start(
                    conversation_id="conv_1",
                    command="failing command",
                    cwd=tmpdir,
                    anchor_node_id="node_1",
                )
                await terminal.wait(run["run_id"], timeout=5)

            snapshot = terminal.snapshot(run["run_id"])
            self.assertEqual(snapshot["status"], RunStatus.FAILED.value)
            self.assertIn("RuntimeError", snapshot["error"])
            self.assertIn("boom", snapshot["error"])
            self.assertIn("failing command", snapshot["error"])
            self.assertIn(str(Path(tmpdir).resolve()), snapshot["error"])

    async def test_terminal_falls_back_when_asyncio_subprocess_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)

            with patch(
                "backend.core.terminal.asyncio.create_subprocess_shell",
                new=AsyncMock(side_effect=NotImplementedError()),
            ):
                run = await terminal.start(
                    conversation_id="conv_1",
                    command=f"{sys.executable} -c \"print('fallback-terminal')\"",
                    cwd=tmpdir,
                    anchor_node_id="node_1",
                )
                await terminal.wait(run["run_id"], timeout=5)

            snapshot = terminal.snapshot(run["run_id"])
            self.assertEqual(snapshot["status"], RunStatus.COMPLETED.value)
            self.assertEqual(snapshot["exit_code"], 0)
            self.assertIn("fallback-terminal", snapshot["stdout"])
            self.assertIsNone(snapshot["error"])

    async def test_stop_cancels_background_terminal_process_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / "marker.txt"
            command = (
                f"{sys.executable} -c "
                "\"import pathlib, time; pathlib.Path('marker.txt').write_text('started', encoding='utf-8'); time.sleep(30)\""
            )
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)

            run = await terminal.start(
                conversation_id="conv_1",
                command=command,
                cwd=tmpdir,
                anchor_node_id="node_1",
            )
            for _ in range(50):
                if marker.exists():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(marker.exists())

            await terminal.stop(run["run_id"])
            await terminal.wait(run["run_id"], timeout=5)

            record = run_manager.get_run(run["run_id"])
            self.assertEqual(record["status"], RunStatus.CANCELLED.value)
            events = [
                event["payload"]
                for event in run_manager.journal.read_from_index("conv_1", run["run_id"], 0)
            ]
            self.assertTrue(any(event.get("event_type") == "terminal_stopped" for event in events))

    async def test_stop_run_tree_recursively_stops_terminal_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_manager = RunManager()
            terminal = TerminalExecutor(run_manager)
            parent = await run_manager.create_run(
                conversation_id="conv_1",
                kind=RunKind.CHAT,
                anchor_node_id="node_1",
                target_node_id="node_2",
            )
            child = await terminal.start(
                conversation_id="conv_1",
                command=f"{sys.executable} -c \"import time; time.sleep(30)\"",
                cwd=tmpdir,
                anchor_node_id="node_1",
                parent_run_id=parent.run_id,
            )

            stopped = await stop_run_tree(
                parent.run_id,
                run_manager=run_manager,
                terminal_executor=terminal,
            )
            await terminal.wait(child["run_id"], timeout=5)

            self.assertIn(parent.run_id, stopped)
            self.assertIn(child["run_id"], stopped)
            self.assertEqual(run_manager.get_run(child["run_id"])["status"], RunStatus.CANCELLED.value)


if __name__ == "__main__":
    unittest.main()
