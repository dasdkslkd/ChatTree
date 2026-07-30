from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.tool_result_format import apply_round_tool_result_budget
from backend.core.config.config import cfg
from backend.core.config.types import Message, Role, StreamController, StreamStatus
from backend.core.instructions import build_agents_instruction_section
from backend.core.projects import filter_capability_registry_for_workspace
from backend.core.prompts import PromptBuilder, PromptBuildRequest
from backend.core.prompts.catalog import load_prompt_template
from backend.core.prompts.types import RuntimePromptContext
from backend.core.runs import (
    RunIdempotency,
    RunKind,
    RunManager,
    ProducerRegistry,
    RunRecord,
    RunStartCoordinator,
    RunStartResult,
    RunStartSpec,
    RunStartValidationError,
    RunStatus,
)
from backend.core.tools.exposure import ToolExposureContext
from backend.core.tools.security.permissions import normalize_permission_mode
from backend.core.tools.task_tools import filter_task_tools_for_context
from .types import AgentDeliveryPolicy


logger = logging.getLogger(__name__)


DEFAULT_MAX_TOOL_ROUNDS = 500
DEFAULT_MAX_TURNS = 1000


@dataclass(frozen=True)
class _PreparedSubagentStart:
    agent_name: str
    workspace: Optional[Dict[str, Any]]
    context_mode: str
    delivery_policy: str
    summary: str
    metadata: Dict[str, Any]


class SubagentExecutor:
    def __init__(
        self,
        *,
        chat_manager: ChatManager,
        run_manager: RunManager,
        capability_registry: CapabilityRegistry,
        mailbox: Any = None,
        run_start_coordinator: RunStartCoordinator | None = None,
        producer_registry: ProducerRegistry | None = None,
    ) -> None:
        self.chat_manager = chat_manager
        self.run_manager = run_manager
        self.capability_registry = capability_registry
        self.mailbox = mailbox
        self.run_start_coordinator = run_start_coordinator
        self.producer_registry = producer_registry or ProducerRegistry.for_run_manager(
            run_manager
        )
        self._controllers: dict[str, StreamController] = {}

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
        task_binding: Optional[Dict[str, Any]] = None,
        runtime_metadata: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        prepared = self._prepare_start(
                conversation_id=conversation_id,
                agent_name=agent_name,
                input_data=input_data,
                provider_id=provider_id,
                model_id=model_id,
                permission_mode=permission_mode,
                workspace=workspace,
                delegated_task=delegated_task,
                original_slash_input=original_slash_input,
                delivery_policy=delivery_policy,
                context_mode=context_mode,
                runtime_metadata=runtime_metadata,
            )
        run = await self.run_manager.create_run(
                conversation_id=conversation_id,
                kind=RunKind.SUBAGENT,
                anchor_node_id=parent_node_id,
                created_by_run_id=created_by_run_id,
                cancellation_parent_run_id=cancellation_parent_run_id,
                summary=prepared.summary,
                metadata=prepared.metadata,
                task_binding=task_binding,
            )
        try:
            self._schedule_existing_locked(
                run=run,
                conversation_id=conversation_id,
                agent_name=prepared.agent_name,
                input_data=input_data,
                parent_node_id=parent_node_id,
                created_by_run_id=created_by_run_id,
                cancellation_parent_run_id=cancellation_parent_run_id,
                provider_id=provider_id,
                model_id=model_id,
                permission_mode=permission_mode,
                workspace=prepared.workspace,
                context_mode=prepared.context_mode,
            )
        except BaseException:
            try:
                await self.producer_registry.terminalize(
                    run.run_id,
                    RunStatus.INTERRUPTED,
                    "producer scheduling failed",
                )
            except BaseException:
                logger.exception(
                    "failed to terminalize unscheduled subagent run %s",
                    run.run_id,
                )
            raise
        return run.to_dict()

    async def start_idempotent(
        self,
        *,
        conversation_id: str,
        agent_name: str,
        input_data: Any,
        idempotency: RunIdempotency,
        request_id: str,
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
        task_binding: Optional[Dict[str, Any]] = None,
        winner_anchor_factory: Callable[[RunRecord], Awaitable[str | None]] | None = None,
        runtime_metadata: Mapping[str, Any] | None = None,
    ) -> RunStartResult:
        coordinator = self.run_start_coordinator
        if coordinator is not None:
            replay = await coordinator.replay_existing(idempotency)
            if replay is not None:
                return replay
        try:
            prepared = self._prepare_start(
                conversation_id=conversation_id,
                agent_name=agent_name,
                input_data=input_data,
                provider_id=provider_id,
                model_id=model_id,
                permission_mode=permission_mode,
                workspace=workspace,
                delegated_task=delegated_task,
                original_slash_input=original_slash_input,
                delivery_policy=delivery_policy,
                context_mode=context_mode,
                runtime_metadata=runtime_metadata,
            )
        except Exception:
            if coordinator is not None:
                replay = await coordinator.replay_existing(idempotency)
                if replay is not None:
                    return replay
            raise
        if coordinator is None:
            raise RuntimeError("run start coordinator is not configured")

        spec = RunStartSpec(
            conversation_id=conversation_id,
            kind=RunKind.SUBAGENT,
            anchor_node_id=parent_node_id,
            created_by_run_id=created_by_run_id,
            cancellation_parent_run_id=cancellation_parent_run_id,
            summary=prepared.summary,
            metadata=prepared.metadata,
            task_binding=task_binding,
            idempotency=idempotency,
            request_id=request_id,
        )

        async def bootstrap(run: RunRecord) -> asyncio.Task[Any]:
            scheduled_run = run
            effective_parent_node_id = run.anchor_node_id
            if winner_anchor_factory is not None:
                winner_anchor_node_id = await winner_anchor_factory(run)
                if (
                    winner_anchor_node_id is not None
                    and winner_anchor_node_id != run.anchor_node_id
                ):
                    scheduled_run = await self.run_manager.bind_anchor_node(
                        run.run_id,
                        winner_anchor_node_id,
                    )
                    effective_parent_node_id = winner_anchor_node_id
            return await self.schedule_existing(
                run=scheduled_run,
                conversation_id=conversation_id,
                agent_name=prepared.agent_name,
                input_data=input_data,
                parent_node_id=effective_parent_node_id,
                created_by_run_id=created_by_run_id,
                cancellation_parent_run_id=cancellation_parent_run_id,
                provider_id=provider_id,
                model_id=model_id,
                permission_mode=permission_mode,
                workspace=prepared.workspace,
                context_mode=prepared.context_mode,
            )

        return await coordinator.start(spec, bootstrap)

    def _prepare_start(
        self,
        *,
        conversation_id: str,
        agent_name: str,
        input_data: Any,
        provider_id: Optional[str],
        model_id: Optional[str],
        permission_mode: Optional[str],
        workspace: Optional[Dict[str, Any]],
        delegated_task: Any,
        original_slash_input: Optional[str],
        delivery_policy: str,
        context_mode: str,
        runtime_metadata: Mapping[str, Any] | None,
    ) -> _PreparedSubagentStart:
        normalized_delivery = AgentDeliveryPolicy(
            str(delivery_policy or "auto")
        ).value
        normalized_context = context_mode if context_mode in {"fresh", "fork"} else "fresh"
        try:
            scope_workspace = self._scope_workspace(conversation_id, workspace)
            agent = self._scoped_registry(scope_workspace).get_agent(agent_name)
        except ValueError as exc:
            raise RunStartValidationError(str(exc)) from exc
        if agent is None:
            raise KeyError(agent_name)
        try:
            self._validate_schema(agent.input_schema, input_data, "input_schema")
        except ValueError as exc:
            raise RunStartValidationError(str(exc)) from exc

        metadata = {
            "agent_name": agent.name,
            "provider_id": provider_id or agent.provider_id,
            "model_id": model_id or agent.model_id or agent.model,
            "permission_mode": permission_mode or agent.permission_mode,
            "delegated_task": delegated_task if delegated_task is not None else input_data,
            "original_slash_input": original_slash_input,
            "delivery_policy": normalized_delivery,
            "context_mode": normalized_context,
        }
        if runtime_metadata is not None:
            metadata.update(dict(runtime_metadata))
        return _PreparedSubagentStart(
            agent_name=agent.name,
            workspace=scope_workspace,
            context_mode=normalized_context,
            delivery_policy=normalized_delivery,
            summary=f"{agent.name}: {self._render_input_summary(input_data, limit=80)}",
            metadata=metadata,
        )

    @staticmethod
    def _render_input_summary(input_data: Any, *, limit: int) -> str:
        if isinstance(input_data, str):
            rendered = input_data
        else:
            try:
                rendered = json.dumps(
                    input_data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise RunStartValidationError(
                    "agent input must contain finite JSON values"
                ) from exc
        return rendered[:limit]

    async def schedule_existing(
        self,
        *,
        run: RunRecord,
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
        context_mode: str,
    ) -> asyncio.Task[Any]:
        return self._schedule_existing_locked(
                run=run,
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
            )

    def _schedule_existing_locked(
        self,
        *,
        run: RunRecord,
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
        context_mode: str,
    ) -> asyncio.Task[Any]:
        producer_coro = self._produce(
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
        )
        task = self.producer_registry.create(
            run.run_id,
            producer_coro,
            name=f"subagent-producer:{run.run_id}",
        )

        return task

    async def stop(self, run_id: str) -> bool:
        failures: list[BaseException] = []
        try:
            await self.run_manager.request_stop(run_id)
        except BaseException as exc:
            failures.append(exc)
        controller = self._controllers.get(run_id)
        if controller:
            try:
                await controller.stop()
            except BaseException as exc:
                failures.append(exc)
        stopped = self.producer_registry.cancel(run_id) or controller is not None
        if failures:
            raise failures[0]
        return stopped

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
            agent = self._scoped_registry(workspace).get_agent(agent_name)
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
            notification_payload = {
                "status": "stopped",
                "event_type": "subagent_result",
                "agent_name": agent_name,
                "content": "",
                "reasoning": None,
            }
            await self.run_manager.append_event(run_id, notification_payload)
        except asyncio.TimeoutError:
            final_status = RunStatus.FAILED
            agent = self._scoped_registry(workspace).get_agent(agent_name)
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
            await self.run_manager.finish_run(run_id, final_status, final_error)

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

            route = self.chat_manager.model_manager.get_route(target_provider, target_model)
            provider = self.chat_manager.model_manager.get_model(
                target_provider,
                target_model,
                True,
            )
            if provider is None:
                raise ValueError(f"无法初始化提供商 {target_provider}")

            messages = self._build_messages(
                agent_name,
                input_data,
                parent_node_id,
                conversation=conversation,
                context_mode=context_mode,
            )
            tools = self._filter_tools(
                agent.tools,
                workspace=conversation.metadata.get("workspace"),
                disallowed_names=agent.disallowed_tools,
            )
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
                round_output_items: list[dict[str, Any]] = []
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
                    if chunk.get("output_item"):
                        output_item = dict(chunk["output_item"])
                        output_item["round_index"] = tool_round
                        output_item["index"] = len(round_output_items)
                        if output_item.get("route_id") != route["route_id"]:
                            raise RuntimeError(
                                "continuation_invalid: subagent 输出项路由不一致"
                            )
                        round_output_items.append(output_item)
                        continue
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
                if (
                    (route.get("reasoning_profile") or {}).get("strict")
                    and not round_output_items
                ):
                    raise RuntimeError(
                        "continuation_invalid: subagent 适配器未封存工具续接状态"
                    )
                messages.append({
                    "role": "assistant",
                    "content": round_content,
                    "reasoning": round_reasoning or None,
                    "tool_calls": round_tool_calls,
                    "model_route_id": route["route_id"],
                    "model_state_items": round_output_items,
                })
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
                            "task_context_mode": "detached",
                            "task_summary": self._render_input_summary(
                                input_data,
                                limit=160,
                            ),
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
                model_tool_messages = apply_round_tool_result_budget(tool_messages)
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
        conversation_metadata = getattr(conversation, "metadata", {}) or {}
        workspace = conversation_metadata.get("workspace") if isinstance(conversation_metadata, dict) else None
        scoped_registry = self._scoped_registry(workspace)
        agent = scoped_registry.get_agent(agent_name)
        if agent is None:
            raise KeyError(agent_name)
        is_workflow_worker = agent.name == "workflow-worker" or agent.metadata.get("runtime") == "workflow"
        system_parts = [
            load_prompt_template("fork"),
            agent.system_prompt or f"You are subagent {agent.name}.",
        ]
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
            for message in PromptBuilder(scoped_registry).build(
                PromptBuildRequest(
                    base_messages=base_messages,
                    active_skill_names=agent.skills,
                    runtime_context=self._runtime_prompt_context(agent, is_workflow_worker),
                    include_core_prompt=False,
                    include_available_capabilities=False,
                    extra_sections=self._agents_instruction_sections(workspace),
                )
            )
        ]

    def _agents_instruction_sections(self, workspace: Optional[dict[str, Any]]) -> list[Any]:
        section = build_agents_instruction_section(
            workspace,
            cfg.data if isinstance(cfg.data, dict) else None,
        )
        return [section] if section is not None else []

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
        conversation_id = str(((getattr(conversation, "metadata", None) or {}) or {}).get("id") or "")
        node_ids = [str((node or {}).get("id") or "") for node in chain or [] if (node or {}).get("id")]
        messages_by_node = (
            self.chat_manager.canonical_messages_by_node(conversation_id, node_ids)
            if conversation_id and node_ids
            else {}
        )
        lines: list[str] = []
        for node in chain or []:
            messages = messages_by_node.get(str((node or {}).get("id") or ""), [])
            user_text = next(
                (
                    str(message.get("content") or "").strip()
                    for message in messages
                    if message.get("role") == Role.USER.value
                    and not message.get("is_hidden_from_transcript")
                    and not message.get("is_visible_in_transcript_only")
                ),
                "",
            )
            assistant_text = next(
                (
                    str(message.get("content") or "").strip()
                    for message in reversed(messages)
                    if message.get("role") == Role.ASSISTANT.value
                    and not message.get("is_hidden_from_transcript")
                    and not message.get("is_visible_in_transcript_only")
                    and (message.get("subtype") in (None, "", "assistant_answer"))
                ),
                "",
            )
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

    def _runtime_prompt_context(self, agent, is_workflow_worker: bool) -> RuntimePromptContext:
        if is_workflow_worker:
            return RuntimePromptContext(
                name="workflow_worker",
                content="\n".join([
                    "## Runtime Context",
                    "",
                    "Runtime mode: workflow worker",
                    "- You are running inside a workflow as a worker subagent.",
                    "- Return the data or result the workflow requested; your final text is returned verbatim to the workflow runtime.",
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

    def _filter_tools(
        self,
        allowed_names: Optional[list[str]],
        *,
        workspace: Optional[dict[str, Any]] = None,
        disallowed_names: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        if not self.chat_manager.tool_manager:
            return []
        exposure_context = ToolExposureContext(
            run_kind="agent",
            allowed_tools=tuple(allowed_names) if allowed_names is not None else None,
            disallowed_tools=tuple(disallowed_names or ()),
        )
        try:
            tools = self.chat_manager.tool_manager.get_openai_tools(
                workspace=workspace,
                exposure_context=exposure_context,
            )
        except TypeError:
            tools = self.chat_manager.tool_manager.get_openai_tools(workspace=workspace)
        if allowed_names == []:
            return []
        tools = filter_task_tools_for_context(
            tools,
            "detached",
            has_active_task=False,
        )
        disallowed = set(disallowed_names or ())
        if disallowed:
            tools = [
                tool
                for tool in tools
                if tool.get("function", {}).get("name") not in disallowed
            ]
        if allowed_names is None or "*" in allowed_names:
            return tools
        allowed = set(allowed_names)
        return [
            tool
            for tool in tools
            if tool.get("function", {}).get("name") in allowed
        ]

    def _scoped_registry(self, workspace: Optional[dict[str, Any]]) -> CapabilityRegistry:
        return filter_capability_registry_for_workspace(
            self.capability_registry,
            cfg.data if isinstance(cfg.data, dict) else None,
            workspace if isinstance(workspace, dict) else None,
        ) or self.capability_registry

    def _scope_workspace(
        self,
        conversation_id: str,
        workspace: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if isinstance(workspace, dict):
            return workspace
        try:
            conversation = self.chat_manager.get_conversation(conversation_id)
        except Exception:
            conversation = None
        if conversation is None:
            return None
        stored_workspace = conversation.metadata.get("workspace")
        return stored_workspace if isinstance(stored_workspace, dict) else None

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
