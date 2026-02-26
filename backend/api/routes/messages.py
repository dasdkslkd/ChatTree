# backend/api/routes/messages.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import List, Dict, Any, Optional, AsyncIterator
from pydantic import BaseModel
import json
from ...core.chat.chat_manager import ChatManager
from ...core.chat.conversation import Conversation
from ..dependencies import get_chat_manager
from ...core.config.types import Message, StreamChunk, StreamStatus

router = APIRouter()

class SendMessageRequest(BaseModel):
    content: str
    model_id: Optional[str] = None

class MessageResponse(BaseModel):
    message: str
    conversation_id: str
    node_id: Optional[str] = None

@router.post("/conversations/{conversation_id}/messages", response_model=MessageResponse)
async def send_message(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """发送消息"""
    try:
        # 确保对话已加载
        if not chat_manager.current_conversation or chat_manager.current_conversation.metadata["id"] != conversation_id:
            if not chat_manager.load_conversation(conversation_id):
                # 如果不存在，创建新对话
                chat_manager.create_conversation()
        assert chat_manager.current_conversation is not None
        response = chat_manager.send_message(request.content, request.model_id)
        
        return {
            "message": response,
            "conversation_id": conversation_id,
            "node_id": chat_manager.current_conversation.current_node_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    request: SendMessageRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """流式发送消息 - 返回 SSE 格式"""
    
    async def event_generator():
        try:
            # 确保对话已加载
            if not chat_manager.current_conversation or chat_manager.current_conversation.metadata["id"] != conversation_id:
                if not chat_manager.load_conversation(conversation_id):
                    # 如果不存在，创建新对话
                    chat_manager.create_conversation()
            
            assert chat_manager.current_conversation is not None
            
            async for chunk in chat_manager.send_message_stream(request.content, request.model_id):
                # 将 StreamChunk 转换为 JSON 字符串
                chunk_data = {
                    "status": chunk.get("status", "content"),
                    "content": chunk.get("content", ""),
                    "node_id": chunk.get("node_id"),
                    "conversation_id": chunk.get("conversation_id", conversation_id),
                    "error": chunk.get("error"),
                    "tokens_used": chunk.get("tokens_used", 0)
                }
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
        chat_manager.stop_stream(node_id)
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
        if not chat_manager.load_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        assert chat_manager.current_conversation is not None
        return chat_manager.current_conversation.get_message_chain_from_node(node_id)
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
        if not chat_manager.load_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        assert chat_manager.current_conversation is not None
        return chat_manager.current_conversation.get_message_chain_from_node()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))