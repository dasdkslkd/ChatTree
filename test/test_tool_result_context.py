import asyncio
import json
import time

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.tool_result_format import (
    apply_round_tool_result_budget,
    build_model_visible_tool_result,
)
from backend.core.config.config import cfg
from backend.core.config.types import Message, Role
from backend.core.tools.code import python_fallback as code_python_fallback
from backend.core.tools.code import ripgrep as code_ripgrep
from backend.core.tools.code import CodeToolConfig, ListFilesTool
from backend.core.tools.security.capabilities import ToolCapability


class FakeChatRepository:
    def __init__(self):
        self.saved = []
        self.calls = []
        self.persistence = None

    def tool_call_exists(self, conversation_id, tool_call_id):
        return any(
            call.get("tool_call_id") == tool_call_id
            for call in self.calls
        )

    def add_tool_call(self, *args, **kwargs):
        self.calls.append(kwargs)
        return kwargs.get("tool_call_id")

    def add_tool_result(self, *args, **kwargs):
        self.saved.append(kwargs)
        return f"result-{len(self.saved)}"


class FakeToolManager:
    def __init__(self, result="tool output"):
        self.result = result
        self.calls = []
        self.runtime_contexts = []

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        self.calls.append((name, arguments))
        self.runtime_contexts.append(dict(runtime_context or {}))
        sink = runtime_context.get("tool_event_sink") if isinstance(runtime_context, dict) else None
        if callable(sink):
            sink({
                "event_type": "tool_progress",
                "status": "running",
                "progress": {"phase": "fake-tool-manager"},
            })
        return self.result

    def capabilities_for(self, name, workspace=None):
        return {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}


class DelayedToolManager:
    def __init__(self, delay=0.12):
        self.delay = delay
        self.calls = []
        self.started = {}
        self.finished = {}

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        self.calls.append((name, arguments))
        self.started[name] = time.perf_counter()
        await asyncio.sleep(self.delay)
        self.finished[name] = time.perf_counter()
        return json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)

    def capabilities_for(self, name, workspace=None):
        if name in {"write", "edit", "patch", "shell"}:
            return {ToolCapability.MUTATES_WORKSPACE}
        return {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE}


def make_manager(tool_manager):
    manager = ChatManager.__new__(ChatManager)
    manager.tool_manager = tool_manager
    manager.tool_orchestrator = None
    manager.chat_repository = FakeChatRepository()
    return manager


def test_command_tool_result_is_model_readable_text(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 20})
    raw_result = json.dumps(
        {
            "command": "pytest -q",
            "cwd": "D:\\Workspace\\ChatTree",
            "exit_code": 1,
            "stdout": "first line\nsecond line",
            "stderr": "boom",
            "timed_out": False,
        },
        ensure_ascii=False,
    )
    manager = make_manager(FakeToolManager())

    content = build_model_visible_tool_result(
        manager.chat_repository,
        raw_result=raw_result,
        name="shell",
        conversation_id="conv-1",
        node_id="node-1",
        tool_call_id="call-1",
    )

    assert not content.lstrip().startswith("{")
    assert "Command: pytest -q" in content
    assert "Cwd: D:\\Workspace\\ChatTree" in content
    assert "Exit code: 1" in content
    assert "Timed out: false" in content
    assert "Stdout:" in content
    assert "first line" in content
    assert "Stderr:" in content
    assert "boom" in content
    assert "tool_result_id: result-1" in content
    assert 'read({"source":"tool_result","tool_result_id":"result-1"' in content
    assert manager.chat_repository.saved[0]["output"] == raw_result
    assert manager.chat_repository.calls[0]["tool_call_id"] == "call-1"
    assert manager.chat_repository.saved[0]["tool_call_id"] == "call-1"


def test_model_visible_tool_result_initial_persistence_binds_tool_call_id(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 2000})
    manager = make_manager(FakeToolManager())

    build_model_visible_tool_result(
        manager.chat_repository,
        raw_result="bound output",
        name="web_search",
        conversation_id="conv-1",
        node_id="node-1",
        tool_call_id="call-bound",
    )

    assert manager.chat_repository.calls == [{
        "tool_call_id": "call-bound",
        "name": "web_search",
        "arguments": None,
        "status": "running",
    }]
    assert manager.chat_repository.saved == [{
        "conversation_id": "conv-1",
        "node_id": "node-1",
        "tool_call_id": "call-bound",
        "output": "bound output",
        "metadata": {"tool_name": "web_search"},
    }]


def test_non_command_tool_result_keeps_preview_with_metadata(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 5})
    manager = make_manager(FakeToolManager())

    content = build_model_visible_tool_result(
        manager.chat_repository,
        raw_result="abcdefghi",
        name="web_search",
        conversation_id="conv-1",
        node_id="node-1",
        tool_call_id="call-1",
    )

    payload = json.loads(content)
    assert payload == {
        "tool_result_id": "result-1",
        "total_chars": 9,
        "truncated": True,
        "preview": "abcde",
        "read_more": 'read({"source":"tool_result","tool_result_id":"result-1","offset":5})',
    }


def test_execute_tool_calls_preserves_raw_and_model_visible_content(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 5})
    manager = make_manager(FakeToolManager(result="abcdefghi"))
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "web_search", "arguments": "{\"query\":\"ChatTree\"}"},
        }
    ]

    results = asyncio.run(
        manager._execute_tool_calls(tool_calls, node_id="node-1", conversation_id="conv-1")
    )

    assert len(results) == 1
    tool_msg = results[0]
    assert tool_msg["role"] == Role.TOOL
    assert tool_msg["tool_call_id"] == "call-1"
    assert tool_msg["raw_content"] == "abcdefghi"
    assert tool_msg["content"] == tool_msg["model_visible_content"]
    assert tool_msg["tool_result_id"] == "result-1"
    assert json.loads(tool_msg["model_visible_content"])["preview"] == "abcde"


def test_execute_tool_calls_runs_read_only_tools_concurrently(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 2000})
    tool_manager = DelayedToolManager(delay=0.15)
    manager = make_manager(tool_manager)
    tool_calls = [
        {
            "id": "call-read",
            "type": "function",
            "function": {"name": "read", "arguments": "{\"path\":\"a.txt\"}"},
        },
        {
            "id": "call-search",
            "type": "function",
            "function": {"name": "grep", "arguments": "{\"query\":\"needle\"}"},
        },
    ]

    started = time.perf_counter()
    results = asyncio.run(
        manager._execute_tool_calls(tool_calls, node_id="node-1", conversation_id="conv-1")
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.28
    assert [message["tool_call_id"] for message in results] == ["call-read", "call-search"]
    assert set(tool_manager.started) == {"read", "grep"}
    assert abs(tool_manager.started["read"] - tool_manager.started["grep"]) < 0.08


def test_execute_tool_calls_streams_observation_events_before_batch_finishes(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 2000})
    tool_manager = DelayedToolManager(delay=0.15)
    manager = make_manager(tool_manager)
    events = []
    tool_calls = [
        {
            "id": "call-read",
            "type": "function",
            "function": {"name": "read", "arguments": "{\"path\":\"a.txt\"}"},
        },
        {
            "id": "call-search",
            "type": "function",
            "function": {"name": "grep", "arguments": "{\"pattern\":\"needle\"}"},
        },
    ]

    async def emit_event(event):
        events.append((time.perf_counter(), event))

    async def run_case():
        task = asyncio.create_task(
            manager._execute_tool_calls(
                tool_calls,
                node_id="node-1",
                conversation_id="conv-1",
                emit_event=emit_event,
            )
        )
        await asyncio.sleep(0.04)
        event_types = [event["event_type"] for _, event in events]
        assert "tool_call_start" in event_types
        assert "tool_progress" in event_types
        assert "tool_result" not in event_types
        results = await task
        return results

    results = asyncio.run(run_case())

    assert [message["tool_call_id"] for message in results] == ["call-read", "call-search"]
    final_events = [event for _, event in events if event["event_type"] == "tool_result"]
    assert {event["tool_call"]["tool_call_id"] for event in final_events} == {"call-read", "call-search"}


def test_execute_tool_calls_flushes_tool_sink_events_before_final_result(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 2000})
    manager = make_manager(FakeToolManager())
    events = []
    tool_calls = [
        {
            "id": "call-read",
            "type": "function",
            "function": {"name": "read", "arguments": "{\"path\":\"a.txt\"}"},
        },
    ]

    async def emit_event(event):
        events.append(event)

    asyncio.run(
        manager._execute_tool_calls(
            tool_calls,
            node_id="node-1",
            conversation_id="conv-1",
            emit_event=emit_event,
        )
    )

    event_types = [event["event_type"] for event in events]
    sink_progress_index = next(
        index
        for index, event in enumerate(events)
        if event["event_type"] == "tool_progress"
        and (event["tool_call"].get("progress") or {}).get("phase") == "fake-tool-manager"
    )
    final_index = event_types.index("tool_result")
    assert sink_progress_index < final_index


def test_execute_tool_calls_keeps_mutating_tools_as_order_barriers(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 2000})
    tool_manager = DelayedToolManager(delay=0.08)
    manager = make_manager(tool_manager)
    tool_calls = [
        {
            "id": "call-read-before",
            "type": "function",
            "function": {"name": "read", "arguments": "{\"path\":\"before.txt\"}"},
        },
        {
            "id": "call-write",
            "type": "function",
            "function": {"name": "write", "arguments": "{\"path\":\"out.txt\",\"content\":\"x\"}"},
        },
        {
            "id": "call-read-after",
            "type": "function",
            "function": {"name": "grep", "arguments": "{\"query\":\"after\"}"},
        },
    ]

    results = asyncio.run(
        manager._execute_tool_calls(tool_calls, node_id="node-1", conversation_id="conv-1")
    )

    assert [message["tool_call_id"] for message in results] == [
        "call-read-before",
        "call-write",
        "call-read-after",
    ]
    assert tool_manager.finished["read"] <= tool_manager.started["write"]
    assert tool_manager.finished["write"] <= tool_manager.started["grep"]


def test_glob_tool_sync_work_runs_off_event_loop(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    def slow_glob_python(**kwargs):
        time.sleep(0.2)
        return {
            "root": ".",
            "files": ["a.txt"],
            "count": 1,
            "total": 1,
            "total_known": True,
            "observed_count": 1,
            "truncated": False,
            "next_offset": None,
            "engine": "python",
            "sort": kwargs["sort"],
            "scanned_entries": 1,
        }

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda config: None)
    monkeypatch.setattr(code_python_fallback, "_glob_files_python", slow_glob_python)
    tool = ListFilesTool(CodeToolConfig.from_dict({
        "workspace_roots": [str(tmp_path)],
        "command_timeout_seconds": 1,
    }))

    async def run_case():
        task = asyncio.create_task(tool.execute(path="."))
        await asyncio.sleep(0.03)
        assert not task.done()
        return await task

    payload = json.loads(asyncio.run(run_case()))

    assert payload["files"][0] == "a.txt"


def test_round_result_budget_shortens_longest_model_visible_result(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 20, "max_round_result_length": 100})
    manager = make_manager(FakeToolManager())
    messages = [
        Message(
            {
                "id": "tool-1",
                "role": Role.TOOL,
                "content": "short result",
                "model_visible_content": "short result",
                "tool_result_id": "result-short",
                "name": "tool_a",
                "tool_call_id": "call-short",
                "timestamp": 1,
            }
        ),
        Message(
            {
                "id": "tool-2",
                "role": Role.TOOL,
                "content": "L" * 120,
                "model_visible_content": "L" * 120,
                "tool_result_id": "result-long",
                "name": "tool_b",
                "tool_call_id": "call-long",
                "timestamp": 1,
            }
        ),
    ]

    budgeted = apply_round_tool_result_budget(messages)

    assert budgeted[0]["content"] == "short result"
    assert len(budgeted[1]["content"]) < 120
    assert "result-long" in budgeted[1]["content"]
    assert 'read({"source":"tool_result","tool_result_id":"result-long","offset":0})' in budgeted[1]["content"]
    assert budgeted[1]["raw_content"] == "L" * 120
