# backend/api/routes/conversations.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging

# 确保导入正确
from backend.api.dependencies import get_chat_manager
from backend.core.chat.chat_manager import ChatManager

logger = logging.getLogger(__name__)

router = APIRouter()

class ConversationCreateRequest(BaseModel):
    title: str = ""
    prompt_id: Optional[str] = None

class ConversationUpdateRequest(BaseModel):
    title: str

class ConversationResponse(BaseModel):
    id: str
    title: str
    created_at: int
    updated_at: int
    model: str
    total_tokens: Dict[str, int]

@router.post("/conversations", response_model=Dict[str, str])
async def create_conversation(
    request: ConversationCreateRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """创建新对话"""
    try:
        logger.info(f"收到创建对话请求: {request}")
        
        # 创建对话
        conversation = chat_manager.create_conversation(request.title, request.prompt_id)
        logger.info(f"对话创建成功: {conversation.metadata}")

        # 立即保存
        chat_manager.save_conversation()
        logger.info("对话已保存")
        
        return {
            "id": conversation.metadata["id"], 
            "title": conversation.metadata["title"],
            "message": "对话创建成功"
        }
    except Exception as e:
        logger.error(f"创建对话失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"创建对话失败: {str(e)}")

@router.get("/conversations", response_model=List[Dict[str, Any]])
async def list_conversations(chat_manager: ChatManager = Depends(get_chat_manager)):
    """获取对话列表"""
    try:
        logger.info("收到获取对话列表请求")
        conversations = chat_manager.list_conversations()
        logger.info(f"获取到 {len(conversations)} 个对话")
        return conversations
    except Exception as e:
        logger.error(f"获取对话列表失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取对话列表失败: {str(e)}")

@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """删除对话"""
    try:
        chat_manager.delete_conversation(conversation_id)
        return {"message": "对话已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/conversations/{conversation_id}/switch/{node_id}")
async def switch_node(
    conversation_id: str,
    node_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """切换到指定节点"""
    try:
        if not chat_manager.load_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        assert chat_manager.current_conversation is not None
        if chat_manager.current_conversation.switch_to_node(node_id):
            chat_manager.save_conversation()
            return {"message": "节点切换成功"}
        else:
            raise HTTPException(status_code=400, detail="无效的节点ID")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/conversations/{conversation_id}/branches")
async def get_branches(
    conversation_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取对话的所有分支"""
    try:
        if not chat_manager.load_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        assert chat_manager.current_conversation is not None
        return chat_manager.current_conversation.get_available_branches()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    request: ConversationUpdateRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """更新对话标题"""
    try:
        if not chat_manager.update_conversation_title(conversation_id, request.title):
            raise HTTPException(status_code=404, detail="对话不存在")
        return {"message": "对话标题已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/conversations/{conversation_id}/nodes/{node_id}")
async def delete_node(
    conversation_id: str,
    node_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """删除节点及其子节点"""
    try:
        if not chat_manager.load_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        assert chat_manager.current_conversation is not None
        
        # 获取父节点ID（删除前获取）
        node = chat_manager.current_conversation.nodes.get(node_id)
        parent_id = node.get("parent_id") if node else None
        
        # 删除节点
        chat_manager.current_conversation.del_node(node_id)
        
        # 保存对话
        chat_manager.save_conversation()
        
        # 返回新的当前节点ID（父节点）
        new_current_node_id = chat_manager.current_conversation.current_node_id
        
        return {
            "message": "节点已删除",
            "deleted_node_id": node_id,
            "new_current_node_id": new_current_node_id,
            "parent_node_id": parent_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))