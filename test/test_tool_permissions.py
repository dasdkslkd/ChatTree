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


def test_builtin_read_tools_are_allowed_by_default():
    decision = PermissionEngine.default().evaluate(make_context(tool_name="web_search"))

    assert decision.behavior == "allow"
    assert "built-in read" in decision.reason


def test_mcp_tools_ask_by_default():
    decision = PermissionEngine.default().evaluate(make_context())

    assert decision.behavior == "ask"
    assert "MCP" in decision.reason


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
