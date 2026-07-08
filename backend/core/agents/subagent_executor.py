from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, Dict, Optional

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import Message, Role, StreamController, StreamStatus
from backend.core.prompts import PromptBuilder, PromptBuildRequest
from backend.core.prompts.catalog import load_prompt_template
from backend.core.prompts.types import RuntimePromptContext
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.tools.security.permissions import normalize_permission_mode
from .types import AgentDeliveryPolicy


DEFAULT_MAX_TOOL_ROUNDS = 500
DEFAULT_MAX_TURNS = 1000


class SubagentExecutor:
    def __init__(
        self,
        *,
        chat_manager: ChatManager,
        run_manager: RunManager,
        capability_registry: CapabilityRegistry,
        mailbox: Any = None,
    ) -> None:
        self.chat_manager = chat_manager
        self.run_manager = run_manager
        self.capability_registry = capability_registry
        self.mailbox = mailbox
        self._controllers: dict[str, StreamController] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(
        self,
        *,
        conversation_id: str,
        agent_name: str,
        input_data: Any,
        parent_node_id: Optional[str] = None,
        created_by_run_id: Optional[str] = None,
        cancellation_parent_run_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        permission_mode: Optional[str] = None,
        workspace: Optional[Dict[str, Any]] = None,
        delegated_task: Any = None,
        original_slash_input: Optional[str] = None,
        delivery_policy: str = "auto",
        context_mode: str = "fresh",
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        delivery_policy = AgentDeliveryPolicy(str(delivery_policy or "auto")).value
        agent = self.capability_registry.get_agent(agent_name)
        if agent is None:
            raise KeyError(agent_name)
        self._validate_schema(agent.input_schema, input_data, "input_schema")

        summary = f"{agent.name}: {str(input_data)[:80]}"
        run = await self.run_manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.SUBAGENT,
            anchor_node_id=parent_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            summary=summary,
            metadata={
                "agent_name": agent.name,
                "provider_id": provider_id or agent.provider_id,
                "model_id": model_id or agent.model_id or agent.model,
                "permission_mode": permission_mode or agent.permission_mode,
                "delegated_task": delegated_task if delegated_task is not None else input_data,
                "original_slash_input": original_slash_input,
                "delivery_policy": delivery_policy,
                "context_mode": context_mode if context_mode in {"fresh", "fork"} else "fresh",
                "task_id": task_id,
            },
        )
        await self._register_task_notification(run.run_id, agent_name=agent.name)
        task = asyncio.create_task(self._produce(
            run_id=run.run_id,
            conversation_id=conversation_id,
            agent_name=agent_name,
            input_data=input_data,
            parent_node_id=parent_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            provider_id=provider_id,
            model_id=model_id,
            permission_mode=permission_mode,
            workspace=workspace,
            context_mode=context_mode,
        ))
        self._tasks[run.run_id] = task
        return run.to_dict()

    async def _register_task_notification(self, run_id: str, *, agent_name: str) -> None:
        run = self.run_manager.get_run(run_id)
        if not run:
            return
        metadata = dict(run.get("metadata") or {})
        if str(metadata.get("delivery_policy") or "auto") == "silent":
            return
        created_by_run_id = run.get("created_by_run_id")
        parent_run = self.run_manager.get_run(str(created_by_run_id)) if created_by_run_id else None
        parent_kind = str((parent_run or {}).get("kind") or "")
        if created_by_run_id and (
            parent_kind in {RunKind.WORKFLOW.value, RunKind.WORKFLOW_STEP.value}
            or agent_name == "workflow-worker"
        ):
            return
        service = getattr(self.run_manager, "notification_service", None)
        if service is None:
            return
        await service.register_run_notification(
            run_id=run_id,
            summary=f"Subagent {agent_name} running",
            payload={
                "agent_name": agent_name,
                "delegated_task": metadata.get("delegated_task"),
                "original_slash_input": metadata.get("original_slash_input"),
                "task_id": metadata.get("task_id"),
            },
            task_id=metadata.get("task_id"),
        )

    async def stop(self, run_id: str) -> bool:
        await self.run_manager.request_stop(run_id)
        controller = self._controllers.get(run_id)
        if controller:
            await controller.stop()
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            return True
        if controller:
            return True
        return False

    async def _produce(
        self,
        *,
        run_id: str,
        conversation_id: str,
        agent_name: str,
        input_data: Any,
        parent_node_id: Optional[str],
        created_by_run_id: Optional[str],
        cancellation_parent_run_id: Optional[str],
        provider_id: Optional[str],
        model_id: Optional[str],
        permission_mode: Optional[str],
        workspace: Optional[Dict[str, Any]],
        context_mode: str = "fresh",
    ) -> None:
        final_status = RunStatus.COMPLETED
        final_error: Optional[str] = None
        notification_payload: Optional[Dict[str, Any]] = None
        try:
            agent = self.capability_registry.get_agent(agent_name)
            if agent is None:
                raise KeyError(agent_name)
            if agent.timeout_seconds:
                notification_payload = await asyncio.wait_for(
                    self._produce_inner(
                        run_id=run_id,
                        conversation_id=conversation_id,
                        agent_name=agent_name,
                        agent=agent,
                        input_data=input_data,
                        parent_node_id=parent_node_id,
                        provider_id=provider_id,
                        model_id=model_id,
                        permission_mode=permission_mode,
                        workspace=workspace,
                        context_mode=context_mode,
                    ),
                    timeout=agent.timeout_seconds,
                )
            else:
                notification_payload = await self._produce_inner(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    agent_name=agent_name,
                    agent=agent,
                    input_data=input_data,
                    parent_node_id=parent_node_id,
                    provider_id=provider_id,
                    model_id=model_id,
                    permission_mode=permission_mode,
                    workspace=workspace,
                    context_mode=context_mode,
                )
        except asyncio.CancelledError:
            final_status = RunStatus.CANCELLED
            await self.run_manager.append_event(run_id, {
                "status": "stopped",
                "event_type": "subagent_result",
                "agent_name": agent_name,
                "content": "",
                "reasoning": None,
            })
        except asyncio.TimeoutError:
            final_status = RunStatus.FAILED
            agent = self.capability_registry.get_agent(agent_name)
            final_error = f"subagent timeout after {agent.timeout_seconds if agent else 'configured'} seconds"
            notification_payload = {
                "status": "error",
                "event_type": "subagent_error",
                "agent_name": agent_name,
                "content": "",
                "error": final_error,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        except Exception as exc:
            final_status = RunStatus.FAILED
            final_error = str(exc) or exc.__class__.__name__
            notification_payload = {
                "status": "error",
                "event_type": "subagent_error",
                "agent_name": agent_name,
                "content": "",
                "error": final_error,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        finally:
            self._controllers.pop(run_id, None)
            self._tasks.pop(run_id, None)
            await self.run_manager.finish_run(run_id, final_status, final_error)
            if notification_payload and final_status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                source_status = "completed" if final_status == RunStatus.COMPLETED else "failed"
                await self._publish_task_notification(
                    run_id,
                    source_status,
                    notification_payload.get("content") or notification_payload.get("error") or "",
                    event_payload=notification_payload,
                )

    async def _produce_inner(
        self,
        *,
        run_id: str,
        conversation_id: str,
        agent_name: str,
        agent,
        input_data: Any,
        parent_node_id: Optional[str],
        provider_id: Optional[str],
        model_id: Optional[str],
        permission_mode: Optional[str],
        workspace: Optional[Dict[str, Any]],
        context_mode: str = "fresh",
    ) -> Dict[str, Any]:
            conversation = self.chat_manager.get_conversation(conversation_id)
            if conversation is None:
                raise ValueError("对话不存在")

            target_provider = provider_id or agent.provider_id or conversation.metadata.get("provider_id") or conversation.current_provider
            target_model = model_id or agent.model_id or agent.model or conversation.metadata.get("model_id") or conversation.current_model
            if not target_model:
                for candidate_provider, models in self.chat_manager.model_manager.model_list.items():
                    if models:
                        target_provider = target_provider or candidate_provider
                        target_model = models[0]
                        break
            if target_model and not target_provider:
                target_provider = self.chat_manager._provider_for_model(target_model)
            if not target_provider or not target_model:
                raise ValueError("无法确定 subagent 模型")

            provider = self.chat_manager.model_manager.get_model(target_provider, True)
            if provider is None:
                raise ValueError(f"无法初始化提供商 {target_provider}")

            messages = self._build_messages(
                agent_name,
                input_data,
                parent_node_id,
                conversation=conversation,
                context_mode=context_mode,
            )
            tools = self._filter_tools(agent.tools)
            permission = normalize_permission_mode(permission_mode or agent.permission_mode)
            max_tool_rounds = agent.max_tool_rounds or DEFAULT_MAX_TOOL_ROUNDS
            max_turns = agent.max_turns or DEFAULT_MAX_TURNS
            controller = StreamController(run_id, conversation_id, run_id=run_id)
            self._controllers[run_id] = controller

            await self.run_manager.append_event(run_id, {
                "status": "start",
                "event_type": "run_start",
                "agent_name": agent_name,
                "provider_id": target_provider,
                "model_id": target_model,
                "content": None,
            })

            total_content = ""
            total_reasoning = ""
            tool_round = 0
            model_turn = 0
            while True:
                if model_turn >= max_turns:
                    raise RuntimeError(f"max_turns exceeded: {max_turns}")
                model_turn += 1
                round_content = ""
                round_reasoning = ""
                round_tool_calls: list[dict[str, Any]] = []
                complete_seen = False

                async for chunk in provider.generate_response_stream(
                    model=target_model,
                    messages=messages,
                    stream_controller=controller,
                    tools=tools or None,
                    tool_choice="auto" if tools else None,
                ):
                    if await self.run_manager.is_stop_requested(run_id):
                        await controller.stop()
                    if chunk.get("reasoning"):
                        text = chunk.get("reasoning") or ""
                        total_reasoning += text
                        round_reasoning += text
                    if chunk.get("content"):
                        text = chunk.get("content") or ""
                        total_content += text
                        round_content += text
                    if chunk.get("tool_calls"):
                        round_tool_calls = self.chat_manager._merge_tool_call_lists(
                            round_tool_calls,
                            chunk.get("tool_calls") or [],
                        )
                    elif chunk.get("tool_call", {}).get("tool_calls"):
                        round_tool_calls = self.chat_manager._merge_tool_call_lists(
                            round_tool_calls,
                            chunk.get("tool_call", {}).get("tool_calls") or [],
                        )

                    status = chunk.get("status")
                    if status == StreamStatus.COMPLETE:
                        complete_seen = True
                        continue
                    if status == StreamStatus.ERROR:
                        raise RuntimeError(chunk.get("error") or "subagent provider error")
                    elif status == StreamStatus.STOPPED:
                        raise asyncio.CancelledError()
                    payload = dict(chunk)
                    payload.update({
                        "run_id": run_id,
                        "conversation_id": conversation_id,
                        "agent_name": agent_name,
                        "target_node_id": None,
                    })
                    await self.run_manager.append_event(run_id, payload)

                if not round_tool_calls:
                    if complete_seen:
                        await self.run_manager.append_event(run_id, {
                            "status": "complete",
                            "event_type": "run_complete",
                            "content": "",
                            "agent_name": agent_name,
                        })
                    break
                if tool_round >= max_tool_rounds:
                    raise RuntimeError(f"工具调用轮数超过上限 {max_tool_rounds}")
                tool_round += 1
                assistant_tool_message = {
                    "role": "assistant",
                    "content": round_content,
                    "tool_calls": round_tool_calls,
                }
                messages.append(assistant_tool_message)
                approval_events: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
                run_snapshot = self.run_manager.get_run(run_id) or {}
                run_metadata = dict(run_snapshot.get("metadata") or {})
                tool_node_id = str(
                    parent_node_id
                    or run_snapshot.get("target_node_id")
                    or run_snapshot.get("anchor_node_id")
                    or run_id
                )

                async def emit_tool_event(event: Dict[str, Any]):
                    event = dict(event)
                    event["run_id"] = run_id
                    event["agent_name"] = agent_name
                    if event.get("event_type") == "tool_approval_request":
                        await self.run_manager.update_status(run_id, RunStatus.WAITING_APPROVAL)
                        event["status"] = RunStatus.WAITING_APPROVAL.value
                    elif event.get("event_type") == "tool_approval_result":
                        if not await self.run_manager.is_stop_requested(run_id):
                            await self.run_manager.update_status(run_id, RunStatus.RUNNING)
                        event["status"] = RunStatus.RUNNING.value
                    await approval_events.put(event)

                execute_task = asyncio.create_task(
                    self.chat_manager._execute_tool_calls(
                        round_tool_calls,
                        node_id=tool_node_id,
                        conversation_id=conversation_id,
                        emit_event=emit_tool_event,
                        workspace=workspace or conversation.metadata.get("workspace"),
                        permission_mode=permission or "default",
                        run_context={
                            "run_id": run_id,
                            "run_kind": RunKind.SUBAGENT.value,
                            "created_by_run_id": run_snapshot.get("created_by_run_id"),
                            "cancellation_parent_run_id": run_snapshot.get("cancellation_parent_run_id"),
                            "root_run_id": run_metadata.get("root_run_id") or run_id,
                            "conversation_id": conversation_id,
                            "anchor_node_id": tool_node_id,
                            "node_id": tool_node_id,
                            "agent_name": agent_name,
                            "delivery_policy": run_metadata.get("delivery_policy"),
                            "task_id": run_metadata.get("task_id"),
                            "task_summary": str(input_data)[:160],
                            "suppress_task_notification": agent_name == "workflow-worker"
                            or run_metadata.get("delivery_policy") == "silent",
                        },
                    )
                )
                event_get_task = asyncio.create_task(approval_events.get())
                stop_task = asyncio.create_task(self.run_manager.stop_event(run_id).wait())
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {execute_task, event_get_task, stop_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done and stop_task.result():
                            execute_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await execute_task
                            raise asyncio.CancelledError()
                        if event_get_task in done:
                            event = event_get_task.result()
                            await self.run_manager.append_event(run_id, self._tool_event_payload(event, agent_name))
                            event_get_task = asyncio.create_task(approval_events.get())
                        if execute_task in done:
                            while not approval_events.empty():
                                event = approval_events.get_nowait()
                                await self.run_manager.append_event(run_id, self._tool_event_payload(event, agent_name))
                            tool_messages = await execute_task
                            break
                finally:
                    if not event_get_task.done():
                        event_get_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await event_get_task
                    if not stop_task.done():
                        stop_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await stop_task
                    if not execute_task.done():
                        execute_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await execute_task
                model_tool_messages = self.chat_manager._apply_round_tool_result_budget(tool_messages)
                messages.extend(model_tool_messages)
                for tool_msg in tool_messages:
                    await self.run_manager.append_event(run_id, {
                        "status": "content",
                        "event_type": "tool_result",
                        "content": None,
                        "agent_name": agent_name,
                        "tool_call": {
                            "tool_call_id": tool_msg.get("tool_call_id"),
                            "name": tool_msg.get("name"),
                            "content": tool_msg.get("content"),
                            "raw_content": tool_msg.get("raw_content"),
                            "model_visible_content": tool_msg.get("model_visible_content"),
                            "tool_result_id": tool_msg.get("tool_result_id"),
                        },
                    })
            self._validate_output_schema(agent.output_schema, total_content)
            result_payload = {
                "status": "complete",
                "event_type": "subagent_result",
                "agent_name": agent_name,
                "content": total_content,
                "reasoning": total_reasoning or None,
            }
            await self.run_manager.append_event(run_id, result_payload)
            return result_payload

    def _build_messages(
        self,
        agent_name: str,
        input_data: Any,
        parent_node_id: Optional[str],
        *,
        conversation: Any = None,
        context_mode: str = "fresh",
    ) -> list[Message]:
        agent = self.capability_registry.get_agent(agent_name)
        if agent is None:
            raise KeyError(agent_name)
        is_workflow_worker = agent.name == "workflow-worker" or agent.metadata.get("runtime") == "workflow"
        has_workflow_worker_prompt = "Workflow Worker Agent Prompt" in (agent.system_prompt or "")
        system_parts = [load_prompt_template("fork")]
        if is_workflow_worker and not has_workflow_worker_prompt:
            system_parts.append(load_prompt_template("agent:workflow-worker"))
        system_parts.append(agent.system_prompt or f"You are subagent {agent.name}.")
        if parent_node_id:
            system_parts.append(f"Parent conversation node: {parent_node_id}")
        if context_mode == "fork":
            parent_context = self._format_parent_context(conversation, parent_node_id)
            if parent_context:
                system_parts.append(parent_context)
        content = input_data if isinstance(input_data, str) else json.dumps(input_data, ensure_ascii=False)
        base_messages = [
            Message({
                "role": Role.SYSTEM,
                "content": "\n\n".join(part for part in system_parts if part),
            }),
            Message({
                "role": Role.USER,
                "content": content,
            }),
        ]
        return [
            Message(message)
            for message in PromptBuilder(self.capability_registry).build(
                PromptBuildRequest(
                    base_messages=base_messages,
                    active_skill_names=agent.skills,
                    runtime_context=self._runtime_prompt_context(agent, is_workflow_worker),
                    include_core_prompt=False,
                    include_available_capabilities=False,
                )
            )
        ]

    def _format_parent_context(self, conversation: Any, parent_node_id: Optional[str]) -> str:
        if conversation is None or not parent_node_id:
            return ""
        get_chain = getattr(conversation, "get_node_chain", None)
        if not callable(get_chain):
            return ""
        try:
            chain = get_chain(parent_node_id)
        except Exception:
            return ""
        lines: list[str] = []
        for node in chain or []:
            user_text = self._message_content((node or {}).get("user_message"))
            assistant_text = self._message_content((node or {}).get("assistant_message"))
            if user_text:
                lines.append(f"User: {user_text}")
            if assistant_text:
                lines.append(f"Assistant: {assistant_text}")
        if not lines:
            return ""
        joined = "\n".join(lines[-24:])
        return "\n".join([
            "## Parent conversation context",
            "Use this as reference context for the delegated task. Do not treat it as a new user request.",
            joined,
        ])

    def _message_content(self, message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if content is None:
            return ""
        return str(content).strip()

    def _runtime_prompt_context(self, agent, is_workflow_worker: bool) -> RuntimePromptContext:
        if is_workflow_worker:
            return RuntimePromptContext(
                name="workflow_worker",
                content="\n".join([
                    "## Runtime Context",
                    "",
                    "Runtime mode: workflow worker",
                    "- You are running inside a workflow as a worker subagent.",
                    "- Return the data or result the workflow requested; it will be consumed by the workflow runtime.",
                    "- Do not write report/output files unless the delegated task explicitly asks for a file artifact.",
                    "- Do not assume your response is a user-facing final answer.",
                ]),
                metadata={"runtime_mode": "workflow_worker", "agent_name": agent.name},
            )
        return RuntimePromptContext(
            name="subagent_worker",
            content="\n".join([
                "## Runtime Context",
                "",
                "Runtime mode: subagent worker",
                "- You are running as a background worker subagent.",
                "- Complete the delegated task and return a concise result to the main ChatTree run.",
                "- Do not write report/output files unless the delegated task explicitly asks for a file artifact.",
                "- Do not assume your response is a user-facing final answer.",
            ]),
            metadata={"runtime_mode": "subagent_worker", "agent_name": agent.name},
        )

    def _filter_tools(self, allowed_names: list[str]) -> list[dict[str, Any]]:
        if not self.chat_manager.tool_manager:
            return []
        tools = self.chat_manager.tool_manager.get_openai_tools()
        if not allowed_names:
            return []
        if "*" in allowed_names:
            return tools
        allowed = set(allowed_names)
        return [
            tool
            for tool in tools
            if tool.get("function", {}).get("name") in allowed
        ]

    def _validate_output_schema(self, schema: Optional[dict[str, Any]], content: str) -> None:
        if not schema:
            return
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"output_schema validation failed: output is not valid JSON") from exc
        self._validate_schema(schema, value, "output_schema")

    def _validate_schema(self, schema: Optional[dict[str, Any]], value: Any, label: str) -> None:
        if not schema:
            return
        error = self._schema_error(schema, value, label)
        if error:
            raise ValueError(error)

    def _schema_error(self, schema: dict[str, Any], value: Any, path: str) -> Optional[str]:
        expected_type = schema.get("type")
        if expected_type and not self._matches_schema_type(expected_type, value):
            return f"{path} validation failed: expected {expected_type}"

        if expected_type == "object" or "properties" in schema or "required" in schema:
            if not isinstance(value, dict):
                return f"{path} validation failed: expected object"
            for key in schema.get("required") or []:
                if key not in value:
                    return f"{path} validation failed: missing required property {key}"
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, dict):
                    child_error = self._schema_error(child_schema, value[key], f"{path}.{key}")
                    if child_error:
                        return child_error

        if expected_type == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                item_error = self._schema_error(schema["items"], item, f"{path}[{index}]")
                if item_error:
                    return item_error
        return None

    def _matches_schema_type(self, expected_type: Any, value: Any) -> bool:
        if isinstance(expected_type, list):
            return any(self._matches_schema_type(item, value) for item in expected_type)
        if expected_type == "object":
            return isinstance(value, dict)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "boolean":
            return isinstance(value, bool)
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
        if expected_type == "null":
            return value is None
        return True

    def _tool_event_payload(self, event: Dict[str, Any], agent_name: str) -> Dict[str, Any]:
        chunk = self.chat_manager._tool_event_stream_chunk(
            event,
            node_id=str(event.get("run_id") or ""),
            conversation_id=str(event.get("conversation_id") or ""),
        )
        payload = dict(chunk)
        payload["agent_name"] = agent_name
        payload["target_node_id"] = None
        return payload

    async def _publish_task_notification(
        self,
        run_id: str,
        source_status: str,
        content: str,
        *,
        event_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        run = self.run_manager.get_run(run_id)
        if not run:
            return None
        if run.get("kind") != RunKind.SUBAGENT.value:
            return None
        metadata = dict(run.get("metadata") or {})
        slash_metadata = metadata.get("slash_command") if isinstance(metadata.get("slash_command"), dict) else {}
        original_slash_input = metadata.get("original_slash_input") or slash_metadata.get("original_input")
        event_payload = dict(event_payload or {})
        delivery_policy = str(metadata.get("delivery_policy") or "auto")
        if delivery_policy == "silent":
            return None
        created_by_run_id = run.get("created_by_run_id")
        parent_run = self.run_manager.get_run(str(created_by_run_id)) if created_by_run_id else None
        parent_kind = str((parent_run or {}).get("kind") or "")
        agent_name = str(metadata.get("agent_name") or event_payload.get("agent_name") or "")
        if created_by_run_id and (
            parent_kind in {RunKind.WORKFLOW.value, RunKind.WORKFLOW_STEP.value}
            or agent_name == "workflow-worker"
        ):
            return None
        service = getattr(self.run_manager, "notification_service", None)
        if service is None:
            return None
        return await service.publish_run_notification(
            run_id=run_id,
            source_status=source_status,
            summary=f"Subagent {metadata.get('agent_name') or event_payload.get('agent_name') or 'run'} {source_status}",
            content=content,
            payload={
                "event_type": event_payload.get("event_type"),
                "agent_name": metadata.get("agent_name") or event_payload.get("agent_name"),
                "delegated_task": metadata.get("delegated_task"),
                "original_slash_input": original_slash_input,
                "task_id": metadata.get("task_id"),
            },
            task_id=metadata.get("task_id"),
        )
