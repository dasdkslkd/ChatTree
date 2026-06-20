# backend/api/routes/messages.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
import json
from ...core.chat.chat_manager import ChatManager
from ..dependencies import get_chat_manager
from ...core.config.types import Message, StreamChunk

router = APIRouter()

class SendMessageRequest(BaseModel):
    content: str
    model_id: Optional[str] = None
    node_id: Optional[str] = None
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None


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

@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """流式发送消息 - 返回 SSE 格式"""

    async def event_generator():
        try:
            async for chunk in chat_manager.send_message_stream(
                conversation_id,
                request.content,
                request.model_id,
                request.node_id,
                reasoning_effort=request.reasoning_effort,
                thinking_enabled=request.thinking_enabled,
            ):
                # 将 StreamChunk 转换为 JSON 字符串
                chunk_data = build_stream_chunk_data(chunk, conversation_id)
                yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
            
            yield "data: [DONE]\n\n"
        except Exception as e:
            error_chunk = {
                "status": "error",
                "content": "",
                "node_id": None,
                "conversation_id": conversation_id,
                "error": str(e),
                "tokens_used": 0
            }
            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
    
    return StreamingResponse(
        event_generator(),
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
