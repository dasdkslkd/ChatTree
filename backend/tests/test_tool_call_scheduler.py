import asyncio
from time import perf_counter

import pytest

from backend.core.chat.chat_manager import ChatManager
from backend.core.tools.security.capabilities import (
    ToolCapability,
    UnknownToolCapabilitiesError,
    capabilities_for_tool,
)
from backend.core.tools.tool_call_scheduler import plan_tool_call_waves


def _tool_call(name: str, call_id: str | None = None) -> dict:
    return {
        "id": call_id or f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_scheduler_batches_only_consecutive_parallel_safe_tools():
    waves = plan_tool_call_waves(
        [_tool_call("read"), _tool_call("grep"), _tool_call("edit"), _tool_call("glob")],
        capabilities_for_tool,
    )

    assert [(wave.parallel, [item.tool_name for item in wave.calls]) for wave in waves] == [
        (True, ["read", "grep"]),
        (False, ["edit"]),
        (True, ["glob"]),
    ]


def test_scheduler_rejects_tools_without_declared_capabilities():
    with pytest.raises(UnknownToolCapabilitiesError):
        plan_tool_call_waves([_tool_call("unknown__read")], capabilities_for_tool)


class _FakeToolManager:
    def __init__(self, capabilities: dict[str, set[ToolCapability]]) -> None:
        self._capabilities = capabilities

    def capabilities_for(self, name: str, workspace=None) -> set[ToolCapability]:
        return self._capabilities[name]


class _ImmediateToolManager:
    async def execute_tool(self, name, arguments, **kwargs) -> str:
        return '{"ok": true}'


def _chat_manager_for_parallel_test(
    capabilities: dict[str, set[ToolCapability]],
    delays: dict[str, float],
    events: list[str] | None = None,
) -> ChatManager:
    manager = ChatManager.__new__(ChatManager)
    manager.tool_manager = _FakeToolManager(capabilities)
    manager.chat_repository = None
    events = events if events is not None else []

    async def execute_single(tool_call: dict, **kwargs) -> dict:
        name = str((tool_call.get("function") or {}).get("name") or "")
        events.append(f"start:{name}")
        await asyncio.sleep(delays.get(name, 0.0))
        events.append(f"end:{name}")
        return {
            "role": "tool",
            "content": name,
            "name": name,
            "tool_call_id": tool_call.get("id"),
        }

    manager._execute_single_tool_call = execute_single
    manager._refresh_task_context_after_relevant_tool = lambda **kwargs: None
    manager._permission_mode_after_plan_tools = lambda messages, mode: mode
    return manager


def test_chat_manager_parallel_wave_preserves_result_order():
    capabilities = {
        "read": {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
        "grep": {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
    }
    manager = _chat_manager_for_parallel_test(capabilities, {"read": 0.06, "grep": 0.01})

    async def run() -> tuple[list[dict], float]:
        start = perf_counter()
        messages = await manager._execute_tool_calls(
            [_tool_call("read", "call-1"), _tool_call("grep", "call-2")],
            node_id="node-1",
            conversation_id="conv-1",
        )
        return messages, perf_counter() - start

    messages, elapsed = asyncio.run(run())

    assert [message["name"] for message in messages] == ["read", "grep"]
    assert elapsed < 0.1


def test_chat_manager_serial_tool_is_a_barrier_between_parallel_waves():
    capabilities = {
        "read": {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
        "edit": {ToolCapability.MUTATES_WORKSPACE},
        "grep": {ToolCapability.READ_ONLY, ToolCapability.PARALLEL_SAFE},
    }
    events: list[str] = []
    manager = _chat_manager_for_parallel_test(
        capabilities,
        {"read": 0.04, "edit": 0.01, "grep": 0.01},
        events,
    )

    asyncio.run(manager._execute_tool_calls(
        [_tool_call("read"), _tool_call("edit"), _tool_call("grep")],
        node_id="node-1",
        conversation_id="conv-1",
    ))

    assert events.index("start:edit") > events.index("end:read")
    assert events.index("start:grep") > events.index("end:edit")


def test_single_tool_call_does_not_emit_running_heartbeat_for_short_tool():
    manager = ChatManager.__new__(ChatManager)
    manager.tool_manager = _ImmediateToolManager()
    manager.chat_repository = None
    manager.tool_orchestrator = None
    events: list[dict] = []

    async def collect(event: dict) -> None:
        events.append(event)

    asyncio.run(manager._execute_single_tool_call(
        _tool_call("grep", "call-fast"),
        node_id="node-1",
        conversation_id="conv-1",
        emit_event=collect,
        run_context={"run_id": "run-1"},
    ))

    progress_phases = [
        ((event.get("tool_call") or {}).get("progress") or {}).get("phase")
        for event in events
        if event.get("event_type") == "tool_progress"
    ]
    assert "started" in progress_phases
    assert "running" not in progress_phases
