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
    normalize_permission_mode,
)
from backend.core.tools.security.capabilities import ToolCapability, capabilities_for_tool
from backend.core.tools.security.command_policy import CommandPolicy
from backend.core.tools.security.logical_sandbox import LogicalSandbox, SandboxViolation


def make_context(tool_name="mcp__filesystem__write", arguments=None, mode="default"):
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
            pattern="mcp__filesystem__write",
            source="user",
        ),
    ])

    decision = engine.evaluate(make_context())

    assert decision.behavior == "deny"
    assert decision.matched_rules[0].id == "deny-write"


@pytest.mark.parametrize(
    "tool_name",
    [
        "web",
        "web_search",
        "fetch_url",
        "read_tool_result",
        "list_available_tools",
        "tools",
    ],
)
def test_builtin_read_tools_are_allowed_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name))

    assert decision.behavior == "allow"
    assert "built-in read" in decision.reason


@pytest.mark.parametrize("tool_name", ["glob", "read", "grep"])
def test_builtin_code_read_tools_are_allowed_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name))

    assert decision.behavior == "allow"


@pytest.mark.parametrize("tool_name", ["edit", "write", "patch", "shell"])
def test_builtin_code_mutating_tools_ask_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name))

    assert decision.behavior == "ask"


@pytest.mark.parametrize("tool_name", ["web_search", "read", "edit", "write", "shell"])
def test_auto_approve_mode_allows_non_delete_tools(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name, mode="auto_approve"))

    assert decision.behavior == "allow"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("delete_file", {"path": "notes.txt"}),
        ("mcp__filesystem__remove_file", {"path": "notes.txt"}),
        ("shell", {"command": "rm notes.txt"}),
    ],
)
def test_auto_approve_mode_still_asks_for_explicit_delete(tool_name, arguments):
    decision = PermissionEngine.default().evaluate(
        make_context(tool_name=tool_name, arguments=arguments, mode="auto_approve")
    )

    assert decision.behavior == "ask"
    assert "delete" in decision.reason.lower() or "remove" in decision.reason.lower()


@pytest.mark.parametrize("tool_name", ["web_search", "read", "grep"])
def test_modify_only_mode_asks_for_read_tools(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name, mode="modify_only"))

    assert decision.behavior == "ask"


@pytest.mark.parametrize("tool_name", ["edit", "write", "patch", "shell"])
def test_modify_only_mode_allows_mutating_tools(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name, mode="modify_only"))

    assert decision.behavior == "allow"


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("delete_file", {"path": "notes.txt"}),
        ("shell", {"command": "rm notes.txt"}),
    ],
)
def test_modify_only_mode_still_asks_for_explicit_delete(tool_name, arguments):
    decision = PermissionEngine.default().evaluate(
        make_context(tool_name=tool_name, arguments=arguments, mode="modify_only")
    )

    assert decision.behavior == "ask"


@pytest.mark.parametrize("tool_name", ["web_search", "read", "edit"])
def test_ask_always_mode_asks_for_every_tool(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name, mode="ask_always"))

    assert decision.behavior == "ask"


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("auto", "auto_approve"),
        ("ask_modify", "modify_only"),
        ("ask_on_modify", "modify_only"),
        ("modify_only", "modify_only"),
        ("all", "ask_always"),
        ("bypass_permissions", "auto_approve"),
        ("default", "default"),
        (None, "modify_only"),
    ],
)
def test_normalize_permission_mode_accepts_new_and_legacy_names(raw, normalized):
    assert normalize_permission_mode(raw) == normalized


def test_mcp_tools_ask_by_default():
    decision = PermissionEngine.default().evaluate(make_context())

    assert decision.behavior == "ask"
    assert "MCP" in decision.reason


def test_mcp_route_tools_ask_by_default():
    decision = PermissionEngine.default().evaluate(make_context(tool_name="filesystem__read"))

    assert decision.behavior == "ask"
    assert "MCP" in decision.reason


@pytest.mark.parametrize("tool_name", [
    "agent",
    "spawn_agent",
    "wait_agent",
    "list_agents",
    "send_message",
    "send_input",
    "followup_task",
    "resume_agent",
    "close_agent",
    "interrupt_agent",
])
def test_agent_management_tools_allowed_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name=tool_name))

    assert decision.behavior == "allow"


def test_spawn_agent_allowed_even_in_ask_always_mode():
    decision = PermissionEngine.default().evaluate(make_context(tool_name="spawn_agent", mode="ask_always"))

    assert decision.behavior == "allow"


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

    route_decision = engine.evaluate(make_context(tool_name="filesystem__read"))
    alias_decision = engine.evaluate(make_context(tool_name="mcp__filesystem__read"))

    assert route_decision.behavior == "deny"
    assert route_decision.matched_rules[0].id == "deny-server"
    assert alias_decision.behavior == "deny"
    assert alias_decision.matched_rules[0].id == "deny-server"


def test_session_allow_overrides_default_mcp_ask():
    context = make_context(
        tool_name="filesystem__read",
        arguments={},
    )
    context = replace(
        context,
        session_grants=[
            PermissionRule(
                id="allow-read-file",
                behavior="allow",
                target_type="mcp_tool",
                pattern="filesystem__read",
                source="session",
            )
        ],
    )

    decision = PermissionEngine.default().evaluate(context)

    assert decision.behavior == "allow"
    assert decision.matched_rules[0].id == "allow-read-file"


def test_turn_allow_overrides_default_mcp_ask():
    context = make_context(
        tool_name="filesystem__read",
        arguments={},
    )
    context = replace(
        context,
        turn_grants=[
            PermissionRule(
                id="turn-allow",
                behavior="allow",
                target_type="mcp_tool",
                pattern="filesystem__read",
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
            pattern="filesystem__write",
            source="user",
        )
    ])
    context = make_context(
        tool_name="filesystem__write",
        arguments={},
    )
    context = replace(
        context,
        session_grants=[
            PermissionRule(
                id="allow-write-file",
                behavior="allow",
                target_type="mcp_tool",
                pattern="filesystem__write",
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
            pattern="filesystem__read",
            source="user",
        )
    ])

    decision = engine.evaluate(make_context(tool_name="filesystem__read"))

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
    assert capabilities_for_tool("tools") == {ToolCapability.READ_ONLY}


def test_tool_permission_rule_matches_shell_argument_pattern():
    engine = PermissionEngine.default()
    context = replace(
        make_context(tool_name="shell", arguments={"command": "git diff -- backend"}),
        turn_grants=[
            PermissionRule(
                id="allow-safe-diff",
                behavior="allow",
                target_type="tool",
                pattern="shell(git diff*)",
                source="session",
            )
        ],
    )

    decision = engine.evaluate(context)

    assert decision.behavior == "allow"
    assert decision.matched_rules[0].id == "allow-safe-diff"


def test_tool_permission_rule_does_not_match_wrong_shell_argument():
    engine = PermissionEngine.default()
    context = replace(
        make_context(tool_name="shell", arguments={"command": "git commit -m hi"}),
        turn_grants=[
            PermissionRule(
                id="allow-safe-diff",
                behavior="allow",
                target_type="tool",
                pattern="shell(git diff*)",
                source="session",
            )
        ],
    )

    decision = engine.evaluate(context)

    assert decision.behavior == "ask"


def test_capabilities_for_builtin_code_tools():
    assert capabilities_for_tool("glob") == {ToolCapability.FILESYSTEM_READ}
    assert capabilities_for_tool("read") == {ToolCapability.FILESYSTEM_READ}
    assert capabilities_for_tool("grep") == {ToolCapability.FILESYSTEM_READ}
    assert capabilities_for_tool("edit") == {ToolCapability.FILESYSTEM_WRITE}
    assert capabilities_for_tool("write") == {ToolCapability.FILESYSTEM_WRITE}
    assert capabilities_for_tool("patch") == {ToolCapability.FILESYSTEM_WRITE}
    assert capabilities_for_tool("shell") == {ToolCapability.COMMAND_EXEC}


def test_mcp_tool_defaults_to_dynamic_capability():
    assert capabilities_for_tool("filesystem__read") == {ToolCapability.MCP_DYNAMIC}
    assert capabilities_for_tool("mcp__unknown__tool") == {ToolCapability.MCP_DYNAMIC}


def test_capability_overrides_replace_defaults():
    assert capabilities_for_tool(
        "filesystem__write",
        {"filesystem__write": ["FILESYSTEM_WRITE"]},
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
