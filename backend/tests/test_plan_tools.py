from __future__ import annotations

import asyncio
import json

from backend.api.routes.config import _sync_runtime_managers
from backend.core.plans import PlanLedger, PlanStatus
from backend.core.tools.plan_tools import register_plan_tools
from backend.core.tools.tool_manager import ToolManager


def run(coro):
    return asyncio.run(coro)


class FakeToolManager:
    def __init__(self) -> None:
        self.tools = {}

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


def context(**overrides):
    data = {
        "conversation_id": "conv-1",
        "node_id": "node-1",
        "tool_call_id": "call-1",
        "run_id": "run-1",
        "permission_mode": "modify_only",
    }
    data.update(overrides)
    return data


async def _enter_plan_mode_creates_session_and_returns_guidance_case():
    ledger = PlanLedger()
    manager = FakeToolManager()
    register_plan_tools(manager, ledger)

    raw = await manager.tools["enter_plan_mode"].execute(_runtime_context=context())
    payload = json.loads(raw)
    current = await ledger.get_active_or_awaiting("conv-1")

    assert payload["status"] == PlanStatus.ACTIVE.value
    assert payload["permission_mode"] == "plan"
    assert current is not None
    assert current.previous_permission_mode == "modify_only"
    assert "Entered plan mode" in payload["message"]
    assert "read-only planning phase" in payload["message"]
    assert "ask_user_question or exit_plan_mode" in payload["message"]
    assert "Do not use plan mode for clear implementation work" in manager.tools["enter_plan_mode"].description
    assert "genuine ambiguity" in manager.tools["enter_plan_mode"].description


def test_enter_plan_mode_creates_session_and_returns_guidance():
    run(_enter_plan_mode_creates_session_and_returns_guidance_case())


async def _exit_plan_mode_submits_plan_for_user_approval_case():
    ledger = PlanLedger()
    manager = FakeToolManager()
    register_plan_tools(manager, ledger)
    await manager.tools["enter_plan_mode"].execute(_runtime_context=context())

    updated_raw = await manager.tools["update_plan"].execute(
        mode="replace",
        content="1. Add ledger\n2. Add tools\n3. Add routes",
        _runtime_context=context(node_id="node-2", run_id="run-2", permission_mode="plan"),
    )
    updated_payload = json.loads(updated_raw)
    raw = await manager.tools["exit_plan_mode"].execute(
        _runtime_context=context(node_id="node-2", run_id="run-2", permission_mode="plan"),
    )
    payload = json.loads(raw)
    current = await ledger.get_active_or_awaiting("conv-1")

    assert payload["status"] == PlanStatus.AWAITING_APPROVAL.value
    assert payload["requires_user_approval"] is True
    assert current is not None
    assert current.status == PlanStatus.AWAITING_APPROVAL
    assert current.plan == "1. Add ledger\n2. Add tools\n3. Add routes"
    assert updated_payload["revision"] == current.plan_revision


def test_exit_plan_mode_submits_plan_for_user_approval():
    run(_exit_plan_mode_submits_plan_for_user_approval_case())


async def _ask_user_question_tool_pauses_plan_for_clarification_case():
    ledger = PlanLedger()
    manager = FakeToolManager()
    register_plan_tools(manager, ledger)
    await manager.tools["enter_plan_mode"].execute(_runtime_context=context())

    raw = await manager.tools["ask_user_question"].execute(
        question="项目栏是否默认显示所有项目？",
        options=[
            {"label": "默认显示", "description": "保持当前主页面体验"},
            {"label": "默认隐藏", "description": "用户手动启用后显示"},
        ],
        _runtime_context=context(node_id="node-2", run_id="run-2", permission_mode="plan"),
    )
    payload = json.loads(raw)
    current = await ledger.get_active_or_awaiting("conv-1")

    assert payload["status"] == PlanStatus.AWAITING_QUESTION.value
    assert payload["requires_user_response"] is True
    assert payload["question"]["question"] == "项目栏是否默认显示所有项目？"
    assert current is not None
    assert current.status == PlanStatus.AWAITING_QUESTION


def test_ask_user_question_tool_pauses_plan_for_clarification():
    run(_ask_user_question_tool_pauses_plan_for_clarification_case())


async def _plan_tools_require_runtime_conversation_context_case():
    ledger = PlanLedger()
    manager = FakeToolManager()
    register_plan_tools(manager, ledger)

    payload = json.loads(await manager.tools["enter_plan_mode"].execute())

    assert payload["error"]["type"] == "missing_runtime_context"


def test_plan_tools_require_runtime_conversation_context():
    run(_plan_tools_require_runtime_conversation_context_case())


def test_sync_runtime_managers_preserves_plan_ledger_and_registers_plan_tools():
    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.command_executor = object()

    first_tool_manager = FakeToolManager()
    _sync_runtime_managers(app, {}, object(), first_tool_manager)
    first_ledger = app.state.plan_ledger

    second_tool_manager = FakeToolManager()
    _sync_runtime_managers(app, {}, object(), second_tool_manager)

    assert isinstance(first_ledger, PlanLedger)
    assert app.state.plan_ledger is first_ledger
    assert {"enter_plan_mode", "update_plan", "exit_plan_mode"} <= set(first_tool_manager.tools)
    assert {"enter_plan_mode", "update_plan", "exit_plan_mode"} <= set(second_tool_manager.tools)


def test_registered_plan_tools_are_model_visible_in_real_tool_manager():
    tool_manager = ToolManager({"tools": {"enabled": False}})
    register_plan_tools(tool_manager, PlanLedger())

    names = {tool["function"]["name"] for tool in tool_manager.get_openai_tools()}

    assert names == {"plan"}
