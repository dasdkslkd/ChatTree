from dataclasses import replace
import os
from pathlib import Path
import sys

import pytest

test_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(test_dir)
sys.path.insert(0, project_root)

from backend.core.tools.security.permissions import (
    PermissionContext,
    PermissionEngine,
    PermissionRule,
)
from backend.core.tools.security.capabilities import ToolCapability, capabilities_for_tool
from backend.core.tools.security.command_policy import CommandPolicy
from backend.core.tools.security.logical_sandbox import LogicalSandbox, SandboxViolation


def make_context(tool_name="mcp__filesystem__write_file", arguments=None, mode="default"):
    return PermissionContext(
        conversation_id="conv-1",
        node_id="node-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        arguments=arguments or {},
        source="model",
        mode=mode,
        turn_grants=[],
        session_grants=[],
    )


def test_explicit_deny_wins_over_allow():
    engine = PermissionEngine(rules=[
        PermissionRule(
            id="allow-mcp",
            behavior="allow",
            target_type="mcp_tool",
            pattern="mcp__filesystem__*",
            source="user",
        ),
        PermissionRule(
            id="deny-write",
            behavior="deny",
            target_type="mcp_tool",
            pattern="mcp__filesystem__write_file",
            source="user",
        ),
    ])

    decision = engine.evaluate(make_context())

    assert decision.behavior == "deny"
    assert decision.matched_rules[0].id == "deny-write"


@pytest.mark.parametrize(
    "tool_name",
    [
        "web_search",
        "fetch_url",
        "read_tool_result",
        "list_available_tools",
    ],
)
def test_builtin_read_tools_are_allowed_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name))

    assert decision.behavior == "allow"
    assert "built-in read" in decision.reason


@pytest.mark.parametrize("tool_name", ["list_files", "read_file"])
def test_builtin_code_read_tools_are_allowed_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name))

    assert decision.behavior == "allow"


@pytest.mark.parametrize("tool_name", ["write_file", "apply_patch", "run_command"])
def test_builtin_code_mutating_tools_ask_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name))

    assert decision.behavior == "ask"


def test_mcp_tools_ask_by_default():
    decision = PermissionEngine.default().evaluate(make_context())

    assert decision.behavior == "ask"
    assert "MCP" in decision.reason


def test_mcp_route_tools_ask_by_default():
    decision = PermissionEngine.default().evaluate(make_context(tool_name="filesystem__read_file"))

    assert decision.behavior == "ask"
    assert "MCP" in decision.reason


def test_mcp_server_rule_matches_route_and_alias_names():
    engine = PermissionEngine(rules=[
        PermissionRule(
            id="deny-server",
            behavior="deny",
            target_type="mcp_server",
            pattern="filesystem",
            source="user",
        )
    ])

    route_decision = engine.evaluate(make_context(tool_name="filesystem__read_file"))
    alias_decision = engine.evaluate(make_context(tool_name="mcp__filesystem__read_file"))

    assert route_decision.behavior == "deny"
    assert route_decision.matched_rules[0].id == "deny-server"
    assert alias_decision.behavior == "deny"
    assert alias_decision.matched_rules[0].id == "deny-server"


def test_session_allow_overrides_default_mcp_ask():
    context = make_context(
        tool_name="filesystem__read_file",
        arguments={},
    )
    context = replace(
        context,
        session_grants=[
            PermissionRule(
                id="allow-read-file",
                behavior="allow",
                target_type="mcp_tool",
                pattern="filesystem__read_file",
                source="session",
            )
        ],
    )

    decision = PermissionEngine.default().evaluate(context)

    assert decision.behavior == "allow"
    assert decision.matched_rules[0].id == "allow-read-file"


def test_turn_allow_overrides_default_mcp_ask():
    context = make_context(
        tool_name="filesystem__read_file",
        arguments={},
    )
    context = replace(
        context,
        turn_grants=[
            PermissionRule(
                id="turn-allow",
                behavior="allow",
                target_type="mcp_tool",
                pattern="filesystem__read_file",
                source="session",
            )
        ],
    )

    decision = PermissionEngine.default().evaluate(context)

    assert decision.behavior == "allow"
    assert decision.matched_rules[0].id == "turn-allow"


def test_explicit_ask_overrides_session_allow():
    engine = PermissionEngine(rules=[
        PermissionRule(
            id="ask-write-file",
            behavior="ask",
            target_type="mcp_tool",
            pattern="filesystem__write_file",
            source="user",
        )
    ])
    context = make_context(
        tool_name="filesystem__write_file",
        arguments={},
    )
    context = replace(
        context,
        session_grants=[
            PermissionRule(
                id="allow-write-file",
                behavior="allow",
                target_type="mcp_tool",
                pattern="filesystem__write_file",
                source="session",
            )
        ],
    )

    decision = engine.evaluate(context)

    assert decision.behavior == "ask"
    assert decision.matched_rules[0].id == "ask-write-file"


@pytest.mark.parametrize("target_type", ["filesystem", "network"])
def test_unimplemented_sandbox_rule_types_do_not_match_tool_name(target_type):
    engine = PermissionEngine(rules=[
        PermissionRule(
            id=f"deny-{target_type}",
            behavior="deny",
            target_type=target_type,
            pattern="filesystem__read_file",
            source="user",
        )
    ])

    decision = engine.evaluate(make_context(tool_name="filesystem__read_file"))

    assert decision.behavior == "ask"
    assert not decision.matched_rules


def test_dont_ask_turns_ask_into_deny():
    decision = PermissionEngine.default().evaluate(make_context(mode="dont_ask"))

    assert decision.behavior == "deny"
    assert "dont_ask" in decision.reason


def test_bypass_does_not_skip_explicit_deny():
    engine = PermissionEngine(rules=[
        PermissionRule(
            id="deny-specific-tool",
            behavior="deny",
            target_type="tool",
            pattern="danger_tool",
            source="user",
        )
    ])

    decision = engine.evaluate(make_context(tool_name="danger_tool", mode="bypass_permissions"))

    assert decision.behavior == "deny"
    assert decision.matched_rules[0].id == "deny-specific-tool"


def test_capabilities_for_builtin_read_tool():
    assert capabilities_for_tool("web_search") == {ToolCapability.NETWORK_READ}
    assert capabilities_for_tool("list_available_tools") == {ToolCapability.READ_ONLY}


def test_capabilities_for_builtin_code_tools():
    assert capabilities_for_tool("list_files") == {ToolCapability.FILESYSTEM_READ}
    assert capabilities_for_tool("read_file") == {ToolCapability.FILESYSTEM_READ}
    assert capabilities_for_tool("write_file") == {ToolCapability.FILESYSTEM_WRITE}
    assert capabilities_for_tool("apply_patch") == {ToolCapability.FILESYSTEM_WRITE}
    assert capabilities_for_tool("run_command") == {ToolCapability.COMMAND_EXEC}


def test_mcp_tool_defaults_to_dynamic_capability():
    assert capabilities_for_tool("filesystem__read_file") == {ToolCapability.MCP_DYNAMIC}
    assert capabilities_for_tool("mcp__unknown__tool") == {ToolCapability.MCP_DYNAMIC}


def test_capability_overrides_replace_defaults():
    assert capabilities_for_tool(
        "filesystem__write_file",
        {"filesystem__write_file": ["FILESYSTEM_WRITE"]},
    ) == {ToolCapability.FILESYSTEM_WRITE}


@pytest.mark.parametrize("command", ["git status", "git diff -- backend", "rg ToolManager", "pytest test/test_tool.py -v"])
def test_command_policy_allows_common_read_and_test_commands(command):
    decision = CommandPolicy.default().classify(command)

    assert decision.behavior == "allow"


@pytest.mark.parametrize("command", ["git commit -m hi", "npm install", "pip install requests"])
def test_command_policy_asks_for_mutating_developer_commands(command):
    decision = CommandPolicy.default().classify(command)

    assert decision.behavior == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "Remove-Item . -Recurse -Force",
        "Remove-Item . -Force -Recurse",
        "git status && rm -rf /",
    ],
)
def test_command_policy_denies_destructive_commands(command):
    decision = CommandPolicy.default().classify(command)

    assert decision.behavior == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git status $(rm -rf /)",
        "git diff `Remove-Item . -Recurse -Force`",
    ],
)
def test_command_policy_asks_for_shell_substitutions(command):
    decision = CommandPolicy.default().classify(command)

    assert decision.behavior == "ask"


@pytest.mark.parametrize("command", ["git status && rm file.txt", "rg key > out.txt", "Get-Content a | Set-Content b"])
def test_command_policy_asks_for_compound_or_redirected_commands(command):
    decision = CommandPolicy.default().classify(command)

    assert decision.behavior == "ask"


def test_logical_sandbox_blocks_protected_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = LogicalSandbox(
        workspace_roots=[workspace],
        protected_paths=[".git", "data/config.json"],
    )

    with pytest.raises(SandboxViolation) as exc:
        sandbox.check_filesystem_write(workspace / ".git" / "config")

    assert "protected path" in str(exc.value)


def test_logical_sandbox_allows_workspace_write(tmp_path):
    workspace = Path(tmp_path) / "workspace"
    workspace.mkdir()
    sandbox = LogicalSandbox(workspace_roots=[workspace], protected_paths=[".git"])

    sandbox.check_filesystem_write(workspace / "notes" / "file.txt")


def test_logical_sandbox_blocks_writes_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    sandbox = LogicalSandbox(workspace_roots=[workspace], protected_paths=[".git"])

    with pytest.raises(SandboxViolation) as exc:
        sandbox.check_filesystem_write(outside / "file.txt")

    assert "workspace" in str(exc.value)


def test_logical_sandbox_blocks_protected_write_in_nested_workspace_root(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "data"
    nested.mkdir(parents=True)
    sandbox = LogicalSandbox(workspace_roots=[repo, nested], protected_paths=[".git"])

    with pytest.raises(SandboxViolation) as exc:
        sandbox.check_filesystem_write(nested / ".git" / "config")

    assert "protected path" in str(exc.value)
