from __future__ import annotations

import pytest

from backend.core.tools.security.permissions import PermissionContext, PermissionEngine, normalize_permission_mode


def make_context(tool_name: str, mode: str = "plan", arguments=None) -> PermissionContext:
    return PermissionContext(
        conversation_id="conv-1",
        node_id="node-1",
        tool_call_id="call-1",
        tool_name=tool_name,
        arguments=arguments or {},
        mode=mode,
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "enter_plan_mode",
        "exit_plan_mode",
        "ask_user_question",
        "list_files",
        "read_file",
        "search_files",
        "web_search",
        "fetch_url",
        "read_tool_result",
        "list_available_tools",
        "list_tasks",
    ],
)
def test_plan_mode_allows_read_only_and_plan_tools(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name))

    assert decision.behavior == "allow"


@pytest.mark.parametrize(
    "tool_name",
    [
        "edit_file",
        "write_file",
        "apply_patch",
        "run_command",
        "start_background_command",
        "spawn_agent",
        "start_workflow",
        "create_task",
        "update_task",
    ],
)
def test_plan_mode_denies_implementation_and_mutating_tools(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name))

    assert decision.behavior == "deny"
    assert "plan mode" in decision.reason


def test_plan_mode_denies_command_text_even_for_unknown_tool_name():
    decision = PermissionEngine.default().evaluate(
        make_context("shell", arguments={"command": "pytest backend/tests"})
    )

    assert decision.behavior == "deny"


def test_normalize_permission_mode_accepts_plan():
    assert normalize_permission_mode("plan") == "plan"
    assert normalize_permission_mode("plan_mode") == "plan"
