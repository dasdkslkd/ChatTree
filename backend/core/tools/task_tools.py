from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Dict, Optional

from backend.core.tasks import (
    ActiveTaskConflictError,
    ActiveTaskNotFoundError,
    ActiveTaskService,
    TaskContextMode,
    TaskLifecycleStatus,
    TaskOutcome,
    TaskStateSnapshot,
    TaskStepStatus,
    normalize_context_mode,
)

from .base import BaseTool
from .task_contract import SET_TASK_STEP_DESCRIPTION


TASK_TOOL_NAMES = {"create_task", "set_task_step", "cancel_task"}
TASK_BOUND_RUN_TOOL_NAMES = {
    "shell",
    "spawn_agent",
    "start_subagent",
    "start_workflow",
}
TASK_OBSERVATION_TOOL_NAMES = {
    "list_agents",
    "wait_agent",
}


def filter_task_tools_for_context(
    tools: list[Dict[str, Any]],
    mode: TaskContextMode | str,
    *,
    has_active_task: bool,
) -> list[Dict[str, Any]]:
    attached = normalize_context_mode(mode) == TaskContextMode.ATTACHED
    filtered: list[Dict[str, Any]] = []
    for tool in tools:
        name = str((tool.get("function") or {}).get("name") or "")
        if name in TASK_TOOL_NAMES:
            if not attached:
                continue
            if has_active_task and name == "create_task":
                continue
            if not has_active_task and name != "create_task":
                continue
        if name not in TASK_BOUND_RUN_TOOL_NAMES or (attached and has_active_task):
            filtered.append(tool)
            continue
        filtered_tool = deepcopy(tool)
        parameters = (filtered_tool.get("function") or {}).get("parameters") or {}
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            properties.pop("step", None)
        required = parameters.get("required")
        if isinstance(required, list):
            parameters["required"] = [item for item in required if item != "step"]
        filtered.append(filtered_tool)
    return filtered


def _runtime_context(kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = kwargs.get("_runtime_context")
    return value if isinstance(value, dict) else None


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(error_type: str, message: str) -> str:
    return _json({"error": {"type": error_type, "message": message}})


def _task_outcome_dict(outcome: TaskOutcome) -> dict[str, Any]:
    return outcome.public_dict()


class ActiveTaskTool(BaseTool):
    def __init__(self, task_service: ActiveTaskService) -> None:
        self._task_service = task_service

    def _context(self, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return _runtime_context(kwargs)

    def _validate_context(self, context: Optional[Dict[str, Any]]) -> Optional[str]:
        if context is None:
            return _error(
                "missing_runtime_context",
                "This tool must be called from an active ChatTree conversation run.",
            )
        if not str(context.get("conversation_id") or ""):
            return _error("invalid_arguments", "conversation_id is required")
        try:
            mode = normalize_context_mode(context.get("task_context_mode"))
        except ValueError:
            return _error("invalid_runtime_context", "invalid task context mode")
        if mode != TaskContextMode.ATTACHED:
            return _error("task_context_disabled", "Task context is detached for this branch.")
        return None

    def _validate_task_version(self, context: Dict[str, Any]) -> Optional[str]:
        if not str(context.get("task_generation_id") or ""):
            return _error("task_context_stale", "The active task was not present in this model context.")
        if context.get("task_revision") is None:
            return _error("task_context_stale", "The active task revision is missing from this model context.")
        return None


class CreateTaskTool(ActiveTaskTool):
    @property
    def name(self) -> str:
        return "create_task"

    @property
    def description(self) -> str:
        return "Create the conversation's single active task with ordered steps."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "description": "Short task title."},
                "detail": {"type": "string", "description": "Optional task detail."},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "description": "Ordered task steps.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "detail": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["title", "steps"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        invalid = self._validate_context(context)
        if invalid:
            return invalid
        assert context is not None
        try:
            task = await self._task_service.create_task(
                conversation_id=str(context["conversation_id"]),
                title=str(kwargs.get("title") or ""),
                detail=str(kwargs.get("detail") or ""),
                steps=kwargs.get("steps") if isinstance(kwargs.get("steps"), list) else [],
                created_by_run_id=str(context.get("run_id") or "") or None,
                tool_call_id=str(context.get("tool_call_id") or "") or None,
            )
        except ValueError as exc:
            return _error("invalid_arguments", str(exc))
        except ActiveTaskConflictError as exc:
            return _error("active_task_exists", str(exc))
        return _json({
            "status": "active",
            "task": task.public_dict(),
            "task_outcome": _task_outcome_dict(TaskOutcome(
                kind="task_created",
                task_status=TaskLifecycleStatus.ACTIVE,
                task_snapshot=TaskStateSnapshot.from_task(task),
            )),
        })


class SetTaskStepTool(ActiveTaskTool):
    @property
    def name(self) -> str:
        return "set_task_step"

    @property
    def description(self) -> str:
        return SET_TASK_STEP_DESCRIPTION

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "step": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "One-based step number shown in the active task.",
                },
                "status": {
                    "type": "string",
                    "enum": [TaskStepStatus.COMPLETED.value, TaskStepStatus.BLOCKED.value],
                },
                "evidence": {
                    "type": "string",
                    "description": "Concise completion evidence or blocking reason.",
                },
            },
            "required": ["step", "status", "evidence"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        invalid = self._validate_context(context)
        if invalid:
            return invalid
        assert context is not None
        stale = self._validate_task_version(context)
        if stale:
            return stale
        try:
            result = await self._task_service.set_step_result(
                conversation_id=str(context["conversation_id"]),
                step=kwargs.get("step"),
                status=str(kwargs.get("status") or ""),
                evidence_summary=str(kwargs.get("evidence") or ""),
                evidence_run_id=str(context.get("run_id") or "") or None,
                expected_generation=str(context.get("task_generation_id") or "") or None,
                expected_revision=(
                    int(context["task_revision"])
                    if context.get("task_revision") is not None
                    else None
                ),
            )
        except ActiveTaskNotFoundError as exc:
            return _error("active_task_not_found", str(exc))
        except ActiveTaskConflictError as exc:
            return _error("task_context_stale", str(exc))
        except ValueError as exc:
            return _error("invalid_arguments", str(exc))
        return _json(result.public_dict())


class CancelTaskTool(ActiveTaskTool):
    @property
    def name(self) -> str:
        return "cancel_task"

    @property
    def description(self) -> str:
        return "Cancel and remove the conversation's active task."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the task is being abandoned.",
                },
            },
            "required": ["reason"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        invalid = self._validate_context(context)
        if invalid:
            return invalid
        assert context is not None
        stale = self._validate_task_version(context)
        if stale:
            return stale
        current_task = await self._task_service.get_active_task(str(context["conversation_id"]))
        try:
            cancelled = await self._task_service.cancel_task(
                conversation_id=str(context["conversation_id"]),
                reason=str(kwargs.get("reason") or ""),
                expected_generation=str(context.get("task_generation_id") or "") or None,
                expected_revision=(
                    int(context["task_revision"])
                    if context.get("task_revision") is not None
                    else None
                ),
            )
        except ActiveTaskNotFoundError as exc:
            return _error("active_task_not_found", str(exc))
        except ActiveTaskConflictError as exc:
            return _error("task_context_stale", str(exc))
        except ValueError as exc:
            return _error("invalid_arguments", str(exc))
        return _json({
            "cancelled": cancelled,
            "task": None,
            "task_outcome": _task_outcome_dict(TaskOutcome(
                kind="task_cancelled",
                task_status=TaskLifecycleStatus.CANCELLED,
                task_snapshot=(
                    TaskStateSnapshot.from_task(current_task)
                    if current_task is not None
                    else None
                ),
            )),
        })


def register_task_tools(tool_manager: Any, task_service: ActiveTaskService) -> None:
    register = getattr(tool_manager, "register", None)
    if not callable(register):
        return
    for tool in (
        CreateTaskTool(task_service),
        SetTaskStepTool(task_service),
        CancelTaskTool(task_service),
    ):
        register(tool)
