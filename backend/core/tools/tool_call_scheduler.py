from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Sequence

from .security.capabilities import ToolCapability, is_parallel_safe


ToolCall = Dict[str, Any]
ToolCapabilityResolver = Callable[[str], Iterable[ToolCapability]]


@dataclass(frozen=True)
class IndexedToolCall:
    index: int
    call: ToolCall
    tool_name: str
    capabilities: frozenset[ToolCapability]


@dataclass(frozen=True)
class ToolCallWave:
    parallel: bool
    calls: tuple[IndexedToolCall, ...]


def tool_call_function_name(tool_call: ToolCall) -> str:
    fn = tool_call.get("function") or {}
    return str(fn.get("name") or "")


def plan_tool_call_waves(
    tool_calls: Sequence[ToolCall],
    capabilities_for_name: ToolCapabilityResolver,
) -> List[ToolCallWave]:
    """Group consecutive explicitly parallel-safe tool calls without reordering."""
    waves: list[ToolCallWave] = []
    pending_parallel: list[IndexedToolCall] = []

    def flush_parallel() -> None:
        nonlocal pending_parallel
        if pending_parallel:
            waves.append(ToolCallWave(parallel=True, calls=tuple(pending_parallel)))
            pending_parallel = []

    for index, tool_call in enumerate(tool_calls):
        tool_name = tool_call_function_name(tool_call)
        capabilities = frozenset(capabilities_for_name(tool_name))
        indexed = IndexedToolCall(
            index=index,
            call=tool_call,
            tool_name=tool_name,
            capabilities=capabilities,
        )
        if is_parallel_safe(capabilities):
            pending_parallel.append(indexed)
            continue

        flush_parallel()
        waves.append(ToolCallWave(parallel=False, calls=(indexed,)))

    flush_parallel()
    return waves
