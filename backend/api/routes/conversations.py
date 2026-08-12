# backend/api/routes/conversations.py
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import asyncio
import logging
from pathlib import Path

from backend.api.dependencies import get_chat_manager, get_config_manager, get_run_manager, get_transcript_assembler
from backend.api.errors import ApiError, ErrorEnvelope
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.config import Config, cfg
from backend.core.projects import (
    normalize_dev_environment,
    normalize_project_path,
    normalize_projects_config,
    project_id_for_workspace,
    workspace_project_path,
)
from backend.core.runs import RunManager
from backend.core.transcript import TranscriptAssembler
from backend.core.workspace import build_default_workspace, normalize_workspace

logger = logging.getLogger(__name__)

router = APIRouter()

class ConversationCreateRequest(BaseModel):
    title: str = ""
    prompt_id: Optional[str] = None
    prompt_mode: str = "override"
    workspace: Optional[Dict[str, Any]] = None
    multi_agent_mode: Optional[str] = None

class ProjectFolderRequest(BaseModel):
    path: str

class ProjectConfigUpdateRequest(BaseModel):
    path: str
    label: Optional[str] = None
    visible: Optional[bool] = None
    enabled_skills: Optional[List[str]] = None
    enabled_mcp_servers: Optional[List[str]] = None
    enabled_agents: Optional[List[str]] = None
    dev_environment: Optional[Dict[str, Any]] = None
    roots: Optional[List[str]] = None

class ProjectHistoryDeleteRequest(BaseModel):
    path: str
    force: bool = False

class ConversationUpdateRequest(BaseModel):
    title: str

class ConversationModelUpdateRequest(BaseModel):
    model_id: str
    provider_id: str
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None

class ConversationMultiAgentModeUpdateRequest(BaseModel):
    multi_agent_mode: str

class ConversationCompactRequest(BaseModel):
    custom_instructions: Optional[str] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    messages_to_keep: Optional[int] = None

class PruneSummaryRequest(BaseModel):
    custom_instructions: Optional[str] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None

class ConversationResponse(BaseModel):
    id: str
    title: str
    created_at: int
    updated_at: int
    model: str
    model_id: str = ""
    provider_id: str = ""
    current_node_id: Optional[str] = None
    multi_agent_mode: str = "explicit_request_only"
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
        "multi_agent_mode": conversation.metadata.get("multi_agent_mode", "explicit_request_only"),
        "current_node_id": conversation.current_node_id,
        "workspace": conversation.metadata.get("workspace"),
        "total_tokens": conversation.metadata.get("total_tokens", {}),
    }

def _workspace_from_project_path(path_value: str) -> Dict[str, Any]:
    path_text = (path_value or "").strip()
    if not path_text:
        raise HTTPException(status_code=400, detail="Project path is required")

    target = Path(path_text).expanduser()
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid project path: {exc}") from exc

    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Project folder does not exist")

    return normalize_workspace({
        "cwd": str(resolved),
        "workspace_roots": [str(resolved)],
    })


def _remember_workspace_project(config_manager: Config, workspace: Dict[str, Any] | None) -> None:
    path = workspace_project_path(workspace)
    if not path:
        return
    projects = normalize_projects_config(config_manager.data.get("projects"))
    project_id = project_id_for_workspace({"projects": projects}, workspace)
    project_path = next(
        (key for key, project in projects.items() if project["id"] == project_id),
        path,
    )
    current = projects.get(project_path, {})
    projects[project_path] = {
        **current,
        "label": str(current.get("label") or (workspace or {}).get("label") or Path(path).name),
        "roots": current.get("roots") or [path],
    }
    config_manager.data["projects"] = normalize_projects_config(projects)
    workspace["project_id"] = config_manager.data["projects"][project_path]["id"]
    config_manager.save()
    cfg.data = config_manager.data


@router.get("/projects", response_model=Dict[str, Any])
async def list_projects(
    chat_manager: ChatManager = Depends(get_chat_manager),
    config_manager: Config = Depends(get_config_manager),
):
    """列出项目、项目配置与对话数量。"""
    try:
        conversations = chat_manager.list_conversations()
        configured = normalize_projects_config(config_manager.data.get("projects"))
        changed = configured != config_manager.data.get("projects")
        for item in conversations:
            workspace = item.get("workspace")
            if not isinstance(workspace, dict) or project_id_for_workspace({"projects": configured}, workspace):
                continue
            path = workspace_project_path(workspace)
            if path:
                configured[path] = normalize_projects_config({
                    path: {"label": workspace.get("label") or Path(path).name},
                })[path]
                changed = True
        if changed:
            config_manager.data["projects"] = configured
            config_manager.save()
            cfg.data = config_manager.data

        projects_by_id: Dict[str, Dict[str, Any]] = {}
        for path, project_config in configured.items():
            project_id = project_config["id"]
            label = project_config.get("label") or Path(path).name or "默认项目"
            roots = project_config["roots"]
            projects_by_id[project_id] = {
                "id": project_id,
                "path": path,
                "label": label,
                "workspace": normalize_workspace({
                    "project_id": project_id,
                    "cwd": path,
                    "workspace_roots": roots,
                    "label": label,
                }),
                "conversation_count": 0,
                "latest_updated_at": 0,
                "config": project_config,
            }

        for item in conversations:
            workspace = item.get("workspace")
            project_id = project_id_for_workspace(
                {"projects": configured},
                workspace if isinstance(workspace, dict) else None,
            )
            project = projects_by_id.get(project_id)
            if project is not None:
                project["conversation_count"] += 1
                project["latest_updated_at"] = max(
                    project["latest_updated_at"],
                    item.get("updated_at", 0) or 0,
                )

        return {
            "projects": sorted(
                projects_by_id.values(),
                key=lambda item: item.get("label") or "",
            ),
            "config": configured,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/projects/{project_path:path}", response_model=Dict[str, Any])
async def update_project_config(
    project_path: str,
    payload: ProjectConfigUpdateRequest,
    http_request: Request,
    config_manager: Config = Depends(get_config_manager),
):
    """保存项目可见性与项目级能力 allowlist。"""
    path = normalize_project_path(payload.path or project_path)
    if not path:
        raise HTTPException(status_code=400, detail="Project path is required")
    projects = normalize_projects_config(config_manager.data.get("projects"))
    existing = projects.get(path, {})
    if payload.roots is not None:
        roots = [normalize_project_path(item) for item in payload.roots]
        roots = [root for root in roots if root]
        # 主项目 cwd 恒为第一根，避免 roots 与项目身份错位。
        if roots and path not in roots:
            roots.insert(0, path)
    else:
        roots = existing.get("roots", [path])
    projects[path] = {
        "id": existing.get("id"),
        "label": payload.label or existing.get("label") or Path(path).name,
        "visible": payload.visible if payload.visible is not None else existing.get("visible", True),
        "roots": roots,
        "enabled_skills": payload.enabled_skills if "enabled_skills" in payload.model_fields_set else existing.get("enabled_skills"),
        "enabled_mcp_servers": payload.enabled_mcp_servers if "enabled_mcp_servers" in payload.model_fields_set else existing.get("enabled_mcp_servers"),
        "enabled_agents": payload.enabled_agents if "enabled_agents" in payload.model_fields_set else existing.get("enabled_agents"),
        "dev_environment": (
            normalize_dev_environment(payload.dev_environment)
            if payload.dev_environment is not None
            else existing.get("dev_environment"),
        ),
    }
    config_manager.data["projects"] = normalize_projects_config(projects)
    config_manager.save()
    cfg.data = config_manager.data
    tool_manager = getattr(http_request.app.state, "tool_manager", None)
    if tool_manager is not None and hasattr(tool_manager, "_config"):
        tool_manager._config = config_manager.data
    return {"message": "项目配置已保存", "project": config_manager.data["projects"][path]}


@router.post("/projects/history/delete", response_model=Dict[str, Any])
async def delete_project_history(
    request: ProjectHistoryDeleteRequest,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    config_manager: Config = Depends(get_config_manager),
):
    """批量删除指定项目下的对话历史。"""
    target_path = normalize_project_path(request.path)
    if not target_path:
        raise HTTPException(status_code=400, detail="Project path is required")
    projects = normalize_projects_config(config_manager.data.get("projects"))
    target_project_id = project_id_for_workspace(
        {"projects": projects},
        {"cwd": target_path},
    )
    if not target_project_id:
        raise HTTPException(status_code=404, detail="Project not found")
    deleted: list[str] = []
    skipped_active: list[str] = []
    project_remembered = False
    for item in chat_manager.list_conversations():
        workspace = item.get("workspace")
        if project_id_for_workspace(
            {"projects": projects},
            workspace if isinstance(workspace, dict) else None,
        ) != target_project_id:
            continue
        if not project_remembered:
            _remember_workspace_project(
                config_manager,
                workspace if isinstance(workspace, dict) else None,
            )
            project_remembered = True
        conversation_id = item.get("id")
        if not conversation_id:
            continue
        active_runs = run_manager.list_active(str(conversation_id))
        if active_runs and not request.force:
            skipped_active.append(str(conversation_id))
            continue
        if active_runs and request.force:
            for run in active_runs:
                await run_manager.request_stop(str(run.get("run_id")))
            skipped_active.append(str(conversation_id))
            continue
        chat_manager.delete_conversation(str(conversation_id))
        deleted.append(str(conversation_id))
    return {
        "project_path": target_path,
        "deleted_count": len(deleted),
        "deleted_ids": deleted,
        "skipped_active_ids": skipped_active,
    }


@router.delete("/projects/{project_path:path}", response_model=Dict[str, Any])
async def delete_project(
    project_path: str,
    http_request: Request,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    config_manager: Config = Depends(get_config_manager),
):
    """删除项目配置及其全部对话历史。"""
    path = normalize_project_path(project_path)
    if not path:
        raise HTTPException(status_code=400, detail="Project path is required")
    projects = normalize_projects_config(config_manager.data.get("projects"))
    target_project_id = project_id_for_workspace({"projects": projects}, {"cwd": path})
    if not target_project_id:
        raise HTTPException(status_code=404, detail="Project not found")
    deleted: list[str] = []
    skipped_active: list[str] = []
    for item in chat_manager.list_conversations():
        workspace = item.get("workspace")
        workspace = workspace if isinstance(workspace, dict) else {}
        if project_id_for_workspace({"projects": projects}, workspace) != target_project_id:
            continue
        conversation_id = item.get("id")
        if not conversation_id:
            continue
        active_runs = run_manager.list_active(str(conversation_id))
        if active_runs:
            for run in active_runs:
                await run_manager.request_stop(str(run.get("run_id")))
            skipped_active.append(str(conversation_id))
            continue
        chat_manager.delete_conversation(str(conversation_id))
        deleted.append(str(conversation_id))
    projects = {
        key: project
        for key, project in projects.items()
        if project["id"] != target_project_id
    }
    config_manager.data["projects"] = normalize_projects_config(projects)
    config_manager.save()
    cfg.data = config_manager.data
    tool_manager = getattr(http_request.app.state, "tool_manager", None)
    if tool_manager is not None and hasattr(tool_manager, "_config"):
        tool_manager._config = config_manager.data
    return {
        "project_path": path,
        "deleted_count": len(deleted),
        "deleted_ids": deleted,
        "skipped_active_ids": skipped_active,
    }


@router.post("/projects/folders/resolve", response_model=Dict[str, Any])
async def resolve_project_folder(
    request: ProjectFolderRequest,
    config_manager: Config = Depends(get_config_manager),
):
    """登记项目文件夹并返回对话 workspace 快照。"""
    workspace = _workspace_from_project_path(request.path)
    _remember_workspace_project(config_manager, workspace)
    return workspace


@router.post("/conversations", response_model=Dict[str, Any])
async def create_conversation(
    request: ConversationCreateRequest,
    chat_manager: ChatManager = Depends(get_chat_manager),
    config_manager: Config = Depends(get_config_manager),
):
    """创建新对话"""
    try:
        logger.info(f"收到创建对话请求: {request}")
        workspace = normalize_workspace(
            request.workspace,
            build_default_workspace(config_manager.data),
        )
        _remember_workspace_project(config_manager, workspace)
        conversation = chat_manager.create_conversation(
            request.title,
            request.prompt_id,
            prompt_mode=request.prompt_mode,
            workspace=workspace,
            multi_agent_mode=request.multi_agent_mode,
        )
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
    chat_manager: ChatManager = Depends(get_chat_manager),
    config_manager: Config = Depends(get_config_manager),
):
    """删除对话"""
    try:
        conversation = chat_manager.get_conversation(conversation_id)
        if conversation is not None:
            _remember_workspace_project(config_manager, conversation.metadata.get("workspace"))
        chat_manager.delete_conversation(conversation_id)
        return {"message": "对话已删除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/conversations/{conversation_id}/transcript")
async def get_conversation_transcript(
    conversation_id: str,
    node_id: Optional[str] = None,
    assembler: TranscriptAssembler = Depends(get_transcript_assembler),
    run_manager: RunManager = Depends(get_run_manager),
):
    """获取后端 canonical transcript 快照。"""
    try:
        active_streams = []
        for run in run_manager.list_active(conversation_id):
            run_id = str(run.get("run_id") or run.get("id") or "")
            if not run_id or not run.get("target_node_id"):
                continue
            stream = assembler.stream_from_run_events(run_id, run_manager.read_events(run_id, 0))
            stream["conversation_id"] = conversation_id
            stream["target_node_id"] = run.get("target_node_id")
            stream["node_id"] = run.get("target_node_id")
            active_streams.append(stream)
        return assembler.snapshot(conversation_id, node_id, active_streams)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Transcript branch not found") from exc
    except HTTPException:
        raise
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


@router.patch("/conversations/{conversation_id}/multi-agent-mode")
async def update_conversation_multi_agent_mode(
    conversation_id: str,
    request: ConversationMultiAgentModeUpdateRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """更新对话级 multi-agent 工具暴露策略"""
    try:
        if request.multi_agent_mode not in {"none", "explicit_request_only", "proactive"}:
            raise HTTPException(status_code=400, detail="无效的 multi-agent mode")
        if not await chat_manager.update_conversation_multi_agent_mode(
            conversation_id,
            request.multi_agent_mode,
        ):
            raise HTTPException(status_code=404, detail="对话不存在")
        return {"message": "multi-agent mode 已更新", "multi_agent_mode": request.multi_agent_mode}
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


@router.post("/conversations/{conversation_id}/nodes/{node_id}/prune-summary")
async def prune_summary(
    conversation_id: str,
    node_id: str,
    request: PruneSummaryRequest,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """总结指定父节点下的所有子分支，并保存为父节点上下文附件。"""
    try:
        return await chat_manager.prune_summary(
            conversation_id,
            node_id,
            custom_instructions=request.custom_instructions,
            model_id=request.model_id,
            provider_id=request.provider_id,
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


@router.delete(
    "/conversations/{conversation_id}/nodes/{node_id}",
    responses={409: {"model": ErrorEnvelope}},
)
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
        subtree_node_ids = [str(node) for node in conv.get_descendant_node_ids(node_id)]
        active_runs = run_manager.active_runs_for_targets(
            conversation_id=conversation_id,
            target_node_ids=subtree_node_ids,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if active_runs and not force:
        raise ApiError(
            409,
            "active_runs_present",
            "该分支仍有运行中的任务，请先停止后再删除",
            True,
            details={
                "active_run_ids": [str(run["run_id"]) for run in active_runs]
            },
        )

    try:
        if active_runs and force:
            for run in active_runs:
                await run_manager.request_stop(str(run["run_id"]))
                if run.get("target_node_id"):
                    await chat_manager.stop_stream(str(run["target_node_id"]))
            # 等待被停止的 run 落入终态，确保下方清理能一并删除；
            # 超时不阻塞删除，残留的悬空 run 会在下次删除分支时被清扫。
            for run in active_runs:
                try:
                    await asyncio.wait_for(
                        run_manager.wait_for_terminal_result(
                            str(run["run_id"]), result_event_types=(),
                        ),
                        timeout=10,
                    )
                except Exception:
                    logger.warning(
                        "Timed out waiting for run %s to terminate before node deletion",
                        run["run_id"],
                    )
        result = await chat_manager.delete_node(conversation_id, node_id)
        if result is None:
            raise HTTPException(status_code=404, detail="对话不存在")
        run_manager.repository.delete_runs_for_deleted_nodes(
            conversation_id, subtree_node_ids,
        )
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
        node_ids = [str(node_id) for node_id in conv.nodes.keys()]
        messages_by_node = chat_manager.canonical_messages_by_node(conversation_id, node_ids)
        for node_id, node in conv.nodes.items():
            node_messages = messages_by_node.get(str(node_id), [])
            user_message = next(
                (
                    message for message in node_messages
                    if message.get("role") == "user"
                    and not message.get("is_hidden_from_transcript")
                    and not message.get("is_visible_in_transcript_only")
                    and message.get("subtype") not in {"compact_summary", "prune_summary"}
                ),
                {},
            )
            user_subtype = user_message.get("subtype") or None
            user_content = str(user_message.get("content") or "")
            assistant_message = next(
                (
                    message for message in reversed(node_messages)
                    if message.get("role") == "assistant"
                    and message.get("subtype") in {None, "", "assistant_answer"}
                    and not message.get("is_hidden_from_transcript")
                ),
                {},
            )
            assistant_content = str(assistant_message.get("content") or "")

            # 处理 parent_id: 根节点的 parent_id 为 "None" 字符串，转为 null
            parent_id = node.get("parent_id")
            if parent_id == "None" or parent_id is None:
                parent_id = None

            nodes.append({
                "id": node_id,
                "parent_id": parent_id,
                "children_ids": node.get("children_ids", []),
                "user_content": user_content,
                "user_subtype": user_subtype,
                "assistant_content": assistant_content,
                "model_id": node.get("model_id"),
                "task_context_mode": node.get("task_context_mode") or "attached",
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
