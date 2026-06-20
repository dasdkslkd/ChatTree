from dataclasses import replace
import os
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
