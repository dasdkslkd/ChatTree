from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .base import BaseTool
from .task_contract import task_step_parameter_schema


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


def _invalid_arguments(message: str) -> str:
    return json.dumps({"error": {"type": "invalid_arguments", "message": message}}, ensure_ascii=False)


def _source_from_context(context: Dict[str, Any]) -> AgentSource:
    from backend.core.agents.types import AgentSource

    run_id = str(context.get("run_id") or context.get("node_id") or "")
    return AgentSource(
        conversation_id=str(context.get("conversation_id") or ""),
        run_id=run_id,
        run_kind=str(context.get("run_kind") or "chat"),
        anchor_node_id=context.get("anchor_node_id") or context.get("node_id"),
        root_run_id=context.get("root_run_id") or run_id,
        agent_name=context.get("agent_name"),
        task_summary=str(context.get("task_summary") or context.get("pending_user_message") or ""),
    )


def _context_permission_mode(context: Dict[str, Any]) -> Optional[str]:
    return context.get("permission_mode") or context.get("tool_permission_mode")


def _context_task_mode(context: Dict[str, Any]) -> str:
    return str(context.get("task_context_mode") or "attached")


def _context_task_generation(context: Dict[str, Any]) -> Optional[str]:
    value = str(context.get("task_generation_id") or "").strip()
    return value or None


def _context_task_revision(context: Dict[str, Any]) -> Optional[int]:
    value = context.get("task_revision")
    return int(value) if value is not None else None


class AgentRuntimeTool(BaseTool):
    def __init__(self, *, agent_runtime: Any) -> None:
        self._agent_runtime = agent_runtime

    def _context(self, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return _runtime_context(kwargs)

    def _source(self, context: Dict[str, Any]) -> AgentSource:
        return _source_from_context(context)


class AgentTool(AgentRuntimeTool):
    @property
    def name(self) -> str:
        return "agent"

    @property
    def description(self) -> str:
        return (
            "Manage delegated ChatTree agent and workflow runs. The wait action accepts agent/workflow run ids only; "
            "it cannot wait for shell command runs. Use this for explicit subagent, agent, forked-agent, or workflow "
            "requests instead of simulating delegation with shell, file tools, or prose."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["spawn", "wait", "list", "message", "input", "followup", "resume", "close", "interrupt", "workflow"],
                },
                "agent_name": {"type": "string"},
                "task": {"type": "string"},
                "context_mode": {"type": "string", "enum": ["fresh", "fork"]},
                "delivery": {"type": "string", "enum": ["auto", "notify", "silent"]},
                "run_id": {"type": "string"},
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "number"},
                "message": {"type": "string"},
                "input": {"type": "object"},
                "include_completed": {"type": "boolean"},
                "script": {"type": "string"},
                "args": {"type": "object"},
                "step": task_step_parameter_schema(),
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs) -> str:
        action = str(kwargs.get("action") or "").strip().lower()
        if action == "spawn":
            return await SpawnAgentTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "wait":
            return await WaitAgentTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "list":
            return await ListAgentsTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "message":
            return await SendMessageTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "input":
            return await SendInputTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "followup":
            return await FollowupTaskTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "resume":
            return await ResumeAgentTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "close":
            return await CloseAgentTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "interrupt":
            return await InterruptAgentTool(agent_runtime=self._agent_runtime).execute(**kwargs)
        if action == "workflow":
            context = self._context(kwargs)
            if context is None:
                return _missing_context_error()
            script = str(kwargs.get("script") or "").strip()
            if not script:
                return _invalid_arguments("script is required")
            run = await self._agent_runtime.start_workflow(
                source=self._source(context),
                script=script,
                args=kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {},
                delivery_policy=str(kwargs.get("delivery") or "auto"),
                permission_mode=_context_permission_mode(context),
                step=kwargs.get("step"),
                task_context_mode=_context_task_mode(context),
                task_generation_id=_context_task_generation(context),
                task_revision=_context_task_revision(context),
            )
            return json.dumps({
                "run_id": run.get("run_id"),
                "kind": run.get("kind", "workflow"),
                "status": run.get("status"),
                "step": run.get("step"),
                "message": "Workflow started. Its result will be delivered back to this conversation when complete.",
            }, ensure_ascii=False)
        return _invalid_arguments("action must be spawn, wait, list, message, input, followup, resume, close, interrupt, or workflow")


class SpawnAgentTool(AgentRuntimeTool):
    @property
    def name(self) -> str:
        return "spawn_agent"

    @property
    def description(self) -> str:
        return (
            "Spawn a real ChatTree subagent for delegated work. "
            "When the user explicitly asks to use a subagent, agent, or forked agent, call this tool before doing equivalent work yourself. "
            "Do not simulate a subagent with shell, file tools, or prose. "
            "Use wait_agent if your current answer depends on the delegated result."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Agent role to spawn. Common roles: explorer, planner, implementer, reviewer, verifier, workflow-worker. "
                        "Project or plugin agents may also be accepted."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "Complete delegated task brief including deliverable and constraints.",
                },
                "context_mode": {
                    "type": "string",
                    "enum": ["fresh", "fork"],
                    "description": "fresh starts from the role prompt and task; fork inherits the current conversation context.",
                },
                "delivery": {
                    "type": "string",
                    "enum": ["auto", "notify", "silent"],
                    "description": "Result delivery only: auto is default for user-visible background work; notify forces asynchronous notification; silent suppresses notification when a parent runtime will consume the result. This does not control cancellation.",
                },
                "step": task_step_parameter_schema(),
            },
            "required": ["agent_name", "task"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        task = str(kwargs.get("task") or "").strip()
        if not task:
            return _invalid_arguments("task is required")
        try:
            result = await self._agent_runtime.spawn_agent(
                source=self._source(context),
                agent_name=str(kwargs.get("agent_name") or "implementer"),
                task=task,
                context_mode=str(kwargs.get("context_mode") or "fresh"),
                delivery_policy=str(kwargs.get("delivery") or "auto"),
                created_by_run_id=context.get("run_id"),
                cancellation_parent_run_id=None,
                provider_id=context.get("provider_id"),
                model_id=context.get("model_id"),
                permission_mode=_context_permission_mode(context),
                workspace=context.get("workspace"),
                step=kwargs.get("step"),
                task_context_mode=_context_task_mode(context),
                task_generation_id=_context_task_generation(context),
                task_revision=_context_task_revision(context),
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False)


class WaitAgentTool(AgentRuntimeTool):
    @property
    def name(self) -> str:
        return "wait_agent"

    @property
    def description(self) -> str:
        return (
            "Wait for one or more spawned ChatTree agent/workflow runs and read their terminal run results. "
            "This tool accepts agent/workflow run ids only; it cannot wait for shell command runs. "
            "A wait timeout only means this wait call expired; it does not mean the run failed."
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent run ids to wait for.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Maximum wait duration in seconds for this call. Defaults to 30.",
                },
            },
            "required": ["run_ids"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        run_ids = kwargs.get("run_ids")
        if not isinstance(run_ids, list) or not run_ids:
            return _invalid_arguments("run_ids is required")
        result = await self._agent_runtime.wait_agent(
            source=self._source(context),
            run_ids=[str(run_id) for run_id in run_ids],
            timeout_seconds=float(kwargs.get("timeout_seconds") or self._agent_runtime.default_wait_timeout_seconds),
        )
        return json.dumps(result, ensure_ascii=False)


class ListAgentsTool(AgentRuntimeTool):
    @property
    def name(self) -> str:
        return "list_agents"

    @property
    def description(self) -> str:
        return "List ChatTree subagent and workflow runs visible to this conversation."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "include_completed": {"type": "boolean", "description": "Whether completed agents are included."},
            },
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        result = await self._agent_runtime.list_agents(
            conversation_id=str(context.get("conversation_id") or ""),
            include_completed=bool(kwargs.get("include_completed", True)),
        )
        return json.dumps(result, ensure_ascii=False)


class SendMessageTool(AgentRuntimeTool):
    @property
    def name(self) -> str:
        return "send_message"

    @property
    def description(self) -> str:
        return "Send a non-blocking message to an existing ChatTree agent run."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {"type": "string", "description": "Target agent run id."},
                "message": {"type": "string", "description": "Message to send."},
            },
            "required": ["run_id", "message"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        result = await self._agent_runtime.send_message(
            source=self._source(context),
            run_id=str(kwargs.get("run_id") or ""),
            message=str(kwargs.get("message") or ""),
        )
        return json.dumps(result, ensure_ascii=False)


class SendInputTool(AgentRuntimeTool):
    @property
    def name(self) -> str:
        return "send_input"

    @property
    def description(self) -> str:
        return "Send structured input to an existing ChatTree agent run."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {"type": "string", "description": "Target agent run id."},
                "input": {
                    "type": "object",
                    "description": "Structured input payload for the agent.",
                },
            },
            "required": ["run_id", "input"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        result = await self._agent_runtime.send_input(
            source=self._source(context),
            run_id=str(kwargs.get("run_id") or ""),
            input_data=kwargs.get("input"),
        )
        return json.dumps(result, ensure_ascii=False)


class FollowupTaskTool(AgentRuntimeTool):
    @property
    def name(self) -> str:
        return "followup_task"

    @property
    def description(self) -> str:
        return "Queue a follow-up task for an existing agent and let the backend deliver the result."

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {"type": "string", "description": "Target agent run id."},
                "task": {"type": "string", "description": "Follow-up task."},
            },
            "required": ["run_id", "task"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        result = await self._agent_runtime.followup_task(
            source=self._source(context),
            run_id=str(kwargs.get("run_id") or ""),
            task=str(kwargs.get("task") or ""),
        )
        return json.dumps(result, ensure_ascii=False)


class _RunControlTool(AgentRuntimeTool):
    action_name = ""

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "run_id": {"type": "string", "description": "Target agent run id."},
            },
            "required": ["run_id"],
        }

    async def execute(self, **kwargs) -> str:
        context = self._context(kwargs)
        if context is None:
            return _missing_context_error()
        method = getattr(self._agent_runtime, self.action_name)
        result = await method(run_id=str(kwargs.get("run_id") or ""))
        return json.dumps(result, ensure_ascii=False)


class ResumeAgentTool(_RunControlTool):
    action_name = "resume_agent"

    @property
    def name(self) -> str:
        return "resume_agent"

    @property
    def description(self) -> str:
        return "Resume or inspect a paused ChatTree agent run."


class CloseAgentTool(_RunControlTool):
    action_name = "close_agent"

    @property
    def name(self) -> str:
        return "close_agent"

    @property
    def description(self) -> str:
        return "Close an agent run whose result is no longer needed."


class InterruptAgentTool(_RunControlTool):
    action_name = "interrupt_agent"

    @property
    def name(self) -> str:
        return "interrupt_agent"

    @property
    def description(self) -> str:
        return "Interrupt an active ChatTree agent run."


def register_agent_management_tools(
    tool_manager: Any,
    *,
    agent_runtime: Any = None,
) -> None:
    if agent_runtime is not None:
        tool_manager.register(AgentTool(agent_runtime=agent_runtime))
