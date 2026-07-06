import asyncio
import json
import time

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.config.config import cfg
from backend.core.config.types import Message, Role
from backend.core.tools import code_tools
from backend.core.tools.code_tools import CodeToolConfig, ListFilesTool


class FakeToolResultStore:
    def __init__(self):
        self.saved = []

    def save_result(self, **kwargs):
        self.saved.append(kwargs)
        return {"id": f"result-{len(self.saved)}"}


class FakeToolManager:
    def __init__(self, result="tool output"):
        self.result = result
        self.tool_result_store = FakeToolResultStore()
        self.calls = []

    async def execute_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


class DelayedToolManager:
    def __init__(self, delay=0.12):
        self.delay = delay
        self.tool_result_store = FakeToolResultStore()
        self.calls = []
        self.started = {}
        self.finished = {}

    async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
        self.calls.append((name, arguments))
        self.started[name] = time.perf_counter()
        await asyncio.sleep(self.delay)
        self.finished[name] = time.perf_counter()
        return json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)


def make_manager(tool_manager):
    manager = ChatManager.__new__(ChatManager)
    manager.tool_manager = tool_manager
    manager.tool_orchestrator = None
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

    content = manager._build_model_visible_tool_result(
        raw_result=raw_result,
        name="run_command",
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
    assert 'read_tool_result({"tool_result_id":"result-1"' in content
    assert manager.tool_manager.tool_result_store.saved[0]["content"] == raw_result


def test_non_command_tool_result_keeps_preview_with_metadata(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 5})
    manager = make_manager(FakeToolManager())

    content = manager._build_model_visible_tool_result(
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
        "read_more": 'read_tool_result({"tool_result_id":"result-1","offset":5})',
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
            "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"},
        },
        {
            "id": "call-search",
            "type": "function",
            "function": {"name": "search_files", "arguments": "{\"query\":\"needle\"}"},
        },
    ]

    started = time.perf_counter()
    results = asyncio.run(
        manager._execute_tool_calls(tool_calls, node_id="node-1", conversation_id="conv-1")
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.28
    assert [message["tool_call_id"] for message in results] == ["call-read", "call-search"]
    assert set(tool_manager.started) == {"read_file", "search_files"}
    assert abs(tool_manager.started["read_file"] - tool_manager.started["search_files"]) < 0.08


def test_execute_tool_calls_keeps_mutating_tools_as_order_barriers(monkeypatch):
    monkeypatch.setitem(cfg.data, "tools", {"max_result_length": 2000})
    tool_manager = DelayedToolManager(delay=0.08)
    manager = make_manager(tool_manager)
    tool_calls = [
        {
            "id": "call-read-before",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{\"path\":\"before.txt\"}"},
        },
        {
            "id": "call-write",
            "type": "function",
            "function": {"name": "write_file", "arguments": "{\"path\":\"out.txt\",\"content\":\"x\"}"},
        },
        {
            "id": "call-read-after",
            "type": "function",
            "function": {"name": "search_files", "arguments": "{\"query\":\"after\"}"},
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
    assert tool_manager.finished["read_file"] <= tool_manager.started["write_file"]
    assert tool_manager.finished["write_file"] <= tool_manager.started["search_files"]


def test_list_files_tool_sync_work_runs_off_event_loop(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    def slow_list_files_python(**kwargs):
        time.sleep(0.2)
        return ([{"path": "a.txt", "type": "file", "size": 5}], False, 1)

    monkeypatch.setattr(code_tools, "_list_files_python", slow_list_files_python)
    tool = ListFilesTool(CodeToolConfig.from_dict({
        "workspace_roots": [str(tmp_path)],
        "command_timeout_seconds": 1,
    }))

    async def run_case():
        task = asyncio.create_task(tool.execute(path=".", max_depth=1))
        await asyncio.sleep(0.03)
        assert not task.done()
        return await task

    payload = json.loads(asyncio.run(run_case()))

    assert payload["items"][0]["path"] == "a.txt"


def test_prepare_messages_uses_model_visible_content_and_drops_orphan_tool_messages():
    conversation = Conversation(title="tool history")
    conversation.initialize_with_system_message("system prompt")
    user_msg = Message(
        {
            "id": "user-1",
            "role": Role.USER,
            "content": "run",
            "timestamp": 1,
        }
    )
    node = NodeManager.create_node(user_msg, parent_id=conversation.current_node_id, model_id="model")
    assistant_tool = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }
        ],
    }
    tool_msg = Message(
        {
            "id": "tool-1",
            "role": Role.TOOL,
            "content": "raw output that UI keeps",
            "model_visible_content": "Command: pytest -q\nExit code: 0",
            "raw_content": "raw output that UI keeps",
            "name": "run_command",
            "tool_call_id": "call-1",
            "timestamp": 2,
        }
    )
    orphan_tool_msg = Message(
        {
            "id": "tool-orphan",
            "role": Role.TOOL,
            "content": "must not reach provider",
            "name": "run_command",
            "tool_call_id": "missing-call",
            "timestamp": 3,
        }
    )
    assistant_final = Message(
        {
            "id": "assistant-1",
            "role": Role.ASSISTANT,
            "content": "done",
            "timestamp": 4,
            "tool_calls": assistant_tool["tool_calls"],
            "tool_results": [tool_msg, orphan_tool_msg],
            "tool_interactions": [{"assistant": assistant_tool, "tools": [tool_msg, orphan_tool_msg]}],
        }
    )
    node["assistant_message"] = assistant_final
    conversation.add_node(node, parent_id=conversation.current_node_id)

    manager = ChatManager.__new__(ChatManager)
    messages = manager._prepare_messages_for_api_with_conversation(conversation)

    tool_messages = [msg for msg in messages if msg["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-1"
    assert tool_messages[0]["content"] == "Command: pytest -q\nExit code: 0"


def test_prepare_messages_synthesizes_missing_tool_result():
    conversation = Conversation(title="missing tool result")
    conversation.initialize_with_system_message("system prompt")
    user_msg = Message(
        {
            "id": "user-1",
            "role": Role.USER,
            "content": "run",
            "timestamp": 1,
        }
    )
    node = NodeManager.create_node(user_msg, parent_id=conversation.current_node_id, model_id="model")
    assistant_tool = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-missing",
                "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }
        ],
    }
    assistant_final = Message(
        {
            "id": "assistant-1",
            "role": Role.ASSISTANT,
            "content": "done",
            "timestamp": 2,
            "tool_calls": assistant_tool["tool_calls"],
            "tool_interactions": [{"assistant": assistant_tool, "tools": []}],
        }
    )
    node["assistant_message"] = assistant_final
    conversation.add_node(node, parent_id=conversation.current_node_id)

    manager = ChatManager.__new__(ChatManager)
    messages = manager._prepare_messages_for_api_with_conversation(conversation)

    tool_messages = [msg for msg in messages if msg["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-missing"
    assert "Tool result missing" in tool_messages[0]["content"]


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

    budgeted = manager._apply_round_tool_result_budget(messages)

    assert budgeted[0]["content"] == "short result"
    assert len(budgeted[1]["content"]) < 120
    assert "result-long" in budgeted[1]["content"]
    assert 'read_tool_result({"tool_result_id":"result-long","offset":0})' in budgeted[1]["content"]
    assert budgeted[1]["raw_content"] == "L" * 120
