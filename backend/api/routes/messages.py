# backend/api/routes/messages.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import Any, AsyncIterator, Dict, List, Optional
from pydantic import BaseModel
import asyncio
import json
import logging
from ...core.chat.chat_manager import ChatManager
from ..dependencies import get_chat_manager, get_run_manager
from ...core.config.types import Message, StreamChunk
from ...core.runs import RunKind, RunManager, RunNotFoundError, RunStatus
from ...core.slash import SlashCommandDispatcher

router = APIRouter()
logger = logging.getLogger(__name__)
_DEFAULT_RUN_MANAGER = RunManager()
_STREAM_SESSIONS: dict[str, "LegacyRunStreamSession"] = {}

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


async def _subscribe_sse(run_manager: RunManager, run_id: str, from_event: int = 0) -> AsyncIterator[str]:
    try:
        async for payload in run_manager.subscribe(run_id, from_event):
            if payload.get("type") == "run_finished":
                continue
            yield _format_sse_data(payload)
    except RunNotFoundError:
        yield _format_sse_data({"status": "error", "error": "运行不存在或已结束", "run_id": run_id})
    yield _format_sse_data("[DONE]")


async def detached_stream_event_generator(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager,
    run_manager: RunManager | None = None,
) -> AsyncIterator[str]:
    """Stream SSE events without tying generation lifetime to the client socket."""
    run_manager = _resolve_run_manager(run_manager)
    slash_result = SlashCommandDispatcher().dispatch(request.content)
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
        },
    )

    async def produce() -> None:
        final_status = RunStatus.COMPLETED
        final_error: str | None = None
        bound_node_id: str | None = None
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
                run_id=run.run_id,
            ):
                chunk_data = build_stream_chunk_data(chunk, conversation_id)
                node_id = chunk_data.get("node_id")
                if node_id and node_id != bound_node_id:
                    bound_node_id = node_id
                    await run_manager.bind_target_node(run.run_id, node_id)
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
            await run_manager.finish_run(run.run_id, final_status, final_error)
            if bound_node_id:
                _STREAM_SESSIONS.pop(bound_node_id, None)

    asyncio.create_task(produce())
    async for event in _subscribe_sse(run_manager, run.run_id, 0):
        yield event

@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
):
    """流式发送消息 - 返回 SSE 格式"""
    
    return StreamingResponse(
        detached_stream_event_generator(conversation_id, request, chat_manager, run_manager),
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
        if run.get("kind") in {RunKind.CHAT.value, RunKind.SIDE_QUESTION.value}
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
        if run.get("kind") in {RunKind.CHAT.value, RunKind.SIDE_QUESTION.value}
    ]


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
        return conversation.get_message_chain_from_node(node_id)
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
        return conversation.get_message_chain_from_node()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
