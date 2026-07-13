from __future__ import annotations

import asyncio
import json

import pytest

from backend.api.routes.config import _sync_runtime_managers
from backend.core.tasks import ActiveTaskService
from backend.core.tools.security.permissions import PermissionContext, PermissionEngine
from backend.core.tools.task_tools import TASK_OBSERVATION_TOOL_NAMES, register_task_tools
from backend.core.tools.task_contract import SET_TASK_STEP_DESCRIPTION
from backend.core.tools.tool_manager import ToolInventoryTool, ToolManager


def run(coro):
    return asyncio.run(coro)


class FakeToolManager:
    def __init__(self) -> None:
        self.tools = {}

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


class FakeRunManager:
    pass


def context(**overrides):
    value = {
        "conversation_id": "conv-1",
        "run_id": "run-parent",
        "run_kind": "chat",
        "node_id": "node-1",
        "tool_call_id": "call-1",
        "task_context_mode": "attached",
    }
    value.update(overrides)
    return value


async def _create_and_complete_case():
    service = ActiveTaskService()
    manager = FakeToolManager()
    register_task_tools(manager, service)

    created = json.loads(await manager.tools["create_task"].execute(
        title="Verify task tools",
        detail="Use focused tests",
        steps=[{"title": "Implement"}, {"title": "Verify"}],
        _runtime_context=context(),
    ))

    assert created["status"] == "active"
    assert created["task"]["title"] == "Verify task tools"
    assert created["task_outcome"]["kind"] == "task_created"
    assert created["task_outcome"]["task_status"] == "active"
    assert created["task_outcome"]["task_snapshot"]["steps"][0]["status"] == "pending"
    assert [step["position"] for step in created["task"]["steps"]] == [1, 2]
    assert "task_id" not in json.dumps(created)
    assert "generation_id" not in json.dumps(created)
    active = await service.get_active_task("conv-1")
    versioned_context = context(
        task_generation_id=active.generation_id,
        task_revision=active.revision,
    )

    first = json.loads(await manager.tools["set_task_step"].execute(
        step=1,
        status="completed",
        evidence="Implementation committed",
        _runtime_context=versioned_context,
    ))
    assert first["completed"] is False
    assert first["task"]["steps"][0]["status"] == "completed"
    assert first["task_outcome"]["kind"] == "step_updated"
    assert first["task_outcome"]["step"] == 1
    assert first["task_outcome"]["step_status"] == "completed"
    assert first["task_outcome"]["task_status"] == "active"

    active = await service.get_active_task("conv-1")
    final = json.loads(await manager.tools["set_task_step"].execute(
        step=2,
        status="completed",
        evidence="Focused tests passed",
        _runtime_context=context(
            task_generation_id=active.generation_id,
            task_revision=active.revision,
        ),
    ))
    assert final["completed"] is True
    assert final["task"] is None
    assert final["task_outcome"]["step"] == 2
    assert final["task_outcome"]["task_status"] == "completed"
    assert [step["status"] for step in final["task_snapshot"]["steps"]] == [
        "completed",
        "completed",
    ]
    assert await service.get_active_task("conv-1") is None


def test_task_tools_create_numbered_steps_and_delete_completed_task():
    run(_create_and_complete_case())


async def _conflict_and_detached_case():
    service = ActiveTaskService()
    manager = FakeToolManager()
    register_task_tools(manager, service)
    await manager.tools["create_task"].execute(
        title="First",
        steps=[{"title": "Only"}],
        _runtime_context=context(tool_call_id="create-1"),
    )

    duplicate = json.loads(await manager.tools["create_task"].execute(
        title="Second",
        steps=[{"title": "Other"}],
        _runtime_context=context(tool_call_id="create-2"),
    ))
    detached = json.loads(await manager.tools["set_task_step"].execute(
        step=1,
        status="completed",
        evidence="hidden",
        _runtime_context=context(task_context_mode="detached"),
    ))

    assert duplicate["error"]["type"] == "active_task_exists"
    assert detached["error"]["type"] == "task_context_disabled"


def test_task_tools_reject_second_task_and_detached_mutation():
    run(_conflict_and_detached_case())


def test_task_mutation_tools_reject_unseen_active_task():
    async def scenario():
        service = ActiveTaskService()
        manager = FakeToolManager()
        register_task_tools(manager, service)
        await service.create_task(
            conversation_id="conv-1",
            title="Created elsewhere",
            steps=[{"title": "Do not mutate late"}],
        )

        update = json.loads(await manager.tools["set_task_step"].execute(
            step=1,
            status="completed",
            evidence="late",
            _runtime_context=context(),
        ))
        cancel = json.loads(await manager.tools["cancel_task"].execute(
            reason="late",
            _runtime_context=context(),
        ))

        assert update["error"]["type"] == "task_context_stale"
        assert cancel["error"]["type"] == "task_context_stale"

    run(scenario())


def test_cancel_task_tool_returns_top_level_task_outcome():
    async def scenario():
        service = ActiveTaskService()
        manager = FakeToolManager()
        register_task_tools(manager, service)
        task = await service.create_task(
            conversation_id="conv-1",
            title="取消测试",
            steps=[{"title": "等待"}],
        )

        cancelled = json.loads(await manager.tools["cancel_task"].execute(
            reason="user changed direction",
            _runtime_context=context(
                task_generation_id=task.generation_id,
                task_revision=task.revision,
            ),
        ))

        assert cancelled["cancelled"] is True
        assert cancelled["task"] is None
        assert cancelled["task_outcome"]["kind"] == "task_cancelled"
        assert cancelled["task_outcome"]["task_status"] == "cancelled"
        assert cancelled["task_outcome"]["task_snapshot"]["steps"][0]["title"] == "等待"

    run(scenario())


def test_task_tool_schemas_expose_only_public_arguments():
    manager = FakeToolManager()
    register_task_tools(manager, ActiveTaskService())

    assert set(manager.tools) == {"create_task", "set_task_step", "cancel_task"}
    assert manager.tools["create_task"].parameters_schema()["required"] == ["title", "steps"]
    assert manager.tools["set_task_step"].parameters_schema()["required"] == ["step", "status", "evidence"]
    assert manager.tools["set_task_step"].description == SET_TASK_STEP_DESCRIPTION
    for tool in manager.tools.values():
        schema = json.dumps(tool.parameters_schema())
        assert "task_id" not in schema
        assert "task_step_id" not in schema
        assert "in_progress" not in schema


@pytest.mark.parametrize("tool_name", ["create_task", "set_task_step", "cancel_task"])
def test_task_tools_allowed_by_default(tool_name):
    decision = PermissionEngine.default().evaluate(
        PermissionContext(
            conversation_id="conv-1",
            node_id="node-1",
            tool_call_id="call-1",
            tool_name=tool_name,
            arguments={},
        )
    )
    assert decision.behavior == "allow"


def test_sync_runtime_managers_preserves_active_task_service():
    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.run_manager = FakeRunManager()
    app.state.command_executor = object()

    first_tools = FakeToolManager()
    _sync_runtime_managers(app, {}, object(), first_tools)
    first_service = app.state.task_service

    second_tools = FakeToolManager()
    _sync_runtime_managers(app, {}, object(), second_tools)

    assert isinstance(first_service, ActiveTaskService)
    assert app.state.task_service is first_service
    assert app.state.run_manager.task_service is first_service
    expected = {"create_task", "set_task_step", "cancel_task"}
    assert expected <= set(first_tools.tools)
    assert expected <= set(second_tools.tools)


def test_registered_task_tools_are_model_visible_in_real_tool_manager():
    tool_manager = ToolManager({"tools": {"enabled": False}})
    register_task_tools(tool_manager, ActiveTaskService())

    names = {tool["function"]["name"] for tool in tool_manager.get_openai_tools()}
    assert {"create_task", "set_task_step", "cancel_task"} <= names


def test_task_observation_tools_refresh_the_runtime_task_version():
    assert TASK_OBSERVATION_TOOL_NAMES == {
        "list_agents",
        "wait_agent",
    }


def test_detached_tool_inventory_does_not_reveal_task_tools():
    async def scenario():
        tool_manager = ToolManager({"tools": {"enabled": False}})
        register_task_tools(tool_manager, ActiveTaskService())

        inventory = json.loads(await ToolInventoryTool(tool_manager).execute(
            _runtime_context={"task_context_mode": "detached"},
        ))

        serialized = json.dumps(inventory)
        assert "create_task" not in serialized
        assert "set_task_step" not in serialized
        assert "cancel_task" not in serialized

    run(scenario())
