# backend/api/routes/messages.py
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from typing import Any, AsyncIterator, Dict, List, Optional
from pydantic import BaseModel
import asyncio
import json
import logging
from ...core.agents import SubagentExecutor
from ...core.chat.chat_manager import ChatManager
from ..dependencies import get_chat_manager, get_run_manager, get_subagent_executor, get_workflow_manager
from ...core.config.types import Message, StreamChunk
from ...core.runs import RunKind, RunManager, RunNotFoundError, RunStatus
from ...core.slash import SlashCommandDispatcher, SlashDispatchKind, SlashCommandRegistry
from ...core.slash.direct_response import build_direct_response_text
from ...core.workflows import WorkflowManager

router = APIRouter()
logger = logging.getLogger(__name__)
_DEFAULT_RUN_MANAGER = RunManager()
_STREAM_SESSIONS: dict[str, "LegacyRunStreamSession"] = {}
_ACTIVE_MESSAGE_STREAM_KINDS = {
    RunKind.CHAT.value,
    RunKind.SIDE_QUESTION.value,
    RunKind.DIRECT_RESPONSE.value,
}


def _wake_synthetic_followup_scheduler(request: Request, conversation_id: str) -> None:
    scheduler = getattr(request.app.state, "synthetic_followup_scheduler", None)
    if scheduler is not None:
        scheduler.notify(conversation_id)

class SendMessageRequest(BaseModel):
    content: str
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    node_id: Optional[str] = None
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    import_files: Optional[List[Dict[str, Any]]] = None
    image_refs: Optional[List[Dict[str, Any]]] = None
    tool_permission_mode: Optional[str] = None
    synthetic_input_id: Optional[str] = None
    synthetic_origin: Optional[str] = None
    synthetic_kind: Optional[str] = None
    synthetic_metadata: Optional[Dict[str, Any]] = None


class SyntheticInputStartRequest(BaseModel):
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    tool_permission_mode: Optional[str] = None


class LegacyRunStreamSession:
    def __init__(self, run_manager: RunManager, run_id: str, conversation_id: str):
        self.run_manager = run_manager
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.node_id: str | None = None

    async def subscribe(self, start_index: int = 0) -> AsyncIterator[str]:
        async for event in _subscribe_sse(self.run_manager, self.run_id, start_index):
            yield event

    def snapshot(self) -> Dict[str, Any]:
        run = self.run_manager.get_run(self.run_id) or {}
        return _run_to_active_stream_info(run)


def _resolve_run_manager(run_manager: Any = None) -> RunManager:
    return run_manager if isinstance(run_manager, RunManager) else _DEFAULT_RUN_MANAGER


def build_stream_chunk_data(chunk: StreamChunk, conversation_id: str) -> Dict[str, Any]:
    """将内部 StreamChunk 转成 SSE JSON payload。"""
    chunk_data: Dict[str, Any] = {
        "status": chunk.get("status", "content"),
        "content": chunk.get("content", ""),
        "node_id": chunk.get("node_id"),
        "target_node_id": chunk.get("target_node_id") or chunk.get("node_id"),
        "run_id": chunk.get("run_id"),
        "event_index": chunk.get("event_index"),
        "conversation_id": chunk.get("conversation_id", conversation_id),
        "error": chunk.get("error"),
        "tokens_used": chunk.get("tokens_used", 0),
        "usage_info": chunk.get("usage_info")
    }
    # 仅在存在时转发可扩展字段，保持当前文本路径 JSON 形状不变
    for opt_key in ("event_type", "reasoning", "tool_call", "tool_calls", "approval"):
        val = chunk.get(opt_key)
        if val is not None:
            chunk_data[opt_key] = val
    if chunk_data.get("event_type") == "tool_result" and isinstance(chunk_data.get("tool_call"), dict):
        chunk_data["tool_call"] = slim_tool_result_for_ui(chunk_data["tool_call"])
    return chunk_data


def _format_sse_data(payload: Dict[str, Any] | str) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_error_chunk(conversation_id: str, error: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "content": "",
        "node_id": None,
        "conversation_id": conversation_id,
        "error": error,
        "tokens_used": 0,
    }


def _run_to_active_stream_info(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "conversation_id": run.get("conversation_id"),
        "anchor_node_id": run.get("anchor_node_id"),
        "node_id": run.get("target_node_id"),
        "target_node_id": run.get("target_node_id"),
        "kind": run.get("kind"),
        "status": run.get("status"),
        "event_count": run.get("event_count", 0),
        "done": run.get("status") in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value},
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def _direct_response_runtime_context(
    *,
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager,
    run_manager: RunManager,
) -> Dict[str, Any]:
    context: Dict[str, Any] = {
        "conversation_id": conversation_id,
        "anchor_node_id": request.node_id,
        "tool_permission_mode": request.tool_permission_mode,
        "active_runs": run_manager.list_active(conversation_id),
    }
    try:
        conversation = chat_manager.get_conversation(conversation_id)
    except Exception:
        conversation = None
    if conversation is not None:
        metadata = conversation.metadata or {}
        model_id = request.model_id or metadata.get("model_id") or conversation.current_model
        provider_id = request.provider_id or metadata.get("provider_id") or conversation.current_provider
        prompt = metadata.get("selected_system_prompt") or {}
        workspace = metadata.get("workspace") or {}
        context.update({
            "provider_model": "/".join([str(provider_id or ""), str(model_id or "")]).strip("/"),
            "workspace_cwd": workspace.get("cwd") if isinstance(workspace, dict) else None,
            "prompt_mode": prompt.get("mode") if isinstance(prompt, dict) and prompt else "none",
        })
    registry = getattr(chat_manager, "capability_registry", None)
    if registry is not None:
        context["capability_counts"] = {
            "skills": len(registry.skills()),
            "agents": len(registry.agents()),
            "plugins": len(registry.plugins()),
        }
    return context


def _synthetic_run_metadata(request: SendMessageRequest) -> Dict[str, Any]:
    if not request.synthetic_input_id:
        return {}
    synthetic_metadata = dict(request.synthetic_metadata or {})
    return {
        "origin": request.synthetic_origin or synthetic_metadata.get("origin") or "synthetic_input",
        "synthetic_input": {
            **synthetic_metadata,
            "input_id": request.synthetic_input_id,
            "kind": request.synthetic_kind,
            "origin": request.synthetic_origin or synthetic_metadata.get("origin"),
        },
    }


def _slash_command_metadata(slash_result: Any) -> Dict[str, Any]:
    return {
        "command": slash_result.canonical_name,
        "input_command": slash_result.command_name,
        "kind": slash_result.kind.value,
        "args": slash_result.args,
        "original_input": slash_result.original_input,
        "tool_policy": slash_result.tool_policy.value,
        "persistence_policy": slash_result.persistence_policy.value,
        "run_kind": slash_result.run_kind,
    }


async def _create_visible_slash_anchor_node(
    *,
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager,
    slash_result: Any,
) -> str:
    create_anchor = getattr(chat_manager, "create_visible_user_anchor_node", None)
    if create_anchor is None:
        raise RuntimeError("ChatManager 不支持 detached slash 锚点节点")
    return await create_anchor(
        conversation_id=conversation_id,
        content=request.content,
        parent_node_id=request.node_id,
        model_id=request.model_id,
        tool_permission_mode=request.tool_permission_mode,
        slash_metadata=_slash_command_metadata(slash_result),
    )


def build_task_notification_content(item: Dict[str, Any]) -> str:
    metadata = dict(item.get("metadata") or {})
    payload = {
        "kind": item.get("kind"),
        "summary": item.get("summary"),
        "source_run_id": item.get("source_run_id"),
        "source_run_kind": item.get("source_run_kind"),
        "source_status": metadata.get("source_status"),
        "delegated_task": metadata.get("delegated_task"),
        "original_slash_input": metadata.get("original_slash_input"),
        "content": item.get("content") or "",
    }
    return "<task-notification>\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n</task-notification>"


def build_synthetic_message_request(
    item: Dict[str, Any],
    request: Optional[SyntheticInputStartRequest] = None,
) -> SendMessageRequest:
    request = request or SyntheticInputStartRequest()
    return SendMessageRequest(
        content=build_task_notification_content(item),
        model_id=request.model_id,
        provider_id=request.provider_id,
        node_id=item.get("anchor_node_id"),
        reasoning_effort=request.reasoning_effort,
        thinking_enabled=request.thinking_enabled,
        tool_permission_mode=request.tool_permission_mode,
        synthetic_input_id=str(item.get("input_id") or ""),
        synthetic_origin="task_notification",
        synthetic_kind=str(item.get("kind") or ""),
        synthetic_metadata={
            "origin": "task_notification",
            "source_run_id": item.get("source_run_id"),
            "source_run_kind": item.get("source_run_kind"),
            "source_status": (item.get("metadata") or {}).get("source_status"),
            "delegated_task": (item.get("metadata") or {}).get("delegated_task"),
            "original_slash_input": (item.get("metadata") or {}).get("original_slash_input"),
            "mailbox_message_id": (item.get("metadata") or {}).get("mailbox_message_id"),
        },
    )


_HEAVY_TOOL_RESULT_FIELDS = {"raw_content", "model_visible_content"}


def slim_tool_result_for_ui(tool_message: Dict[str, Any]) -> Dict[str, Any]:
    """Return a UI-safe tool message without model-only or raw result payloads."""
    return {
        key: value
        for key, value in dict(tool_message).items()
        if key not in _HEAVY_TOOL_RESULT_FIELDS
    }


def _slim_tool_interaction_for_ui(interaction: Any) -> Any:
    if not isinstance(interaction, dict):
        return interaction
    slimmed = dict(interaction)
    tools = slimmed.get("tools")
    if isinstance(tools, list):
        slimmed["tools"] = [
            slim_tool_result_for_ui(tool) if isinstance(tool, dict) else tool
            for tool in tools
        ]
    return slimmed


def slim_message_for_ui(message: Message | Dict[str, Any]) -> Message:
    """Slim heavy nested tool payloads before sending transcript data to the UI."""
    slimmed: Dict[str, Any] = dict(message)
    if slimmed.get("role") == "tool":
        return Message(slim_tool_result_for_ui(slimmed))

    tool_results = slimmed.get("tool_results")
    if isinstance(tool_results, list):
        slimmed["tool_results"] = [
            slim_tool_result_for_ui(tool) if isinstance(tool, dict) else tool
            for tool in tool_results
        ]

    tool_interactions = slimmed.get("tool_interactions")
    if isinstance(tool_interactions, list):
        slimmed["tool_interactions"] = [
            _slim_tool_interaction_for_ui(interaction)
            for interaction in tool_interactions
        ]

    return Message(slimmed)


def slim_messages_for_ui(messages: List[Message]) -> List[Message]:
    return [slim_message_for_ui(message) for message in messages]


async def _subscribe_sse(run_manager: RunManager, run_id: str, from_event: int = 0) -> AsyncIterator[str]:
    try:
        async for payload in run_manager.subscribe(run_id, from_event):
            if payload.get("type") == "run_finished":
                continue
            yield _format_sse_data(payload)
    except RunNotFoundError:
        yield _format_sse_data({"status": "error", "error": "运行不存在或已结束", "run_id": run_id})
    yield _format_sse_data("[DONE]")


async def start_detached_chat_run(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager,
    run_manager: RunManager,
    slash_result: Any | None = None,
) -> Dict[str, Any]:
    slash_result = slash_result or SlashCommandDispatcher().dispatch(request.content)
    run_kind = RunKind(str(slash_result.run_kind or RunKind.CHAT.value))
    run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=run_kind,
        anchor_node_id=request.node_id,
        summary=request.content[:80],
        metadata={
            "slash_command": {
                "command": slash_result.canonical_name,
                "input_command": slash_result.command_name,
                "kind": slash_result.kind.value,
                "args": slash_result.args,
                "original_input": slash_result.original_input,
                "tool_policy": slash_result.tool_policy.value,
                "persistence_policy": slash_result.persistence_policy.value,
                "run_kind": slash_result.run_kind,
            } if not slash_result.is_passthrough else None,
            "model_id": request.model_id,
            "provider_id": request.provider_id,
            "reasoning_effort": request.reasoning_effort,
            "thinking_enabled": request.thinking_enabled,
            "tool_permission_mode": request.tool_permission_mode,
            **_synthetic_run_metadata(request),
        },
    )

    async def produce() -> None:
        final_status = RunStatus.COMPLETED
        final_error: str | None = None
        bound_node_id: str | None = None
        synthetic_consumed = False
        try:
            async for chunk in chat_manager.send_message_stream(
                conversation_id=conversation_id,
                content=request.content,
                model_id=request.model_id,
                provider_id=request.provider_id,
                node_id=request.node_id,
                reasoning_effort=request.reasoning_effort,
                thinking_enabled=request.thinking_enabled,
                import_files=request.import_files,
                image_refs=request.image_refs,
                tool_permission_mode=request.tool_permission_mode,
                message_subtype=request.synthetic_kind if request.synthetic_input_id else None,
                run_id=run.run_id,
            ):
                chunk_data = build_stream_chunk_data(chunk, conversation_id)
                node_id = chunk_data.get("node_id")
                if node_id and node_id != bound_node_id:
                    bound_node_id = node_id
                    await run_manager.bind_target_node(run.run_id, node_id)
                    if request.synthetic_input_id and not synthetic_consumed:
                        run_manager.synthetic_inputs.mark_consumed(conversation_id, request.synthetic_input_id)
                        mailbox = getattr(run_manager, "agent_mailbox", None)
                        mailbox_message_id = (request.synthetic_metadata or {}).get("mailbox_message_id")
                        if mailbox is not None and mailbox_message_id:
                            await mailbox.mark_integrated(conversation_id, str(mailbox_message_id))
                            await mailbox.acknowledge(conversation_id, str(mailbox_message_id))
                        synthetic_consumed = True
                    legacy_session = LegacyRunStreamSession(run_manager, run.run_id, conversation_id)
                    legacy_session.node_id = node_id
                    _STREAM_SESSIONS[node_id] = legacy_session
                    chunk_data["target_node_id"] = node_id
                    if await run_manager.is_stop_requested(run.run_id):
                        await chat_manager.stop_stream(node_id)
                await run_manager.append_event(run.run_id, chunk_data)
                if chunk_data.get("status") == "error":
                    final_status = RunStatus.FAILED
                    final_error = chunk_data.get("error")
                elif chunk_data.get("status") == "stopped":
                    final_status = RunStatus.CANCELLED

        except Exception as e:
            logger.exception("Detached stream failed for conversation %s", conversation_id)
            final_status = RunStatus.FAILED
            final_error = str(e)
            await run_manager.append_event(run.run_id, _stream_error_chunk(conversation_id, str(e)))
        finally:
            if request.synthetic_input_id and not synthetic_consumed:
                mailbox = getattr(run_manager, "agent_mailbox", None)
                mailbox_message_id = (request.synthetic_metadata or {}).get("mailbox_message_id")
                if mailbox is not None and mailbox_message_id:
                    await mailbox.release_notification(conversation_id, str(mailbox_message_id))
                run_manager.synthetic_inputs.release(conversation_id, request.synthetic_input_id, notify=False)
            await run_manager.finish_run(run.run_id, final_status, final_error)
            if bound_node_id:
                _STREAM_SESSIONS.pop(bound_node_id, None)

    asyncio.create_task(produce())
    return run.to_dict()


class SyntheticFollowupScheduler:
    def __init__(
        self,
        *,
        chat_manager: ChatManager,
        run_manager: RunManager,
    ) -> None:
        self.chat_manager = chat_manager
        self.run_manager = run_manager
        self._draining: set[str] = set()

    def install(self) -> None:
        self.run_manager.synthetic_inputs.set_pending_listener(self.notify)
        self.run_manager.add_finish_listener(self._handle_run_finished)

    def notify(self, conversation_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.drain(conversation_id))

    def _handle_run_finished(self, run: Dict[str, Any]) -> None:
        metadata = run.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("origin") == "task_notification":
            return
        if run.get("kind") == RunKind.CHAT.value:
            self.notify(str(run.get("conversation_id") or ""))

    def _has_active_chat(self, conversation_id: str) -> bool:
        return any(
            run.get("kind") == RunKind.CHAT.value
            for run in self.run_manager.list_active(conversation_id)
        )

    async def drain(self, conversation_id: str) -> bool:
        if not conversation_id or conversation_id in self._draining:
            return False
        if self._has_active_chat(conversation_id):
            return False
        self._draining.add(conversation_id)
        try:
            if self._has_active_chat(conversation_id):
                return False
            item = self.run_manager.synthetic_inputs.claim_next(conversation_id)
            if item is None:
                return False
            if item.get("kind") != "task_notification":
                self.run_manager.synthetic_inputs.release(conversation_id, str(item.get("input_id") or ""), notify=False)
                return False
            mailbox = getattr(self.run_manager, "agent_mailbox", None)
            mailbox_message_id = (item.get("metadata") or {}).get("mailbox_message_id")
            if mailbox is not None and mailbox_message_id and await mailbox.is_integrated(conversation_id, str(mailbox_message_id)):
                self.run_manager.synthetic_inputs.mark_consumed(conversation_id, str(item.get("input_id") or ""))
                await mailbox.acknowledge(conversation_id, str(mailbox_message_id))
                return True
            try:
                await start_detached_chat_run(
                    conversation_id,
                    build_synthetic_message_request(item),
                    self.chat_manager,
                    self.run_manager,
                )
            except Exception:
                self.run_manager.synthetic_inputs.release(conversation_id, str(item.get("input_id") or ""), notify=False)
                raise
            return True
        finally:
            self._draining.discard(conversation_id)


async def detached_stream_event_generator(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager,
    run_manager: RunManager | None = None,
    subagent_executor: SubagentExecutor | None = None,
    workflow_manager: WorkflowManager | None = None,
) -> AsyncIterator[str]:
    """Stream SSE events without tying generation lifetime to the client socket."""
    run_manager = _resolve_run_manager(run_manager)
    slash_result = SlashCommandDispatcher().dispatch(request.content)

    if slash_result.kind == SlashDispatchKind.ERROR:
        yield _format_sse_data(_stream_error_chunk(conversation_id, slash_result.error or "Slash command error"))
        yield _format_sse_data("[DONE]")
        return

    if slash_result.kind == SlashDispatchKind.SUBAGENT:
        if subagent_executor is None:
            yield _format_sse_data(_stream_error_chunk(conversation_id, "Subagent 执行器未初始化"))
            yield _format_sse_data("[DONE]")
            return
        try:
            anchor_node_id = await _create_visible_slash_anchor_node(
                conversation_id=conversation_id,
                request=request,
                chat_manager=chat_manager,
                slash_result=slash_result,
            )
            run = await subagent_executor.start(
                conversation_id=conversation_id,
                agent_name="implementer",
                input_data=slash_result.args,
                parent_node_id=anchor_node_id,
                provider_id=request.provider_id,
                model_id=request.model_id,
                permission_mode=request.tool_permission_mode,
                delegated_task=slash_result.args,
                original_slash_input=slash_result.original_input,
            )
        except Exception as exc:
            yield _format_sse_data(_stream_error_chunk(conversation_id, str(exc)))
            yield _format_sse_data("[DONE]")
            return
        async for event in _subscribe_sse(run_manager, str(run["run_id"]), 0):
            yield event
        return

    if slash_result.kind == SlashDispatchKind.WORKFLOW:
        if workflow_manager is None:
            yield _format_sse_data(_stream_error_chunk(conversation_id, "Workflow 管理器未初始化"))
            yield _format_sse_data("[DONE]")
            return
        try:
            anchor_node_id = await _create_visible_slash_anchor_node(
                conversation_id=conversation_id,
                request=request,
                chat_manager=chat_manager,
                slash_result=slash_result,
            )
            run = await workflow_manager.start(
                conversation_id=conversation_id,
                script=slash_result.args,
                args={},
                parent_node_id=anchor_node_id,
                permission_mode=request.tool_permission_mode,
                delegated_task=slash_result.args,
                original_slash_input=slash_result.original_input,
            )
        except Exception as exc:
            yield _format_sse_data(_stream_error_chunk(conversation_id, str(exc)))
            yield _format_sse_data("[DONE]")
            return
        async for event in _subscribe_sse(run_manager, str(run["run_id"]), 0):
            yield event
        return

    if slash_result.kind == SlashDispatchKind.DIRECT_RESPONSE:
        run = await run_manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.DIRECT_RESPONSE,
            anchor_node_id=request.node_id,
            summary=request.content[:80],
            metadata={
                "slash_command": {
                    "command": slash_result.canonical_name,
                    "input_command": slash_result.command_name,
                    "kind": slash_result.kind.value,
                    "args": slash_result.args,
                    "original_input": slash_result.original_input,
                    "tool_policy": slash_result.tool_policy.value,
                    "persistence_policy": slash_result.persistence_policy.value,
                    "run_kind": slash_result.run_kind,
                },
                "model_id": request.model_id,
                "provider_id": request.provider_id,
                "reasoning_effort": request.reasoning_effort,
                "thinking_enabled": request.thinking_enabled,
                "tool_permission_mode": request.tool_permission_mode,
            },
        )
        content = build_direct_response_text(
            slash_result,
            SlashCommandRegistry.builtins().public_definitions(),
            _direct_response_runtime_context(
                conversation_id=conversation_id,
                request=request,
                chat_manager=chat_manager,
                run_manager=run_manager,
            ),
        )
        await run_manager.append_event(run.run_id, {
            "status": "start",
            "content": None,
            "node_id": None,
            "target_node_id": None,
            "conversation_id": conversation_id,
            "run_id": run.run_id,
            "tokens_used": 0,
        })
        await run_manager.append_event(run.run_id, {
            "status": "content",
            "content": content,
            "node_id": None,
            "target_node_id": None,
            "conversation_id": conversation_id,
            "run_id": run.run_id,
            "tokens_used": 0,
        })
        await run_manager.append_event(run.run_id, {
            "status": "complete",
            "content": None,
            "node_id": None,
            "target_node_id": None,
            "conversation_id": conversation_id,
            "run_id": run.run_id,
            "tokens_used": 0,
        })
        await run_manager.finish_run(run.run_id, RunStatus.COMPLETED)
        async for event in _subscribe_sse(run_manager, run.run_id, 0):
            yield event
        return

    run = await start_detached_chat_run(
        conversation_id,
        request,
        chat_manager,
        run_manager,
        slash_result,
    )
    async for event in _subscribe_sse(run_manager, str(run["run_id"]), 0):
        yield event

@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    subagent_executor: SubagentExecutor = Depends(get_subagent_executor),
    workflow_manager: WorkflowManager = Depends(get_workflow_manager),
):
    """流式发送消息 - 返回 SSE 格式"""
    
    return StreamingResponse(
        detached_stream_event_generator(
            conversation_id,
            request,
            chat_manager,
            run_manager,
            subagent_executor,
            workflow_manager,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/conversations/{conversation_id}/messages/streams/active", response_model=List[Dict[str, Any]])
async def get_active_streams(
    conversation_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    """获取当前对话仍在生成中的可重连流。"""
    run_manager = _resolve_run_manager(run_manager)
    return [
        _run_to_active_stream_info(run)
        for run in run_manager.list_active(conversation_id)
        if run.get("kind") in _ACTIVE_MESSAGE_STREAM_KINDS
    ]


@router.get("/conversations/messages/streams/active", response_model=List[Dict[str, Any]])
async def get_all_active_streams(
    run_manager: RunManager = Depends(get_run_manager),
):
    """获取所有仍在生成中的可重连流。"""
    run_manager = _resolve_run_manager(run_manager)
    return [
        _run_to_active_stream_info(run)
        for run in run_manager.list_active()
        if run.get("kind") in _ACTIVE_MESSAGE_STREAM_KINDS
    ]


@router.get("/conversations/{conversation_id}/synthetic-inputs/pending", response_model=List[Dict[str, Any]])
async def get_pending_synthetic_inputs(
    conversation_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    return run_manager.synthetic_inputs.list_pending(conversation_id)


@router.post("/conversations/{conversation_id}/synthetic-inputs/{input_id}/consume", response_model=Dict[str, Any])
async def consume_synthetic_input(
    conversation_id: str,
    input_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    item = run_manager.synthetic_inputs.mark_consumed(conversation_id, input_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Synthetic input 不存在")
    return item


@router.post("/conversations/{conversation_id}/synthetic-inputs/{input_id}/stream")
async def stream_synthetic_input(
    conversation_id: str,
    input_id: str,
    request: Optional[SyntheticInputStartRequest] = None,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    subagent_executor: SubagentExecutor = Depends(get_subagent_executor),
    workflow_manager: WorkflowManager = Depends(get_workflow_manager),
):
    item = run_manager.synthetic_inputs.get(conversation_id, input_id)
    if item is None or item.get("status") != "pending":
        raise HTTPException(status_code=404, detail="Pending synthetic input 不存在")
    if item.get("kind") != "task_notification":
        raise HTTPException(status_code=400, detail="不支持的 synthetic input 类型")

    claimed = run_manager.synthetic_inputs.claim(conversation_id, input_id)
    if claimed is None:
        raise HTTPException(status_code=404, detail="Pending synthetic input 不存在")
    try:
        run = await start_detached_chat_run(
            conversation_id,
            build_synthetic_message_request(claimed, request),
            chat_manager,
            run_manager,
        )
    except Exception:
        run_manager.synthetic_inputs.release(conversation_id, input_id, notify=False)
        raise
    return StreamingResponse(
        _subscribe_sse(run_manager, str(run["run_id"]), 0),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations/{conversation_id}/messages/{node_id}/stream/attach")
async def attach_stream_message(
    conversation_id: str,
    node_id: str,
    from_event: int = 0,
    run_manager: RunManager = Depends(get_run_manager),
):
    """重新订阅仍在运行的流式消息。"""
    run_manager = _resolve_run_manager(run_manager)
    run = run_manager.find_active_by_target(
        conversation_id=conversation_id,
        target_node_id=node_id,
        kind=RunKind.CHAT,
    )
    if not run:
        raise HTTPException(status_code=404, detail="流式消息不存在或已结束")

    return StreamingResponse(
        _subscribe_sse(run_manager, str(run["run_id"]), from_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
    
@router.post("/conversations/{conversation_id}/messages/{node_id}/stream/stop")
async def stop_stream_message(
    conversation_id: str,
    node_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    subagent_executor: SubagentExecutor = Depends(get_subagent_executor),
    workflow_manager: WorkflowManager = Depends(get_workflow_manager),
):
    """停止流式消息"""
    try:
        if not chat_manager.storage.index.get(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        run = run_manager.find_active_by_target(
            conversation_id=conversation_id,
            target_node_id=node_id,
            kind=RunKind.CHAT,
        )
        if run:
            await run_manager.request_stop(str(run["run_id"]))
        subagent_run = run_manager.find_active_by_anchor(
            conversation_id=conversation_id,
            anchor_node_id=node_id,
            kind=RunKind.SUBAGENT,
        )
        if subagent_run:
            await run_manager.request_stop(str(subagent_run["run_id"]))
            if subagent_executor is not None and hasattr(subagent_executor, "stop"):
                await subagent_executor.stop(str(subagent_run["run_id"]))
        workflow_run = run_manager.find_active_by_anchor(
            conversation_id=conversation_id,
            anchor_node_id=node_id,
            kind=RunKind.WORKFLOW,
        )
        if workflow_run:
            await run_manager.request_stop(str(workflow_run["run_id"]))
            if workflow_manager is not None and hasattr(workflow_manager, "stop"):
                await workflow_manager.stop(str(workflow_run["run_id"]))
        await chat_manager.stop_stream(node_id)
        return {"detail": "流式消息已停止"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/conversations/{conversation_id}/messages/{node_id}", response_model=List[Message])
async def get_messages(
    conversation_id: str,
    node_id: str,
    request: Request,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取消息历史"""
    try:
        conversation = chat_manager.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="对话不存在")
        _wake_synthetic_followup_scheduler(request, conversation_id)
        return slim_messages_for_ui(conversation.get_message_chain_from_node(node_id))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/conversations/{conversation_id}/messages", response_model=List[Message])
async def get_all_messages(
    conversation_id: str,
    request: Request,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取对话中所有消息"""
    try:
        conversation = chat_manager.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="对话不存在")
        _wake_synthetic_followup_scheduler(request, conversation_id)
        return slim_messages_for_ui(conversation.get_message_chain_from_node())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
