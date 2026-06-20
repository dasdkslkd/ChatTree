import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core.config.types import Role
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine, PermissionRule


class FakeToolManager:
    def __init__(self):
        self.calls = []

    async def execute_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"ok": True, "name": name}, ensure_ascii=False)


def make_tool_call(name="web_search", args=None):
    return {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args or {"query": "ChatTree"}, ensure_ascii=False),
        },
    }


def make_raw_tool_call(name="web_search", raw_arguments=""):
    return {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": raw_arguments,
        },
    }


def make_orchestrator(permission_engine, logical_sandbox):
    tool_manager = FakeToolManager()
    return (
        ToolOrchestrator(
            tool_manager=tool_manager,
            permission_engine=permission_engine,
            approval_manager=ApprovalManager(),
            logical_sandbox=logical_sandbox,
        ),
        tool_manager,
    )


async def _allowed_tool_executes_manager(tmp_path):
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine.default(),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_tool_call("web_search", {"query": "中文"}),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == [("web_search", {"query": "中文"})]
    assert message["role"] == Role.TOOL
    assert message["name"] == "web_search"
    assert message["tool_call_id"] == "call-1"
    assert message["tool_calls"] is None
    assert json.loads(message["content"]) == {"ok": True, "name": "web_search"}


def test_allowed_tool_executes_manager_and_returns_tool_message(tmp_path):
    asyncio.run(_allowed_tool_executes_manager(tmp_path))


async def _denied_tool_does_not_execute_manager(tmp_path):
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine(
            rules=[
                PermissionRule(
                    id="deny-danger",
                    behavior="deny",
                    target_type="tool",
                    pattern="danger_tool",
                )
            ]
        ),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_tool_call("danger_tool", {"value": 1}),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == []
    assert message["role"] == Role.TOOL
    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert error["tool_name"] == "danger_tool"
    assert "deny-danger" in error["reason"]


def test_denied_tool_returns_permission_denied_without_manager_call(tmp_path):
    asyncio.run(_denied_tool_does_not_execute_manager(tmp_path))


async def _sandbox_violation_does_not_execute_manager(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected_target = workspace / ".git" / "config"
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine(
            rules=[
                PermissionRule(
                    id="allow-writes",
                    behavior="allow",
                    target_type="tool",
                    pattern="write_file",
                )
            ]
        ),
        LogicalSandbox(workspace_roots=[workspace], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_tool_call("write_file", {"path": str(protected_target), "content": "x"}),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == []
    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert error["tool_name"] == "write_file"
    assert "protected path" in error["reason"]


def test_sandbox_protected_write_violation_returns_permission_denied(tmp_path):
    asyncio.run(_sandbox_violation_does_not_execute_manager(tmp_path))


async def _sandbox_violation_for_edit_file_path_like_args(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected_target = workspace / ".git" / "config"
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine(
            rules=[
                PermissionRule(
                    id="allow-edit",
                    behavior="allow",
                    target_type="tool",
                    pattern="mcp__filesystem__edit_file",
                )
            ]
        ),
        LogicalSandbox(workspace_roots=[workspace], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_tool_call(
            "mcp__filesystem__edit_file",
            {"file_path": str(protected_target), "edits": []},
        ),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == []
    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert error["tool_name"] == "mcp__filesystem__edit_file"
    assert "protected path" in error["reason"]


def test_sandbox_protected_write_violation_for_edit_file_path_like_args(tmp_path):
    asyncio.run(_sandbox_violation_for_edit_file_path_like_args(tmp_path))


async def _sandbox_violation_for_destination_arg(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected_target = workspace / ".git" / "config"
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine(
            rules=[
                PermissionRule(
                    id="allow-copy",
                    behavior="allow",
                    target_type="tool",
                    pattern="copy_file",
                )
            ]
        ),
        LogicalSandbox(workspace_roots=[workspace], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_tool_call(
            "copy_file",
            {
                "source": str(workspace / "notes.txt"),
                "destination": str(protected_target),
            },
        ),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == []
    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert error["tool_name"] == "copy_file"
    assert "protected path" in error["reason"]


def test_sandbox_protected_write_violation_for_destination_arg(tmp_path):
    asyncio.run(_sandbox_violation_for_destination_arg(tmp_path))


async def _invalid_json_arguments_are_preserved_for_tool_manager(tmp_path):
    raw_arguments = '{"query": "ChatTree"'
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine.default(),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_raw_tool_call("web_search", raw_arguments),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == [("web_search", {"arguments": raw_arguments})]
    assert json.loads(message["content"]) == {"ok": True, "name": "web_search"}


def test_invalid_json_arguments_are_preserved_for_tool_manager(tmp_path):
    asyncio.run(_invalid_json_arguments_are_preserved_for_tool_manager(tmp_path))
