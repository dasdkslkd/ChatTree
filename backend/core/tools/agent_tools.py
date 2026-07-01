from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .base import BaseTool


def _runtime_context(kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    value = kwargs.get("_runtime_context")
    return value if isinstance(value, dict) else None


def _missing_context_error() -> str:
    return json.dumps({
        "error": {
            "type": "missing_runtime_context",
            "message": "This tool must be called from an active ChatTree conversation run.",
        }
    }, ensure_ascii=False)


class StartSubagentTool(BaseTool):
    def __init__(self, *, subagent_executor: Any) -> None:
        self._subagent_executor = subagent_executor

    @property
    def name(self) -> str:
        return "start_subagent"

    @property
    def description(self) -> str:
        return (
            "Start a background ChatTree subagent for a delegated task. "
            "Use this when the user explicitly asks to use a subagent, or when an independent worker improves coverage."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Complete delegated task brief for the subagent.",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Role agent to use. Defaults to implementer.",
                },
            },
            "required": ["task"],
        }

    async def execute(self, **kwargs) -> str:
        context = _runtime_context(kwargs)
        if context is None:
            return _missing_context_error()
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return json.dumps({"error": {"type": "invalid_arguments", "message": "task is required"}}, ensure_ascii=False)
        agent_name = str(kwargs.get("agent_name") or "implementer").strip() or "implementer"
        run = await self._subagent_executor.start(
            conversation_id=str(context.get("conversation_id") or ""),
            agent_name=agent_name,
            input_data=task,
            parent_node_id=context.get("node_id"),
            provider_id=context.get("provider_id"),
            model_id=context.get("model_id"),
            permission_mode=context.get("permission_mode"),
            workspace=context.get("workspace"),
            delegated_task=task,
            original_slash_input=None,
        )
        return json.dumps({
            "run_id": run.get("run_id"),
            "kind": run.get("kind", "subagent"),
            "status": run.get("status"),
            "agent_name": agent_name,
            "task": task,
            "message": "Subagent started. Its result will be delivered back to this conversation when complete.",
        }, ensure_ascii=False)


class StartWorkflowTool(BaseTool):
    def __init__(self, *, workflow_manager: Any) -> None:
        self._workflow_manager = workflow_manager

    @property
    def name(self) -> str:
        return "start_workflow"

    @property
    def description(self) -> str:
        return (
            "Start a background ChatTree workflow script that can coordinate subagents. "
            "Use only when the user explicitly asks for workflow-scale orchestration."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Dynamic workflow JavaScript script body to run.",
                },
                "args": {
                    "type": "object",
                    "description": "Optional workflow arguments object.",
                },
            },
            "required": ["script"],
        }

    async def execute(self, **kwargs) -> str:
        context = _runtime_context(kwargs)
        if context is None:
            return _missing_context_error()
        script = str(kwargs.get("script") or "").strip()
        if not script:
            return json.dumps({"error": {"type": "invalid_arguments", "message": "script is required"}}, ensure_ascii=False)
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        run = await self._workflow_manager.start(
            conversation_id=str(context.get("conversation_id") or ""),
            script=script,
            args=args,
            parent_node_id=context.get("node_id"),
            permission_mode=context.get("permission_mode"),
            delegated_task=script,
            original_slash_input=None,
        )
        return json.dumps({
            "run_id": run.get("run_id"),
            "kind": run.get("kind", "workflow"),
            "status": run.get("status"),
            "message": "Workflow started. Its result will be delivered back to this conversation when complete.",
        }, ensure_ascii=False)


def register_agent_management_tools(
    tool_manager: Any,
    *,
    subagent_executor: Any = None,
    workflow_manager: Any = None,
) -> None:
    if subagent_executor is not None:
        tool_manager.register(StartSubagentTool(subagent_executor=subagent_executor))
    if workflow_manager is not None:
        tool_manager.register(StartWorkflowTool(workflow_manager=workflow_manager))
