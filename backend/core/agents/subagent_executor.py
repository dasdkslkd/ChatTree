from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from copy import deepcopy
from time import time
from typing import Any, Dict, Optional

from backend.core.capabilities.prompting import build_skill_injections, format_skill_injections
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import Message, Role, StreamController, StreamStatus
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.tools.security.permissions import normalize_permission_mode


class SubagentExecutor:
    def __init__(
        self,
        *,
        chat_manager: ChatManager,
        run_manager: RunManager,
        capability_registry: CapabilityRegistry,
    ) -> None:
        self.chat_manager = chat_manager
        self.run_manager = run_manager
        self.capability_registry = capability_registry
        self._controllers: dict[str, StreamController] = {}

    async def start(
        self,
        *,
        conversation_id: str,
        agent_name: str,
        input_data: Any,
        parent_node_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        permission_mode: Optional[str] = None,
        workspace: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        agent = self.capability_registry.get_agent(agent_name)
        if agent is None:
            raise KeyError(agent_name)

        summary = f"{agent.name}: {str(input_data)[:80]}"
        run = await self.run_manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.SUBAGENT,
            anchor_node_id=parent_node_id,
            parent_run_id=parent_run_id,
            summary=summary,
            metadata={
                "agent_name": agent.name,
                "provider_id": provider_id or agent.provider_id,
                "model_id": model_id or agent.model_id or agent.model,
                "permission_mode": permission_mode or agent.permission_mode,
            },
        )
        asyncio.create_task(self._produce(
            run_id=run.run_id,
            conversation_id=conversation_id,
            agent_name=agent_name,
            input_data=input_data,
            parent_node_id=parent_node_id,
            provider_id=provider_id,
            model_id=model_id,
            permission_mode=permission_mode,
            workspace=workspace,
        ))
        return run.to_dict()

    async def stop(self, run_id: str) -> bool:
        await self.run_manager.request_stop(run_id)
        controller = self._controllers.get(run_id)
        if controller:
            await controller.stop()
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
        provider_id: Optional[str],
        model_id: Optional[str],
        permission_mode: Optional[str],
        workspace: Optional[Dict[str, Any]],
    ) -> None:
        final_status = RunStatus.COMPLETED
        final_error: Optional[str] = None
        try:
            agent = self.capability_registry.get_agent(agent_name)
            if agent is None:
                raise KeyError(agent_name)
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

            messages = self._build_messages(agent_name, input_data, parent_node_id)
            tools = self._filter_tools(agent.tools)
            permission = normalize_permission_mode(permission_mode or agent.permission_mode)
            max_tool_rounds = agent.max_tool_rounds or 5
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
            while True:
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
                        final_status = RunStatus.FAILED
                        final_error = chunk.get("error")
                    elif status == StreamStatus.STOPPED:
                        final_status = RunStatus.CANCELLED
                    payload = dict(chunk)
                    payload.update({
                        "run_id": run_id,
                        "conversation_id": conversation_id,
                        "agent_name": agent_name,
                        "target_node_id": None,
                    })
                    await self.run_manager.append_event(run_id, payload)

                if final_status != RunStatus.COMPLETED:
                    break
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

                async def emit_tool_event(event: Dict[str, Any]):
                    event = dict(event)
                    event["run_id"] = run_id
                    event["agent_name"] = agent_name
                    await approval_events.put(event)

                execute_task = asyncio.create_task(
                    self.chat_manager._execute_tool_calls(
                        round_tool_calls,
                        node_id=run_id,
                        conversation_id=conversation_id,
                        emit_event=emit_tool_event,
                        workspace=workspace or conversation.metadata.get("workspace"),
                        permission_mode=permission or "default",
                    )
                )
                event_get_task = asyncio.create_task(approval_events.get())
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {execute_task, event_get_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
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
            await self.run_manager.append_event(run_id, {
                "status": "complete" if final_status == RunStatus.COMPLETED else "stopped",
                "event_type": "subagent_result",
                "agent_name": agent_name,
                "content": total_content,
                "reasoning": total_reasoning or None,
            })
        except Exception as exc:
            final_status = RunStatus.FAILED
            final_error = str(exc) or exc.__class__.__name__
            await self.run_manager.append_event(run_id, {
                "status": "error",
                "event_type": "subagent_error",
                "agent_name": agent_name,
                "content": "",
                "error": final_error,
            })
        finally:
            self._controllers.pop(run_id, None)
            await self.run_manager.finish_run(run_id, final_status, final_error)

    def _build_messages(self, agent_name: str, input_data: Any, parent_node_id: Optional[str]) -> list[Message]:
        agent = self.capability_registry.get_agent(agent_name)
        if agent is None:
            raise KeyError(agent_name)
        system_parts = [agent.system_prompt or f"You are subagent {agent.name}."]
        skill_injections = build_skill_injections(agent.skills, self.capability_registry)
        if skill_injections:
            system_parts.append(format_skill_injections(skill_injections))
        if parent_node_id:
            system_parts.append(f"Parent conversation node: {parent_node_id}")
        content = input_data if isinstance(input_data, str) else json.dumps(input_data, ensure_ascii=False)
        return [
            Message({
                "role": Role.SYSTEM,
                "content": "\n\n".join(part for part in system_parts if part),
            }),
            Message({
                "role": Role.USER,
                "content": content,
            }),
        ]

    def _filter_tools(self, allowed_names: list[str]) -> list[dict[str, Any]]:
        if not self.chat_manager.tool_manager:
            return []
        tools = self.chat_manager.tool_manager.get_openai_tools()
        if not allowed_names:
            return tools
        allowed = set(allowed_names)
        return [
            tool
            for tool in tools
            if tool.get("function", {}).get("name") in allowed
        ]

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
