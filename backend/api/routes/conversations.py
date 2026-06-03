# backend/api/routes/conversations.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging

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
        conversation = chat_manager.create_conversation(request.title, request.prompt_id)
        logger.info(f"对话创建成功: {conversation.metadata}")
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
        node = chat_manager.current_conversation.nodes.get(node_id)
        parent_id = node.get("parent_id") if node else None
        chat_manager.current_conversation.del_node(node_id)
        chat_manager.save_conversation()
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


@router.get("/conversations/{conversation_id}/tree")
async def get_conversation_tree(
    conversation_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取对话的完整树结构（节点+边），用于图渲染"""
    try:
        if not chat_manager.load_conversation(conversation_id):
            raise HTTPException(status_code=404, detail="对话不存在")
        assert chat_manager.current_conversation is not None

        conv = chat_manager.current_conversation
        nodes = []
        for node_id, node in conv.nodes.items():
            user_content = ""
            if node.get("user_message"):
                user_content = node["user_message"].get("content", "")

            assistant_content = ""
            if node.get("assistant_message"):
                assistant_content = node["assistant_message"].get("content", "")

            # 处理 parent_id: 根节点的 parent_id 为 "None" 字符串，转为 null
            parent_id = node.get("parent_id")
            if parent_id == "None" or parent_id is None:
                parent_id = None

            nodes.append({
                "id": node_id,
                "parent_id": parent_id,
                "children_ids": node.get("children_ids", []),
                "user_content": user_content,
                "assistant_content": assistant_content,
                "model_id": node.get("model_id"),
                "timestamp": node.get("timestamp"),
                "is_current": node_id == conv.current_node_id,
                "is_root": node_id == conv.root_node_id,
            })

        return {
            "root_node_id": conv.root_node_id,
            "current_node_id": conv.current_node_id,
            "nodes": nodes,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -- Import file management --

@router.post("/conversations/{conversation_id}/imports")
async def upload_import_file(
    conversation_id: str,
    file: UploadFile = File(...),
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    import os as _os
    allowed_exts = {
        ".txt", ".md", ".csv", ".html", ".htm", ".py", ".js", ".ts",
        ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
        ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
        ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1",
        ".sql", ".r", ".lua", ".perl", ".pl", ".ex", ".exs",
        ".vue", ".svelte", ".css", ".scss", ".less", ".sass",
        ".env", ".gitignore", ".dockerfile", ".makefile",
        ".log", ".conf", ".properties",
    }
    ext = _os.path.splitext(file.filename or "")[1].lower()
    content_type = file.content_type or ""
    is_text_type = content_type.startswith("text/") or content_type in {
        "application/json", "application/xml",
        "application/javascript", "application/typescript",
        "application/x-yaml", "application/yaml",
    }
    if not is_text_type and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type or ext}")
    raw = await file.read()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text.")
    chat_manager.storage.save_import_file(conversation_id, file.filename or "unnamed", raw)
    return {"filename": file.filename, "size": len(raw)}


@router.get("/conversations/{conversation_id}/imports/{filename:path}")
async def read_import_file(
    conversation_id: str,
    filename: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    from fastapi.responses import PlainTextResponse
    data = chat_manager.storage.read_import_file(conversation_id, filename)
    if data is None:
        raise HTTPException(status_code=404, detail="File not found")
    return PlainTextResponse(data)


@router.get("/conversations/{conversation_id}/imports")
async def list_import_files(
    conversation_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    return chat_manager.storage.list_import_files(conversation_id)


@router.delete("/conversations/{conversation_id}/imports/{filename:path}")
async def delete_import_file(
    conversation_id: str,
    filename: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    ok = chat_manager.storage.delete_import_file(conversation_id, filename)
    if not ok:
        raise HTTPException(status_code=404, detail="File not found")
    return {"message": "File deleted"}
