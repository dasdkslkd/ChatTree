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
from backend.core.tools.security.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionRule,
)


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


def make_orchestrator(permission_engine, logical_sandbox, approval_manager=None):
    tool_manager = FakeToolManager()
    return (
        ToolOrchestrator(
            tool_manager=tool_manager,
            permission_engine=permission_engine,
            approval_manager=approval_manager or ApprovalManager(),
            logical_sandbox=logical_sandbox,
        ),
        tool_manager,
    )


def make_permission_context(tool_name="filesystem__read_file", arguments=None):
    return PermissionContext(
        conversation_id="conv-1",
        node_id="node-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        arguments=arguments or {},
        source="model",
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


async def _ask_approval_approve_once_executes_manager(tmp_path):
    decision = PermissionEngine.default().evaluate(
        make_permission_context("filesystem__read_file", {"path": "notes.txt"})
    )
    assert decision.behavior == "ask"

    approval_manager = ApprovalManager(timeout_seconds=1)
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine.default(),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
        approval_manager=approval_manager,
    )
    events = []

    async def emit_event(event):
        events.append(event)
        if event["event_type"] == "tool_approval_request":
            approval = event["approval"]
            assert approval["conversation_id"] == "conv-1"
            assert approval["node_id"] == "node-1"
            assert approval["tool_call_id"] == "call-1"
            assert approval["tool_name"] == "filesystem__read_file"
            assert approval["arguments_preview"] == '{"path": "notes.txt"}'
            assert approval["risk_level"] == "medium"
            assert approval["suggested_actions"] == [
                "allow_once",
                "allow_session",
                "deny",
            ]
            asyncio.get_running_loop().call_soon(
                approval_manager.decide,
                approval["id"],
                "approve",
                "once",
            )

    message = await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "notes.txt"}),
        conversation_id="conv-1",
        node_id="node-1",
        emit_event=emit_event,
    )

    assert [event["event_type"] for event in events] == [
        "tool_approval_request",
        "tool_approval_result",
    ]
    result_event = events[1]["approval"]
    assert result_event["status"] == "approved"
    assert result_event["grant_scope"] == "once"
    assert tool_manager.calls == [("filesystem__read_file", {"path": "notes.txt"})]
    assert json.loads(message["content"]) == {
        "ok": True,
        "name": "filesystem__read_file",
    }


def test_ask_approval_approve_once_executes_manager(tmp_path):
    asyncio.run(_ask_approval_approve_once_executes_manager(tmp_path))


async def _ask_approval_can_be_decided_synchronously_in_emit_event(tmp_path):
    approval_manager = ApprovalManager(timeout_seconds=1)
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine.default(),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
        approval_manager=approval_manager,
    )
    events = []

    async def emit_event(event):
        events.append(event)
        if event["event_type"] == "tool_approval_request":
            approval_manager.decide(
                event["approval"]["id"],
                decision="approve",
                scope="once",
            )

    message = await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "notes.txt"}),
        conversation_id="conv-1",
        node_id="node-1",
        emit_event=emit_event,
    )

    assert [event["event_type"] for event in events] == [
        "tool_approval_request",
        "tool_approval_result",
    ]
    assert events[1]["approval"]["status"] == "approved"
    assert tool_manager.calls == [("filesystem__read_file", {"path": "notes.txt"})]
    assert json.loads(message["content"]) == {
        "ok": True,
        "name": "filesystem__read_file",
    }


def test_ask_approval_can_be_decided_synchronously_in_emit_event(tmp_path):
    asyncio.run(_ask_approval_can_be_decided_synchronously_in_emit_event(tmp_path))


async def _ask_approval_deny_returns_permission_denied(tmp_path):
    approval_manager = ApprovalManager(timeout_seconds=1)
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine.default(),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
        approval_manager=approval_manager,
    )
    events = []

    async def emit_event(event):
        events.append(event)
        if event["event_type"] == "tool_approval_request":
            approval = event["approval"]
            asyncio.get_running_loop().call_soon(
                approval_manager.decide,
                approval["id"],
                "deny",
                "once",
            )

    message = await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "notes.txt"}),
        conversation_id="conv-1",
        node_id="node-1",
        emit_event=emit_event,
    )

    assert [event["event_type"] for event in events] == [
        "tool_approval_request",
        "tool_approval_result",
    ]
    assert events[1]["approval"]["status"] == "denied"
    assert tool_manager.calls == []
    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert error["tool_name"] == "filesystem__read_file"
    assert "denied" in error["reason"]


def test_ask_approval_deny_returns_permission_denied(tmp_path):
    asyncio.run(_ask_approval_deny_returns_permission_denied(tmp_path))


async def _ask_approval_timeout_returns_permission_denied(tmp_path):
    approval_manager = ApprovalManager(timeout_seconds=0.01)
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine.default(),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
        approval_manager=approval_manager,
    )
    events = []

    async def emit_event(event):
        events.append(event)

    message = await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "notes.txt"}),
        conversation_id="conv-1",
        node_id="node-1",
        emit_event=emit_event,
    )

    assert [event["event_type"] for event in events] == [
        "tool_approval_request",
        "tool_approval_result",
    ]
    assert events[1]["approval"]["status"] == "expired"
    assert events[1]["approval"]["grant_scope"] is None
    assert tool_manager.calls == []
    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert error["tool_name"] == "filesystem__read_file"
    assert "expired" in error["reason"] or "timeout" in error["reason"].lower()


def test_ask_approval_timeout_returns_permission_denied(tmp_path):
    asyncio.run(_ask_approval_timeout_returns_permission_denied(tmp_path))


async def _ask_approval_session_scope_bypasses_second_prompt(tmp_path):
    approval_manager = ApprovalManager(timeout_seconds=1)
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine.default(),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
        approval_manager=approval_manager,
    )
    events = []

    async def emit_event(event):
        events.append(event)
        if event["event_type"] == "tool_approval_request":
            approval = event["approval"]
            asyncio.get_running_loop().call_soon(
                approval_manager.decide,
                approval["id"],
                "approve",
                "session",
            )

    first_message = await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "one.txt"}),
        conversation_id="conv-1",
        node_id="node-1",
        emit_event=emit_event,
    )
    second_message = await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "two.txt"}),
        conversation_id="conv-1",
        node_id="node-2",
        emit_event=emit_event,
    )

    assert [event["event_type"] for event in events] == [
        "tool_approval_request",
        "tool_approval_result",
    ]
    assert events[1]["approval"]["status"] == "approved"
    assert events[1]["approval"]["grant_scope"] == "session"
    assert tool_manager.calls == [
        ("filesystem__read_file", {"path": "one.txt"}),
        ("filesystem__read_file", {"path": "two.txt"}),
    ]
    assert json.loads(first_message["content"])["ok"] is True
    assert json.loads(second_message["content"])["ok"] is True


def test_ask_approval_session_scope_bypasses_second_prompt(tmp_path):
    asyncio.run(_ask_approval_session_scope_bypasses_second_prompt(tmp_path))


async def _session_scope_does_not_bypass_explicit_ask_rule(tmp_path):
    approval_manager = ApprovalManager(timeout_seconds=1)
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine(
            rules=[
                PermissionRule(
                    id="ask-read-file-explicitly",
                    behavior="ask",
                    target_type="mcp_tool",
                    pattern="filesystem__read_file",
                    source="user",
                )
            ]
        ),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
        approval_manager=approval_manager,
    )
    events = []

    async def emit_event(event):
        events.append(event)
        if event["event_type"] == "tool_approval_request":
            asyncio.get_running_loop().call_soon(
                approval_manager.decide,
                event["approval"]["id"],
                "approve",
                "session",
            )

    await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "one.txt"}),
        conversation_id="conv-1",
        node_id="node-1",
        emit_event=emit_event,
    )
    await orchestrator.execute_tool_call(
        make_tool_call("filesystem__read_file", {"path": "two.txt"}),
        conversation_id="conv-1",
        node_id="node-2",
        emit_event=emit_event,
    )

    assert [event["event_type"] for event in events] == [
        "tool_approval_request",
        "tool_approval_result",
        "tool_approval_request",
        "tool_approval_result",
    ]
    assert tool_manager.calls == [
        ("filesystem__read_file", {"path": "one.txt"}),
        ("filesystem__read_file", {"path": "two.txt"}),
    ]


def test_session_scope_does_not_bypass_explicit_ask_rule(tmp_path):
    asyncio.run(_session_scope_does_not_bypass_explicit_ask_rule(tmp_path))


async def _command_policy_denies_destructive_command_before_execution(tmp_path, command):
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine(
            rules=[
                PermissionRule(
                    id="allow-shell-tool",
                    behavior="allow",
                    target_type="tool",
                    pattern="shell_exec",
                    source="user",
                )
            ]
        ),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_tool_call("shell_exec", {"command": command}),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == []
    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert error["tool_name"] == "shell_exec"
    assert "destructive recursive deletion" in error["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "Remove-Item . -Recurse -Force",
    ],
)
def test_command_policy_denies_destructive_command_before_execution(tmp_path, command):
    asyncio.run(_command_policy_denies_destructive_command_before_execution(tmp_path, command))


async def _command_policy_allows_common_read_command_to_continue(tmp_path):
    orchestrator, tool_manager = make_orchestrator(
        PermissionEngine(
            rules=[
                PermissionRule(
                    id="allow-shell-tool",
                    behavior="allow",
                    target_type="tool",
                    pattern="shell_exec",
                    source="user",
                )
            ]
        ),
        LogicalSandbox(workspace_roots=[tmp_path], protected_paths=[".git"]),
    )

    message = await orchestrator.execute_tool_call(
        make_tool_call("shell_exec", {"command": "git status --short"}),
        conversation_id="conv-1",
        node_id="node-1",
    )

    assert tool_manager.calls == [("shell_exec", {"command": "git status --short"})]
    assert json.loads(message["content"]) == {"ok": True, "name": "shell_exec"}


def test_command_policy_allows_common_read_command_to_continue(tmp_path):
    asyncio.run(_command_policy_allows_common_read_command_to_continue(tmp_path))
