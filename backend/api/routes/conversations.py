# backend/api/routes/conversations.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging
from pathlib import Path

from backend.api.dependencies import get_chat_manager, get_run_manager
from backend.core.chat.chat_manager import ChatManager
from backend.core.runs import RunManager
from backend.core.workspace import normalize_workspace

logger = logging.getLogger(__name__)

router = APIRouter()

class ConversationCreateRequest(BaseModel):
    title: str = ""
    prompt_id: Optional[str] = None
    workspace: Optional[Dict[str, Any]] = None

class ProjectFolderRequest(BaseModel):
    path: str
    label: Optional[str] = None

class ConversationUpdateRequest(BaseModel):
    title: str

class ConversationModelUpdateRequest(BaseModel):
    model_id: str
    provider_id: str
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None

class ConversationCompactRequest(BaseModel):
    custom_instructions: Optional[str] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    messages_to_keep: Optional[int] = None

class ConversationResponse(BaseModel):
    id: str
    title: str
    created_at: int
    updated_at: int
    model: str
    model_id: str = ""
    provider_id: str = ""
    current_node_id: Optional[str] = None
    workspace: Optional[Dict[str, Any]] = None
    total_tokens: Dict[str, int]

def _conversation_response(conversation) -> Dict[str, Any]:
    return {
        "id": conversation.metadata["id"],
        "title": conversation.metadata.get("title", ""),
        "created_at": conversation.metadata.get("created_at", 0),
        "updated_at": conversation.metadata.get("updated_at", 0),
        "model": conversation.metadata.get("model_id", "") or "",
        "model_id": conversation.metadata.get("model_id", "") or "",
        "provider_id": conversation.metadata.get("provider_id", "") or "",
        "reasoning_effort": conversation.metadata.get("reasoning_effort"),
        "thinking_enabled": conversation.metadata.get("thinking_enabled"),
        "current_node_id": conversation.current_node_id,
        "workspace": conversation.metadata.get("workspace"),
        "total_tokens": conversation.metadata.get("total_tokens", {}),
    }


def _workspace_from_project_path(path_value: str, label: Optional[str], create: bool) -> Dict[str, Any]:
    path_text = (path_value or "").strip()
    if not path_text:
        raise HTTPException(status_code=400, detail="Project path is required")

    target = Path(path_text).expanduser()
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid project path: {exc}") from exc

    if create:
        if resolved.exists():
            raise HTTPException(status_code=400, detail="Project folder already exists")
        parent = resolved.parent
        if not parent.exists() or not parent.is_dir():
            raise HTTPException(status_code=400, detail="Parent folder does not exist")
        try:
            resolved.mkdir()
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Failed to create project folder: {exc}") from exc
    elif not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Project folder does not exist")

    return normalize_workspace({
        "cwd": str(resolved),
        "workspace_roots": [str(resolved)],
        "label": label,
    })


@router.post("/projects/folders", response_model=Dict[str, Any])
async def create_project_folder(request: ProjectFolderRequest):
    """新建项目文件夹并返回对话 workspace 快照。"""
    return _workspace_from_project_path(request.path, request.label, create=True)


@router.post("/projects/folders/resolve", response_model=Dict[str, Any])
async def resolve_project_folder(request: ProjectFolderRequest):
    """使用现有项目文件夹并返回对话 workspace 快照。"""
    return _workspace_from_project_path(request.path, request.label, create=False)


@router.post("/conversations", response_model=Dict[str, Any])
async def create_conversation(
    request: ConversationCreateRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """创建新对话"""
    try:
        logger.info(f"收到创建对话请求: {request}")
        conversation = chat_manager.create_conversation(request.title, request.prompt_id, workspace=request.workspace)
        logger.info(f"对话创建成功并已保存: {conversation.metadata['id']}")
        return _conversation_response(conversation)
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
        result = await chat_manager.switch_node(conversation_id, node_id)
        if result is None:
            # 区分对话不存在与节点无效
            if chat_manager.get_conversation(conversation_id) is None:
                raise HTTPException(status_code=404, detail="对话不存在")
            raise HTTPException(status_code=400, detail="无效的节点ID")
        return {"message": "节点切换成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/conversations/{conversation_id}/branches")
async def get_branches(
    conversation_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """获取对话的所有分支"""
    try:
        conversation = chat_manager.get_conversation(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="对话不存在")
        return conversation.get_available_branches()
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
        if not await chat_manager.update_conversation_title(conversation_id, request.title):
            raise HTTPException(status_code=404, detail="对话不存在")
        return {"message": "对话标题已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/conversations/{conversation_id}/model")
async def update_conversation_model(
    conversation_id: str,
    request: ConversationModelUpdateRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """更新对话的默认模型"""
    try:
        if not await chat_manager.update_conversation_model(
            conversation_id,
            request.model_id,
            request.provider_id,
            reasoning_effort=request.reasoning_effort,
            thinking_enabled=request.thinking_enabled,
        ):
            raise HTTPException(status_code=404, detail="对话不存在")
        return {"message": "对话模型已更新"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conversations/{conversation_id}/compact")
async def compact_conversation(
    conversation_id: str,
    request: ConversationCompactRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """手动压缩当前分支上下文。"""
    try:
        return await chat_manager.compact_conversation(
            conversation_id,
            custom_instructions=request.custom_instructions,
            model_id=request.model_id,
            provider_id=request.provider_id,
            trigger="manual",
            messages_to_keep=request.messages_to_keep if request.messages_to_keep is not None else 1,
        )
    except ValueError as e:
        detail = str(e)
        if detail == "对话不存在":
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=400, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/conversations/{conversation_id}/nodes/{node_id}")
async def delete_node(
    conversation_id: str,
    node_id: str,
    force: bool = False,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
):
    """删除节点及其子节点"""
    try:
        conv = chat_manager.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="对话不存在")
        active_runs = run_manager.active_runs_for_targets(
            conversation_id=conversation_id,
            target_node_ids=conv.get_descendant_node_ids(node_id),
        )
        if active_runs and not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "该分支仍有运行中的任务，请先停止后再删除",
                    "active_run_ids": [run["run_id"] for run in active_runs],
                },
            )
        if active_runs and force:
            for run in active_runs:
                await run_manager.request_stop(str(run["run_id"]))
                if run.get("target_node_id"):
                    await chat_manager.stop_stream(str(run["target_node_id"]))
        result = await chat_manager.delete_node(conversation_id, node_id)
        if result is None:
            raise HTTPException(status_code=404, detail="对话不存在")
        return {
            "message": "节点已删除",
            "deleted_node_id": result["deleted_node_id"],
            "new_current_node_id": result["new_current_node_id"],
            "parent_node_id": result["parent_node_id"],
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
        conv = chat_manager.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="对话不存在")

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
                "total_tokens": node.get("total_tokens", 0),
                "branch_usage_info": node.get("branch_usage_info"),
                "usage": node.get("usage"),
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
    image_types = {
        "image/png", "image/jpeg", "image/gif", "image/webp",
    }
    image_exts = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    image_mime_type = content_type if content_type in image_types else image_exts.get(ext)
    is_image_type = image_mime_type is not None
    is_text_type = content_type.startswith("text/") or content_type in {
        "application/json", "application/xml",
        "application/javascript", "application/typescript",
        "application/x-yaml", "application/yaml",
    }
    if not is_image_type and not is_text_type and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type or ext}")
    raw = await file.read()
    if not is_image_type:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File is not valid UTF-8 text.")
    chat_manager.storage.save_import_file(conversation_id, file.filename or "unnamed", raw)
    return {
        "filename": file.filename,
        "size": len(raw),
        "kind": "image" if is_image_type else "file",
        "mime_type": image_mime_type if is_image_type else None,
    }


@router.get("/conversations/{conversation_id}/imports/{filename:path}")
async def read_import_file(
    conversation_id: str,
    filename: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    import os as _os
    from fastapi import Response
    from fastapi.responses import PlainTextResponse
    image_exts = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = image_exts.get(_os.path.splitext(filename or "")[1].lower())
    if media_type:
        raw = chat_manager.storage.read_import_file_bytes(conversation_id, filename)
        if raw is None:
            raise HTTPException(status_code=404, detail="File not found")
        return Response(content=raw, media_type=media_type)
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
