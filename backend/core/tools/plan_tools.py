from __future__ import annotations

import json
from typing import Any, Dict, Optional

from backend.core.plans import PlanLedger

from .base import BaseTool


PLAN_TOOL_NAMES = {
    "enter_plan_mode",
    "ask_user_question",
    "exit_plan_mode",
}


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _runtime_context(kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = kwargs.get("_runtime_context")
    return value if isinstance(value, dict) else None


def _conversation_id(context: Dict[str, Any]) -> str:
    return str(context.get("conversation_id") or "")


def _missing_context_error() -> str:
    return _json({
        "error": {
            "type": "missing_runtime_context",
            "message": "This tool must be called from an active ChatTree conversation run.",
        }
    })


def _invalid_arguments(message: str) -> str:
    return _json({"error": {"type": "invalid_arguments", "message": message}})


class PlanLedgerTool(BaseTool):
    def __init__(self, plan_ledger: PlanLedger) -> None:
        self._plan_ledger = plan_ledger

    def _context(self, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return _runtime_context(kwargs)


class EnterPlanModeTool(PlanLedgerTool):
    @property
    def name(self) -> str:
        return "enter_plan_mode"

    @property
    def description(self) -> str:
        return (
            "Enter read-only plan mode only when the user explicitly asks to plan or explore before "
            "implementation, or when the implementation approach has genuine ambiguity and user "
            "sign-off would prevent significant rework. Do not use plan mode for clear "
            "implementation work, small fixes, obvious bug fixes, or tasks where the user asked you "
            "to implement now."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        conversation_id = _conversation_id(context)
        if not conversation_id:
            return _invalid_arguments("conversation_id is required")
        try:
            session = await self._plan_ledger.enter_plan_mode(
                conversation_id=conversation_id,
                node_id=str(context.get("node_id") or "") or None,
                previous_permission_mode=str(context.get("permission_mode") or "modify_only"),
                run_id=str(context.get("run_id") or "") or None,
            )
        except ValueError as exc:
            return _invalid_arguments(str(exc))
        return _json({
            "plan_id": session.plan_id,
            "status": session.status.value,
            "permission_mode": "plan",
            "previous_permission_mode": session.previous_permission_mode,
            "message": (
                "Entered plan mode. You are now in a read-only planning phase.\n"
                "Explore the codebase, identify existing patterns, compare viable approaches, and "
                "design a concrete implementation strategy. Do not edit files, run implementation "
                "commands, change configuration, commit, or claim changes were made.\n"
                "Use ask_user_question only when a genuine user decision blocks planning."
            ),
        })


class AskUserQuestionTool(PlanLedgerTool):
    @property
    def name(self) -> str:
        return "ask_user_question"

    @property
    def description(self) -> str:
        return (
            "Ask the user one concise clarification question while in plan mode. Use only when a "
            "genuine user decision is required to continue planning."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "question": {
                    "type": "string",
                    "description": "A concise question that is necessary to continue planning.",
                },
                "options": {
                    "type": "array",
                    "description": "Optional mutually exclusive choices for the user.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["label"],
                    },
                },
            },
            "required": ["question"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        conversation_id = _conversation_id(context)
        question = str(kwargs.get("question") or "").strip()
        options = kwargs.get("options")
        if not conversation_id:
            return _invalid_arguments("conversation_id is required")
        if not question:
            return _invalid_arguments("question is required")
        if options is not None and not isinstance(options, list):
            return _invalid_arguments("options must be an array")
        try:
            tool_call_id = str(context.get("tool_call_id") or "") or None
            session = await self._plan_ledger.ask_user_question(
                conversation_id=conversation_id,
                question=question,
                options=options,
                node_id=str(context.get("node_id") or "") or None,
                run_id=str(context.get("run_id") or "") or None,
                tool_call_id=tool_call_id,
            )
        except ValueError as exc:
            return _invalid_arguments(str(exc))
        return _json({
            "plan_id": session.plan_id,
            "status": session.status.value,
            "requires_user_response": True,
            "message": "Question submitted for user clarification.",
        })


class ExitPlanModeTool(PlanLedgerTool):
    @property
    def name(self) -> str:
        return "exit_plan_mode"

    @property
    def description(self) -> str:
        return (
            "Submit the final implementation plan for user approval and leave plan mode only after "
            "the user approves it. Use this in plan mode when the plan is concrete enough to execute."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The concrete implementation plan that requires user approval.",
                },
            },
            "required": ["plan"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        conversation_id = _conversation_id(context)
        plan = str(kwargs.get("plan") or "").strip()
        if not conversation_id:
            return _invalid_arguments("conversation_id is required")
        if not plan:
            return _invalid_arguments("plan is required")
        try:
            session = await self._plan_ledger.exit_plan_mode(
                conversation_id=conversation_id,
                node_id=str(context.get("node_id") or "") or None,
                run_id=str(context.get("run_id") or "") or None,
                tool_call_id=str(context.get("tool_call_id") or "") or None,
            )
        except ValueError as exc:
            return _invalid_arguments(str(exc))
        return _json({
            "plan_id": session.plan_id,
            "status": session.status.value,
            "requires_user_response": True,
            "message": "Plan submitted for user approval.",
        })


def register_plan_tools(tool_manager: Any, plan_ledger: PlanLedger) -> None:
    register = getattr(tool_manager, "register", None)
    if not callable(register):
        return
    for tool in (
        EnterPlanModeTool(plan_ledger),
        AskUserQuestionTool(plan_ledger),
        ExitPlanModeTool(plan_ledger),
    ):
        register(tool)
