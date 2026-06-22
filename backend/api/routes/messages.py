# backend/api/routes/messages.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import Any, AsyncIterator, Dict, List, Optional
from pydantic import BaseModel
import asyncio
import json
import logging
from time import time
from ...core.chat.chat_manager import ChatManager
from ..dependencies import get_chat_manager
from ...core.config.types import Message, StreamChunk

router = APIRouter()
logger = logging.getLogger(__name__)
_STREAM_DONE = object()
_STREAM_SESSIONS: dict[str, "DetachedStreamSession"] = {}

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


class DetachedStreamSession:
    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        self.node_id: str | None = None
        self.events: list[str] = []
        self.done = False
        self.created_at = time()
        self.updated_at = self.created_at
        self._condition = asyncio.Condition()

    async def append(self, event: str, node_id: str | None = None) -> None:
        async with self._condition:
            if node_id:
                self.node_id = node_id
            self.events.append(event)
            self.updated_at = time()
            self._condition.notify_all()

    async def finish(self) -> None:
        async with self._condition:
            self.done = True
            self.updated_at = time()
            self._condition.notify_all()

    async def subscribe(self, start_index: int = 0) -> AsyncIterator[str]:
        index = max(0, start_index)
        while True:
            async with self._condition:
                while index >= len(self.events) and not self.done:
                    await self._condition.wait()
                if index < len(self.events):
                    event = self.events[index]
                    index += 1
                elif self.done:
                    break
                else:
                    continue
            yield event

    def snapshot(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "node_id": self.node_id,
            "event_count": len(self.events),
            "done": self.done,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def build_stream_chunk_data(chunk: StreamChunk, conversation_id: str) -> Dict[str, Any]:
    """将内部 StreamChunk 转成 SSE JSON payload。"""
    chunk_data: Dict[str, Any] = {
        "status": chunk.get("status", "content"),
        "content": chunk.get("content", ""),
        "node_id": chunk.get("node_id"),
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


async def detached_stream_event_generator(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager,
) -> AsyncIterator[str]:
    """Stream SSE events without tying generation lifetime to the client socket."""
    session = DetachedStreamSession(conversation_id)

    async def produce() -> None:
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
            ):
                chunk_data = build_stream_chunk_data(chunk, conversation_id)
                await session.append(
                    _format_sse_data(chunk_data),
                    chunk_data.get("node_id"),
                )
                if session.node_id:
                    _STREAM_SESSIONS[session.node_id] = session

            await session.append(_format_sse_data("[DONE]"))
        except Exception as e:
            logger.exception("Detached stream failed for conversation %s", conversation_id)
            await session.append(_format_sse_data(_stream_error_chunk(conversation_id, str(e))))
        finally:
            await session.finish()
            if session.node_id:
                _STREAM_SESSIONS.pop(session.node_id, None)

    asyncio.create_task(produce())
    async for event in session.subscribe():
        yield event

@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """流式发送消息 - 返回 SSE 格式"""
    
    return StreamingResponse(
        detached_stream_event_generator(conversation_id, request, chat_manager),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/conversations/{conversation_id}/messages/streams/active", response_model=List[Dict[str, Any]])
async def get_active_streams(conversation_id: str):
    """获取当前对话仍在生成中的可重连流。"""
    return [
        session.snapshot()
        for session in _STREAM_SESSIONS.values()
        if session.conversation_id == conversation_id and not session.done and session.node_id
    ]


@router.get("/conversations/messages/streams/active", response_model=List[Dict[str, Any]])
async def get_all_active_streams():
    """获取所有仍在生成中的可重连流。"""
    return [
        session.snapshot()
        for session in _STREAM_SESSIONS.values()
        if not session.done and session.node_id
    ]


@router.get("/conversations/{conversation_id}/messages/{node_id}/stream/attach")
async def attach_stream_message(
    conversation_id: str,
    node_id: str,
    from_event: int = 0,
):
    """重新订阅仍在运行的流式消息。"""
    session = _STREAM_SESSIONS.get(node_id)
    if not session or session.conversation_id != conversation_id:
        raise HTTPException(status_code=404, detail="流式消息不存在或已结束")

    return StreamingResponse(
        session.subscribe(from_event),
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
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """停止流式消息"""
    try:
        if not chat_manager.storage.index.get(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
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
