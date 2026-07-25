from __future__ import annotations

import pytest

from backend.core.plans import PlanLedger
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.plan_repository import SQLitePlanRepository
from backend.core.tools.plan_tools import register_plan_tools
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
        "ask_user_question",
        "exit_plan_mode",
        "glob",
        "read",
        "grep",
        "web",
        "web_search",
        "fetch_url",
        "read_tool_result",
        "list_available_tools",
    ],
)
def test_plan_mode_allows_read_only_and_plan_tools(tool_name):
    decision = PermissionEngine.default().evaluate(make_context(tool_name))

    assert decision.behavior == "allow"


@pytest.mark.parametrize(
    "tool_name",
    [
        "edit",
        "write",
        "patch",
        "shell",
        "agent",
        "spawn_agent",
        "start_workflow",
        "create_task",
        "set_task_step",
        "cancel_task",
        "plan",
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


def test_registers_exit_plan_mode_as_independent_required_plan_tool(tmp_path):
    class Manager:
        def __init__(self):
            self.tools = {}

        def register(self, tool):
            self.tools[tool.name] = tool

    manager = Manager()
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    register_plan_tools(manager, PlanLedger(repository=SQLitePlanRepository(persistence)))

    assert set(manager.tools) == {"enter_plan_mode", "ask_user_question", "exit_plan_mode"}
    schema = manager.tools["exit_plan_mode"].parameters_schema()
    assert schema["required"] == ["plan"]
    assert schema["properties"]["plan"]["type"] == "string"
