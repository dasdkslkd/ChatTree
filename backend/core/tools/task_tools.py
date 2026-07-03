from __future__ import annotations

import json
from typing import Any, Dict, Optional

from backend.core.tasks import TaskLedger, TaskNotFoundError, TaskOwnerType, TaskStatus

from .base import BaseTool


TASK_TOOL_NAMES = {"create_task", "update_task", "list_tasks"}


def _runtime_context(kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = kwargs.get("_runtime_context")
    return value if isinstance(value, dict) else None


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _missing_context_error() -> str:
    return _json({
        "error": {
            "type": "missing_runtime_context",
            "message": "This tool must be called from an active ChatTree conversation run.",
        }
    })


def _invalid_arguments(message: str) -> str:
    return _json({"error": {"type": "invalid_arguments", "message": message}})


def _not_found(task_id: str) -> str:
    return _json({"error": {"type": "task_not_found", "message": f"Task {task_id} was not found."}})


def _conversation_id(context: Dict[str, Any]) -> str:
    return str(context.get("conversation_id") or "")


def _run_id(context: Dict[str, Any]) -> Optional[str]:
    value = str(context.get("run_id") or "")
    return value or None


def _owner_type_from_context(context: Dict[str, Any]) -> TaskOwnerType:
    run_kind = str(context.get("run_kind") or "").lower()
    if run_kind in {TaskOwnerType.SUBAGENT.value, TaskOwnerType.WORKFLOW.value, TaskOwnerType.COMMAND.value}:
        return TaskOwnerType(run_kind)
    return TaskOwnerType.ASSISTANT


def _coerce_status(value: Any) -> TaskStatus:
    return value if isinstance(value, TaskStatus) else TaskStatus(str(value))


class TaskLedgerTool(BaseTool):
    def __init__(self, task_ledger: TaskLedger) -> None:
        self._task_ledger = task_ledger

    def _context(self, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return _runtime_context(kwargs)


class CreateTaskTool(TaskLedgerTool):
    @property
    def name(self) -> str:
        return "create_task"

    @property
    def description(self) -> str:
        return "Create a user-visible task in the ChatTree TaskLedger before delegating or committing to multi-step work."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "description": "Short user-visible task title."},
                "detail": {"type": "string", "description": "Optional longer task detail."},
            },
            "required": ["title"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        conversation_id = _conversation_id(context)
        title = str(kwargs.get("title") or "").strip()
        if not conversation_id:
            return _invalid_arguments("conversation_id is required")
        if not title:
            return _invalid_arguments("title is required")
        try:
            task = await self._task_ledger.create_task(
                conversation_id=conversation_id,
                title=title,
                detail=str(kwargs.get("detail") or ""),
                created_by_run_id=_run_id(context),
                owner_type=_owner_type_from_context(context),
                metadata={
                    "anchor_node_id": context.get("anchor_node_id") or context.get("node_id"),
                    "node_id": context.get("node_id"),
                    "tool_call_id": context.get("tool_call_id"),
                    "source_run_id": context.get("run_id"),
                    "source_run_kind": context.get("run_kind"),
                },
            )
        except ValueError as exc:
            return _invalid_arguments(str(exc))
        return _json({"task_id": task.task_id, "status": task.status.value, "task": task.to_dict()})


class UpdateTaskTool(TaskLedgerTool):
    @property
    def name(self) -> str:
        return "update_task"

    @property
    def description(self) -> str:
        return "Update a TaskLedger task status, title, detail, or evidence before final response."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": "string", "description": "Task id returned by create_task or list_tasks."},
                "status": {
                    "type": "string",
                    "enum": [status.value for status in TaskStatus],
                    "description": "New task status.",
                },
                "evidence_summary": {
                    "type": "string",
                    "description": "Short evidence or blocked reason. Required when status is completed or blocked.",
                },
                "title": {"type": "string", "description": "Optional replacement title."},
                "detail": {"type": "string", "description": "Optional replacement detail."},
            },
            "required": ["task_id"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        conversation_id = _conversation_id(context)
        task_id = str(kwargs.get("task_id") or "").strip()
        if not conversation_id:
            return _invalid_arguments("conversation_id is required")
        if not task_id:
            return _invalid_arguments("task_id is required")
        try:
            status = _coerce_status(kwargs.get("status")) if kwargs.get("status") is not None else None
        except ValueError:
            return _invalid_arguments("status must be one of pending, in_progress, completed, blocked, cancelled")
        evidence_summary = kwargs.get("evidence_summary")
        if status in {TaskStatus.COMPLETED, TaskStatus.BLOCKED} and not str(evidence_summary or "").strip():
            return _invalid_arguments("evidence_summary is required when completing or blocking a task")
        try:
            task = await self._task_ledger.update_task(
                conversation_id=conversation_id,
                task_id=task_id,
                status=status,
                title=str(kwargs["title"]) if "title" in kwargs else None,
                detail=str(kwargs["detail"]) if "detail" in kwargs else None,
                evidence_run_id=_run_id(context) if evidence_summary is not None or status is not None else None,
                evidence_summary=str(evidence_summary) if evidence_summary is not None else None,
            )
        except TaskNotFoundError:
            return _not_found(task_id)
        except ValueError as exc:
            return _invalid_arguments(str(exc))
        return _json({"task_id": task.task_id, "status": task.status.value, "task": task.to_dict()})


class ListTasksTool(TaskLedgerTool):
    @property
    def name(self) -> str:
        return "list_tasks"

    @property
    def description(self) -> str:
        return "List TaskLedger tasks for the active conversation, optionally filtered by status."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "statuses": {
                    "type": "array",
                    "items": {"type": "string", "enum": [status.value for status in TaskStatus]},
                    "description": "Optional status filters.",
                },
                "include_finished": {
                    "type": "boolean",
                    "description": "Whether completed and cancelled tasks are included. Defaults to true.",
                },
            },
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        conversation_id = _conversation_id(context)
        if not conversation_id:
            return _invalid_arguments("conversation_id is required")
        raw_statuses = kwargs.get("statuses")
        statuses = None
        if raw_statuses is not None:
            if not isinstance(raw_statuses, list):
                return _invalid_arguments("statuses must be a list")
            try:
                statuses = [_coerce_status(value) for value in raw_statuses]
            except ValueError:
                return _invalid_arguments("statuses contains an invalid task status")
        tasks = await self._task_ledger.list_tasks(
            conversation_id,
            statuses=statuses,
            include_finished=bool(kwargs.get("include_finished", True)),
        )
        return _json({"tasks": [task.to_dict() for task in tasks]})


def register_task_tools(tool_manager: Any, task_ledger: TaskLedger) -> None:
    register = getattr(tool_manager, "register", None)
    if not callable(register):
        return
    for tool in (
        CreateTaskTool(task_ledger),
        UpdateTaskTool(task_ledger),
        ListTasksTool(task_ledger),
    ):
        register(tool)
