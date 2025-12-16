# backend/api/routes/messages.py
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from ...core.chat.chat_manager import ChatManager
from ...core.chat.conversation import Conversation
from ..dependencies import get_chat_manager
from ...core.config.types import Message

router = APIRouter()

class SendMessageRequest(BaseModel):
    content: str
    model_id: Optional[str] = None

class MessageResponse(BaseModel):
    message: str
    conversation_id: str
    node_id: Optional[str] = None

@router.post("/{conversation_id}/messages", response_model=MessageResponse)
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

@router.get("/{conversation_id}/messages", response_model=List[Message])
async def get_messages(
    conversation_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取消息历史"""
    try:
        if not chat_manager.load_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        
        return chat_manager.get_conversation_history()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))