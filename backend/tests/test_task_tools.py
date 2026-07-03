from __future__ import annotations

import asyncio
import json

import pytest

from backend.api.routes.config import _sync_runtime_managers
from backend.core.tasks import TaskLedger, TaskStatus
from backend.core.tools.tool_manager import ToolManager
from backend.core.tools.security.permissions import PermissionContext, PermissionEngine
from backend.core.tools.task_tools import register_task_tools


def run(coro):
    return asyncio.run(coro)


class FakeToolManager:
    def __init__(self) -> None:
        self.tools = {}

    def register(self, tool) -> None:
        self.tools[tool.name] = tool


class FakeRunManager:
    def __init__(self) -> None:
        self.listeners = []

    def add_finish_listener(self, listener) -> None:
        self.listeners.append(listener)


def context(**overrides):
    data = {
        "conversation_id": "conv-1",
        "run_id": "run-parent",
        "run_kind": "chat",
        "node_id": "node-1",
        "tool_call_id": "call-1",
    }
    data.update(overrides)
    return data


async def _create_task_records_created_by_run_id_case():
    ledger = TaskLedger()
    manager = FakeToolManager()
    register_task_tools(manager, ledger)

    payload = json.loads(
        await manager.tools["create_task"].execute(
            title="Verify backend task tools",
            detail="Use focused tests",
            _runtime_context=context(),
        )
    )

    assert payload["task_id"].startswith("task_")
    assert payload["status"] == "pending"
    assert payload["task"]["created_by_run_id"] == "run-parent"
    assert payload["task"]["conversation_id"] == "conv-1"
    assert payload["task"]["metadata"]["anchor_node_id"] == "node-1"
    assert payload["task"]["metadata"]["tool_call_id"] == "call-1"

def test_create_task_records_created_by_run_id():
    run(_create_task_records_created_by_run_id_case())


async def _update_task_refuses_invalid_status_and_requires_evidence_for_finished_case():
    ledger = TaskLedger()
    manager = FakeToolManager()
    register_task_tools(manager, ledger)
    created = json.loads(
        await manager.tools["create_task"].execute(
            title="Finish with evidence",
            _runtime_context=context(),
        )
    )

    invalid = json.loads(
        await manager.tools["update_task"].execute(
            task_id=created["task_id"],
            status="done",
            _runtime_context=context(),
        )
    )
    missing_completed_evidence = json.loads(
        await manager.tools["update_task"].execute(
            task_id=created["task_id"],
            status="completed",
            _runtime_context=context(),
        )
    )
    missing_blocked_evidence = json.loads(
        await manager.tools["update_task"].execute(
            task_id=created["task_id"],
            status="blocked",
            _runtime_context=context(),
        )
    )

    assert invalid["error"]["type"] == "invalid_arguments"
    assert "status" in invalid["error"]["message"]
    assert missing_completed_evidence["error"]["type"] == "invalid_arguments"
    assert "evidence_summary" in missing_completed_evidence["error"]["message"]
    assert missing_blocked_evidence["error"]["type"] == "invalid_arguments"
    assert "evidence_summary" in missing_blocked_evidence["error"]["message"]


def test_update_task_refuses_invalid_status_and_requires_evidence_for_finished():
    run(_update_task_refuses_invalid_status_and_requires_evidence_for_finished_case())


async def _update_task_marks_completed_with_evidence_case():
    ledger = TaskLedger()
    manager = FakeToolManager()
    register_task_tools(manager, ledger)
    created = json.loads(
        await manager.tools["create_task"].execute(
            title="Complete me",
            _runtime_context=context(),
        )
    )

    updated = json.loads(
        await manager.tools["update_task"].execute(
            task_id=created["task_id"],
            status="completed",
            evidence_summary="Focused tests passed",
            _runtime_context=context(),
        )
    )

    assert updated["status"] == "completed"
    assert updated["task"]["evidence_run_id"] == "run-parent"
    assert updated["task"]["evidence_summary"] == "Focused tests passed"


def test_update_task_marks_completed_with_evidence():
    run(_update_task_marks_completed_with_evidence_case())


async def _list_tasks_filters_by_status_case():
    ledger = TaskLedger()
    manager = FakeToolManager()
    register_task_tools(manager, ledger)
    first = json.loads(
        await manager.tools["create_task"].execute(
            title="Open task",
            _runtime_context=context(),
        )
    )
    second = json.loads(
        await manager.tools["create_task"].execute(
            title="Finished task",
            _runtime_context=context(),
        )
    )
    await manager.tools["update_task"].execute(
        task_id=second["task_id"],
        status=TaskStatus.COMPLETED.value,
        evidence_summary="Done",
        _runtime_context=context(),
    )

    pending_only = json.loads(
        await manager.tools["list_tasks"].execute(
            statuses=["pending"],
            include_finished=True,
            _runtime_context=context(),
        )
    )
    unfinished = json.loads(
        await manager.tools["list_tasks"].execute(
            include_finished=False,
            _runtime_context=context(),
        )
    )

    assert [task["task_id"] for task in pending_only["tasks"]] == [first["task_id"]]
    assert [task["task_id"] for task in unfinished["tasks"]] == [first["task_id"]]


def test_list_tasks_filters_by_status():
    run(_list_tasks_filters_by_status_case())


@pytest.mark.parametrize("tool_name", ["create_task", "update_task", "list_tasks"])
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


def test_sync_runtime_managers_preserves_ledger_and_installs_listener_once():
    app = type("App", (), {})()
    app.state = type("State", (), {})()
    app.state.run_manager = FakeRunManager()
    app.state.command_executor = object()

    first_tool_manager = FakeToolManager()
    _sync_runtime_managers(app, {}, object(), first_tool_manager)
    first_ledger = app.state.task_ledger
    first_listener_count = len(app.state.run_manager.listeners)

    second_tool_manager = FakeToolManager()
    _sync_runtime_managers(app, {}, object(), second_tool_manager)

    assert isinstance(first_ledger, TaskLedger)
    assert app.state.task_ledger is first_ledger
    assert len(app.state.run_manager.listeners) == first_listener_count == 1
    assert {"create_task", "update_task", "list_tasks"} <= set(first_tool_manager.tools)
    assert {"create_task", "update_task", "list_tasks"} <= set(second_tool_manager.tools)


def test_registered_task_tools_are_model_visible_in_real_tool_manager():
    tool_manager = ToolManager({"tools": {"enabled": False}})
    register_task_tools(tool_manager, TaskLedger())

    names = {tool["function"]["name"] for tool in tool_manager.get_openai_tools()}

    assert {"create_task", "update_task", "list_tasks"} <= names
