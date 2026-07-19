# backend/api/routes/messages.py
from typing import Annotated, Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio
import json
import logging
import re
from ...core.agents import SubagentExecutor
from ...core.chat.chat_manager import ChatManager
from ..dependencies import (
    get_chat_manager,
    get_command_executor,
    get_run_manager,
    get_run_start_coordinator,
    get_subagent_executor,
    get_workflow_manager,
)
from ..run_start import (
    RunStartResponse,
    require_idempotency_key,
    run_start_api_error,
    run_start_openapi_responses,
    run_start_response,
)
from ...core.config.types import Message, StreamChunk
from ...core.perf import get_profiler
from ...core.runs import (
    RunIdempotency,
    RunIdempotencyConflictError,
    RunKind,
    RunManager,
    RunNotFoundError,
    RunRecord,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunRequestFingerprintError,
    RunStartCoordinator,
    RunStartReservationError,
    RunStartSchedulingError,
    RunStartSpec,
    RunStartValidationError,
    RunStatus,
    fingerprint_run_request,
)
from ...core.slash import SlashCommandDispatcher, SlashDispatchKind, SlashCommandRegistry
from ...core.slash.direct_response import build_direct_response_text
from ...core.workflows import WorkflowManager
from .run_control import stop_run_tree

router = APIRouter()
logger = logging.getLogger(__name__)
_ACTIVE_MESSAGE_STREAM_KINDS = {
    RunKind.CHAT.value,
    RunKind.SIDE_QUESTION.value,
    RunKind.DIRECT_RESPONSE.value,
    RunKind.SUBAGENT.value,
    RunKind.WORKFLOW.value,
    RunKind.WORKFLOW_STEP.value,
}
_ANCHOR_STOP_RUN_KINDS = (
    RunKind.SIDE_QUESTION,
    RunKind.DIRECT_RESPONSE,
    RunKind.SUBAGENT,
    RunKind.WORKFLOW,
    RunKind.WORKFLOW_STEP,
)
_RUN_EVENT_BATCH_MAX_DELAY_SECONDS = 0.05
_RUN_EVENT_BATCH_MAX_SIZE = 24


def _should_flush_run_event_immediately(payload: Dict[str, Any]) -> bool:
    status = str(payload.get("status") or "")
    event_type = str(payload.get("event_type") or payload.get("type") or "")
    if status in {"start", "complete", "error", "stopped"}:
        return True
    if event_type.startswith("tool_") or event_type.startswith("child_"):
        return True
    return event_type not in {"", "text", "reasoning", "process_content"}


class RunEventBatcher:
    def __init__(self, run_manager: RunManager, run_id: str):
        self.run_manager = run_manager
        self.run_id = run_id
        self.pending: list[Dict[str, Any]] = []
        self.last_flush = asyncio.get_running_loop().time()
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def append(self, payload: Dict[str, Any]) -> None:
        if _should_flush_run_event_immediately(payload):
            await self.flush()
            await self.run_manager.append_event(self.run_id, payload)
            self.last_flush = asyncio.get_running_loop().time()
            return
        async with self._lock:
            self.pending.append(payload)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_delay())
        now = asyncio.get_running_loop().time()
        if (
            len(self.pending) >= _RUN_EVENT_BATCH_MAX_SIZE
            or now - self.last_flush >= _RUN_EVENT_BATCH_MAX_DELAY_SECONDS
        ):
            await self.flush(now)

    async def flush(self, now: float | None = None) -> None:
        current_task = asyncio.current_task()
        if self._flush_task is not None and self._flush_task is not current_task and not self._flush_task.done():
            self._flush_task.cancel()
        async with self._lock:
            if not self.pending:
                self.last_flush = now if now is not None else asyncio.get_running_loop().time()
                return
            batch = self.pending
            self.pending = []
        await self.run_manager.append_events(self.run_id, batch)
        self.last_flush = now if now is not None else asyncio.get_running_loop().time()

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(_RUN_EVENT_BATCH_MAX_DELAY_SECONDS)
            await self.flush()
        except asyncio.CancelledError:
            return

class SendMessageRequest(BaseModel):
    content: str
    parent_node_id: str
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    focus_new_node: bool = True
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    import_files: Optional[List[Dict[str, Any]]] = None
    image_refs: Optional[List[Dict[str, Any]]] = None
    tool_permission_mode: Optional[str] = None
    task_context_mode: Optional[str] = None


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
    for opt_key in (
        "event_type",
        "reasoning",
        "tool_call",
        "tool_calls",
        "tool_round",
        "tool_round_id",
        "approval",
        "child_run_id",
        "child_kind",
        "child_status",
        "child_summary",
        "payload",
        "task_context_mode",
    ):
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
        "created_by_run_id": run.get("created_by_run_id"),
        "cancellation_parent_run_id": run.get("cancellation_parent_run_id"),
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
        "anchor_node_id": request.parent_node_id,
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


def _parse_prune_summary_args(args: str, default_node_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    target_node_id = default_node_id
    remaining: List[str] = []
    for token in (args or "").split():
        match = re.match(r"^node:(.+)$", token.strip())
        if match:
            target_node_id = match.group(1).strip()
            continue
        remaining.append(token)
    custom_instructions = " ".join(remaining).strip() or None
    return target_node_id, custom_instructions


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
        parent_node_id=request.parent_node_id,
        model_id=request.model_id,
        tool_permission_mode=request.tool_permission_mode,
        task_context_mode=request.task_context_mode,
        slash_metadata=_slash_command_metadata(slash_result),
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
    return [
        slim_message_for_ui(message)
        for message in messages
        if not message.get("is_hidden_from_transcript")
    ]


async def _subscribe_sse(run_manager: RunManager, run_id: str, from_event: int = 0) -> AsyncIterator[str]:
    profiler = get_profiler()
    emitted = 0
    first_event = True
    try:
        with profiler.span("sse.subscribe", run_id=run_id, from_event=from_event, route="messages"):
            async for payload in run_manager.subscribe(run_id, from_event):
                if payload.get("type") == "run_finished":
                    continue
                if first_event:
                    profiler.mark("sse.first_event", run_id=run_id, route="messages")
                    first_event = False
                emitted += 1
                yield _format_sse_data(payload)
    except RunNotFoundError:
        yield _format_sse_data({"status": "error", "error": "运行不存在或已结束", "run_id": run_id})
    finally:
        profiler.mark("sse.done", run_id=run_id, route="messages", emitted_events=emitted)
    yield _format_sse_data("[DONE]")


def _message_run_metadata(request: SendMessageRequest, slash_result: Any) -> Dict[str, Any]:
    return {
        "slash_command": (
            _slash_command_metadata(slash_result)
            if not slash_result.is_passthrough
            else None
        ),
        "model_id": request.model_id,
        "provider_id": request.provider_id,
        "reasoning_effort": request.reasoning_effort,
        "thinking_enabled": request.thinking_enabled,
        "tool_permission_mode": request.tool_permission_mode,
        "task_context_mode": request.task_context_mode,
    }


async def _produce_chat_run(
    *,
    run: RunRecord,
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager,
    run_manager: RunManager,
) -> None:
    profiler = get_profiler()
    final_status = RunStatus.COMPLETED
    final_error: str | None = None
    bound_node_id: str | None = None
    event_batcher = RunEventBatcher(run_manager, run.run_id)
    try:
        with profiler.span(
            "message.run.produce",
            conversation_id=conversation_id,
            run_id=run.run_id,
            kind=run.kind.value,
            provider_id=request.provider_id,
            model_id=request.model_id,
        ):
            async for chunk in chat_manager.send_message_stream(
                conversation_id=conversation_id,
                content=request.content,
                model_id=request.model_id,
                provider_id=request.provider_id,
                parent_node_id=request.parent_node_id,
                focus_new_node=request.focus_new_node,
                reasoning_effort=request.reasoning_effort,
                thinking_enabled=request.thinking_enabled,
                import_files=request.import_files,
                image_refs=request.image_refs,
                tool_permission_mode=request.tool_permission_mode,
                task_context_mode=request.task_context_mode,
                run_id=run.run_id,
            ):
                chunk_data = build_stream_chunk_data(chunk, conversation_id)
                node_id = chunk_data.get("node_id")
                if node_id and node_id != bound_node_id:
                    bound_node_id = node_id
                    await run_manager.bind_target_node(run.run_id, node_id)
                    chunk_data["target_node_id"] = node_id
                    if await run_manager.is_stop_requested(run.run_id):
                        await chat_manager.stop_stream(node_id)
                await event_batcher.append(chunk_data)
                if chunk_data.get("status") == "error":
                    final_status = RunStatus.FAILED
                    final_error = chunk_data.get("error")
                elif chunk_data.get("status") == "stopped":
                    final_status = RunStatus.CANCELLED
    except asyncio.CancelledError:
        final_status = RunStatus.CANCELLED
        await event_batcher.flush()
        await run_manager.append_event(
            run.run_id,
            {
                "status": "stopped",
                "content": "",
                "node_id": bound_node_id,
                "conversation_id": conversation_id,
            },
        )
    except Exception as exc:
        logger.exception("Message producer failed for conversation %s", conversation_id)
        final_status = RunStatus.FAILED
        final_error = str(exc) or exc.__class__.__name__
        await event_batcher.flush()
        await run_manager.append_event(
            run.run_id,
            _stream_error_chunk(conversation_id, final_error),
        )
    finally:
        await event_batcher.flush()
        await run_manager.finish_run(run.run_id, final_status, final_error)


async def _produce_direct_response(
    *,
    run: RunRecord,
    conversation_id: str,
    request: SendMessageRequest,
    slash_result: Any,
    chat_manager: ChatManager,
    run_manager: RunManager,
) -> None:
    try:
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
        await run_manager.append_events(
            run.run_id,
            [
                {"status": "start", "content": None, "node_id": None, "tokens_used": 0},
                {"status": "content", "content": content, "node_id": None, "tokens_used": 0},
                {"status": "complete", "content": None, "node_id": None, "tokens_used": 0},
            ],
        )
        await run_manager.finish_run(run.run_id, RunStatus.COMPLETED)
    except asyncio.CancelledError:
        await run_manager.finish_run(run.run_id, RunStatus.CANCELLED)
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        await run_manager.append_event(
            run.run_id,
            _stream_error_chunk(conversation_id, error),
        )
        await run_manager.finish_run(run.run_id, RunStatus.FAILED, error)


async def _produce_prune_summary(
    *,
    run: RunRecord,
    conversation_id: str,
    request: SendMessageRequest,
    slash_result: Any,
    chat_manager: ChatManager,
    run_manager: RunManager,
) -> None:
    target_node_id, custom_instructions = _parse_prune_summary_args(
        slash_result.args,
        request.parent_node_id,
    )
    try:
        await run_manager.append_event(
            run.run_id,
            {
                "status": "start",
                "content": None,
                "node_id": None,
                "anchor_node_id": target_node_id or request.parent_node_id,
                "tokens_used": 0,
            },
        )
        if not target_node_id:
            raise ValueError("缺少目标节点")
        result = await chat_manager.prune_summary(
            conversation_id,
            target_node_id,
            custom_instructions=custom_instructions,
            model_id=request.model_id,
            provider_id=request.provider_id,
        )
        content = (
            "剪枝摘要已生成\n\n"
            f"- 目标节点: {result['parent_node_id']}\n"
            f"- 摘要 ID: {result['summary_id']}\n"
            f"- 覆盖节点: {result['covered_node_count']}\n"
            f"- 直接子分支: {result['covered_direct_child_count']}\n"
            f"\n摘要预览:\n{result.get('summary_preview') or ''}"
        )
        await run_manager.append_events(
            run.run_id,
            [
                {
                    "status": "content",
                    "content": content,
                    "node_id": None,
                    "anchor_node_id": target_node_id,
                    "tokens_used": 0,
                },
                {
                    "status": "complete",
                    "content": None,
                    "node_id": None,
                    "anchor_node_id": target_node_id,
                    "tokens_used": 0,
                },
            ],
        )
        await run_manager.finish_run(run.run_id, RunStatus.COMPLETED)
    except asyncio.CancelledError:
        await run_manager.finish_run(run.run_id, RunStatus.CANCELLED)
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        await run_manager.append_event(
            run.run_id,
            _stream_error_chunk(conversation_id, error),
        )
        await run_manager.finish_run(run.run_id, RunStatus.FAILED, error)


@router.post(
    "/conversations/{conversation_id}/messages/runs",
    response_model=RunStartResponse,
    responses=run_start_openapi_responses(),
)
async def start_message_run(
    conversation_id: str,
    body: SendMessageRequest,
    http_request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    coordinator: RunStartCoordinator = Depends(get_run_start_coordinator),
    subagent_executor: SubagentExecutor = Depends(get_subagent_executor),
    workflow_manager: WorkflowManager = Depends(get_workflow_manager),
):
    try:
        run_manager.validate_run_references(
            conversation_id,
            anchor_node_id=body.parent_node_id,
        )
        slash_result = SlashCommandDispatcher().dispatch(body.content)
        if slash_result.kind == SlashDispatchKind.ERROR:
            raise RunStartValidationError(
                slash_result.error or "Slash command error"
            )
        idempotency = RunIdempotency(
            key=idempotency_key,
            request_fingerprint=fingerprint_run_request(
                operation="message",
                conversation_id=conversation_id,
                anchor_node_id=body.parent_node_id,
                payload=body.model_dump(mode="json"),
            ),
        )

        async def winner_anchor_factory(_run: RunRecord) -> str:
            return await _create_visible_slash_anchor_node(
                conversation_id=conversation_id,
                request=body,
                chat_manager=chat_manager,
                slash_result=slash_result,
            )

        if slash_result.kind == SlashDispatchKind.SUBAGENT:
            result = await subagent_executor.start_idempotent(
                conversation_id=conversation_id,
                agent_name="implementer",
                input_data=slash_result.args,
                idempotency=idempotency,
                request_id=http_request.state.request_id,
                parent_node_id=body.parent_node_id,
                provider_id=body.provider_id,
                model_id=body.model_id,
                permission_mode=body.tool_permission_mode,
                delegated_task=slash_result.args,
                original_slash_input=slash_result.original_input,
                winner_anchor_factory=winner_anchor_factory,
            )
            return run_start_response(result)

        if slash_result.kind == SlashDispatchKind.WORKFLOW:
            result = await workflow_manager.start_idempotent(
                conversation_id=conversation_id,
                script=slash_result.args,
                args={},
                idempotency=idempotency,
                request_id=http_request.state.request_id,
                parent_node_id=body.parent_node_id,
                permission_mode=body.tool_permission_mode,
                delegated_task=slash_result.args,
                original_slash_input=slash_result.original_input,
                winner_anchor_factory=winner_anchor_factory,
            )
            return run_start_response(result)

        run_kind = RunKind(str(slash_result.run_kind or RunKind.CHAT.value))
        anchor_node_id = body.parent_node_id
        if (
            slash_result.kind == SlashDispatchKind.DIRECT_RESPONSE
            and slash_result.canonical_name == "prune-summary"
        ):
            target_node_id, _instructions = _parse_prune_summary_args(
                slash_result.args,
                body.parent_node_id,
            )
            anchor_node_id = target_node_id or body.parent_node_id

        spec = RunStartSpec(
            conversation_id=conversation_id,
            kind=run_kind,
            anchor_node_id=anchor_node_id,
            summary=body.content[:80],
            metadata=_message_run_metadata(body, slash_result),
            idempotency=idempotency,
            request_id=http_request.state.request_id,
        )

        async def bootstrap(run: RunRecord) -> asyncio.Task[Any]:
            if slash_result.kind == SlashDispatchKind.DIRECT_RESPONSE:
                producer = (
                    _produce_prune_summary(
                        run=run,
                        conversation_id=conversation_id,
                        request=body,
                        slash_result=slash_result,
                        chat_manager=chat_manager,
                        run_manager=run_manager,
                    )
                    if slash_result.canonical_name == "prune-summary"
                    else _produce_direct_response(
                        run=run,
                        conversation_id=conversation_id,
                        request=body,
                        slash_result=slash_result,
                        chat_manager=chat_manager,
                        run_manager=run_manager,
                    )
                )
            else:
                producer = _produce_chat_run(
                    run=run,
                    conversation_id=conversation_id,
                    request=body,
                    chat_manager=chat_manager,
                    run_manager=run_manager,
                )
            return asyncio.create_task(
                producer,
                name=f"message-producer:{run.run_id}",
            )

        return run_start_response(await coordinator.start(spec, bootstrap))
    except (
        RunRequestFingerprintError,
        RunReferenceNotFoundError,
        RunReferenceConversationMismatchError,
        RunIdempotencyConflictError,
        RunStartReservationError,
        RunStartSchedulingError,
        RunStartValidationError,
    ) as exc:
        raise run_start_api_error(exc) from exc



@router.get("/conversations/{conversation_id}/messages/streams/active", response_model=List[Dict[str, Any]])
async def get_active_streams(
    conversation_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    """获取当前对话仍在生成中的可重连流。"""
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
    return [
        _run_to_active_stream_info(run)
        for run in run_manager.list_active()
        if run.get("kind") in _ACTIVE_MESSAGE_STREAM_KINDS
    ]


@router.get("/conversations/{conversation_id}/messages/{node_id}/stream/attach")
async def attach_stream_message(
    conversation_id: str,
    node_id: str,
    from_event: int = 0,
    run_manager: RunManager = Depends(get_run_manager),
):
    """重新订阅仍在运行的流式消息。"""
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
    command_executor: Any = Depends(get_command_executor),
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
            await stop_run_tree(
                str(run["run_id"]),
                run_manager=run_manager,
                chat_manager=chat_manager,
                subagent_executor=subagent_executor,
                command_executor=command_executor,
                workflow_manager=workflow_manager,
            )
        for run_kind in _ANCHOR_STOP_RUN_KINDS:
            anchored_run = run_manager.find_active_by_anchor(
                conversation_id=conversation_id,
                anchor_node_id=node_id,
                kind=run_kind,
            )
            if anchored_run:
                await stop_run_tree(
                    str(anchored_run["run_id"]),
                    run_manager=run_manager,
                    chat_manager=chat_manager,
                    subagent_executor=subagent_executor,
                    command_executor=command_executor,
                    workflow_manager=workflow_manager,
                )
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
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取消息历史"""
    try:
        conversation = chat_manager.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="对话不存在")
        return slim_messages_for_ui(conversation.get_message_chain_from_node(node_id))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/conversations/{conversation_id}/messages", response_model=List[Message])
async def get_all_messages(
    conversation_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取对话中所有消息"""
    try:
        conversation = chat_manager.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="对话不存在")
        return slim_messages_for_ui(conversation.get_message_chain_from_node())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
