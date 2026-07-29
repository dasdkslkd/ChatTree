# backend/api/routes/config.py
from fastapi import APIRouter, Depends, HTTPException, Request
from pathlib import Path
from typing import Dict, Any, Optional, List, Literal
from pydantic import BaseModel
from ...core.capabilities.bootstrap import build_runtime_config_with_plugin_mcp
from ...core.agents import AgentMailbox, AgentRuntime
from ...core.auth import subscription as sub_mod
from ...core.config.config import Config, cfg
from ...core.model.model_manager import ModelManager
from ...core.projects import normalize_projects_config
from ...core.persistence import SQLitePersistence, SQLitePlanRepository, SQLiteTaskRepository
from ...core.plans import PlanLedger
from ...core.notifications import TaskNotificationService
from ...core.tasks import ActiveTaskService
from ...core.tools.orchestrator import ToolOrchestrator
from ...core.tools.agent_tools import register_agent_management_tools
from ...core.tools.plan_tools import register_plan_tools
from ...core.tools.task_tools import register_task_tools
from ...core.tools.security.approval import ApprovalManager
from ...core.tools.security.logical_sandbox import LogicalSandbox
from ...core.tools.security.permissions import PermissionEngine
from ...core.tools.tool_manager import ToolManager
from ...core.command_runtime import CommandExecutor
from ..dependencies import get_config_manager, get_tool_manager

router = APIRouter()


class ConfigUpdateRequest(BaseModel):
    default_provider: Optional[str] = None
    default_model: Optional[str] = None
    context_window: Optional[Literal[200_000, 400_000, 600_000]] = None
    provider_configs: Optional[Dict[str, Dict[str, Any]]] = None
    tools: Optional[Dict[str, Any]] = None
    projects: Optional[Dict[str, Dict[str, Any]]] = None


class AddProviderRequest(BaseModel):
    id: str
    name: str
    api_format: str = "chat_completions"
    base_url: str = ""
    api_key: str = ""
    auth: Optional[Dict[str, Any]] = None


class SubscriptionLoginRequest(BaseModel):
    subscription: str
    enterprise_domain: Optional[str] = None


class SubscriptionPollRequest(BaseModel):
    subscription: str
    handle: Dict[str, Any]


class CliImportRequest(BaseModel):
    subscription: str


def _provider_config_without_default_model(conf: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(conf)
    cleaned.pop("default_model", None)
    return cleaned


def _sync_runtime_managers(app, config_data: Dict[str, Any], model_manager, tool_manager: ToolManager) -> None:
    app.state.model_manager = model_manager
    app.state.tool_manager = tool_manager
    chat_manager = getattr(app.state, 'chat_manager', None)
    capability_registry = getattr(app.state, 'capability_registry', None)
    if capability_registry is None and chat_manager is not None:
        capability_registry = getattr(chat_manager, 'capability_registry', None)
    if capability_registry is not None:
        app.state.capability_registry = capability_registry

    old_orchestrator = getattr(app.state, 'tool_orchestrator', None)
    approval_manager = getattr(app.state, 'approval_manager', None) or getattr(
        old_orchestrator,
        'approval_manager',
        None,
    )
    if approval_manager is None:
        approval_manager = ApprovalManager()
    app.state.approval_manager = approval_manager

    logical_sandbox = LogicalSandbox.for_config(config_data, Path.cwd())
    if old_orchestrator:
        old_orchestrator.tool_manager = tool_manager
        old_orchestrator.permission_engine = PermissionEngine.default()
        old_orchestrator.approval_manager = approval_manager
        old_orchestrator.logical_sandbox = logical_sandbox
        tool_orchestrator = old_orchestrator
    else:
        tool_orchestrator = ToolOrchestrator(
            tool_manager=tool_manager,
            permission_engine=PermissionEngine.default(),
            approval_manager=approval_manager,
            logical_sandbox=logical_sandbox,
        )
        app.state.tool_orchestrator = tool_orchestrator

    if chat_manager is not None:
        chat_manager.model_manager = model_manager
        chat_manager.tool_manager = tool_manager
        chat_manager.tool_orchestrator = tool_orchestrator
        chat_manager.capability_registry = capability_registry
    subagent_executor = getattr(app.state, 'subagent_executor', None)
    run_manager = getattr(app.state, 'run_manager', None)
    plan_ledger = getattr(app.state, 'plan_ledger', None)
    if plan_ledger is None:
        plan_repository = getattr(app.state, 'plan_repository', None)
        persistence = getattr(app.state, 'persistence', None)
        if persistence is None:
            config_manager = getattr(app.state, 'config_manager', None)
            config_path = getattr(config_manager, 'config_path', None)
            home = Path(config_path).parent if config_path else None
            persistence = SQLitePersistence(home)
            persistence.initialize()
            app.state.persistence = persistence
        if plan_repository is None:
            plan_repository = SQLitePlanRepository(persistence)
            app.state.plan_repository = plan_repository
        plan_ledger = PlanLedger(repository=plan_repository)
        app.state.plan_ledger = plan_ledger
    task_service = getattr(app.state, 'task_service', None)
    task_notification_service = getattr(app.state, 'task_notification_service', None)
    if task_service is None:
        persistence = getattr(app.state, 'persistence', None)
        task_repository = SQLiteTaskRepository(persistence) if persistence is not None else None
        task_service = ActiveTaskService(repository=task_repository)
        app.state.task_service = task_service
    if task_notification_service is None:
        persistence = getattr(app.state, 'persistence', None)
        if persistence is not None:
            task_notification_service = TaskNotificationService(persistence)
            app.state.task_notification_service = task_notification_service
    if run_manager is not None:
        run_manager.task_service = task_service
        task_service.run_manager = run_manager
    if chat_manager is not None:
        chat_manager.task_service = task_service
        chat_manager.plan_ledger = plan_ledger
    command_executor = getattr(app.state, 'command_executor', None)
    if command_executor is None and run_manager is not None:
        command_executor = CommandExecutor(run_manager, task_service=task_service)
        app.state.command_executor = command_executor
    elif command_executor is not None and hasattr(command_executor, "__dict__"):
        command_executor.task_service = task_service
    tool_manager.command_executor = command_executor
    agent_mailbox = getattr(app.state, 'agent_mailbox', None)
    if agent_mailbox is None:
        agent_mailbox = AgentMailbox()
        app.state.agent_mailbox = agent_mailbox
    if subagent_executor is not None:
        subagent_executor.chat_manager = chat_manager
        subagent_executor.capability_registry = capability_registry
        subagent_executor.mailbox = agent_mailbox
    workflow_manager = getattr(app.state, 'workflow_manager', None)
    if workflow_manager is not None and subagent_executor is not None:
        workflow_manager.subagent_executor = subagent_executor
        workflow_manager.mailbox = agent_mailbox
    agent_runtime = getattr(app.state, 'agent_runtime', None)
    wait_agent_timeout = float(config_data.get("tools", {}).get("wait_agent_timeout_seconds", 30.0))
    if (
        agent_runtime is None
        and run_manager is not None
        and subagent_executor is not None
        and capability_registry is not None
    ):
        agent_runtime = AgentRuntime(
            run_manager=run_manager,
            mailbox=agent_mailbox,
            subagent_executor=subagent_executor,
            workflow_manager=workflow_manager,
            capability_registry=capability_registry,
            task_service=task_service,
            default_wait_timeout_seconds=wait_agent_timeout,
        )
        app.state.agent_runtime = agent_runtime
    elif agent_runtime is not None:
        agent_runtime.run_manager = run_manager
        agent_runtime.mailbox = agent_mailbox
        agent_runtime.subagent_executor = subagent_executor
        agent_runtime.workflow_manager = workflow_manager
        agent_runtime.capability_registry = capability_registry
        agent_runtime.task_service = task_service
        agent_runtime.default_wait_timeout_seconds = wait_agent_timeout
    if workflow_manager is not None:
        workflow_manager.agent_runtime = agent_runtime
    register_agent_management_tools(
        tool_manager,
        agent_runtime=agent_runtime,
        subagent_executor=subagent_executor,
        workflow_manager=workflow_manager,
    )
    register_plan_tools(tool_manager, plan_ledger)
    register_task_tools(tool_manager, task_service)


def _runtime_config_for_app(app, config_data: Dict[str, Any]) -> Dict[str, Any]:
    capability_registry = getattr(app.state, 'capability_registry', None)
    if capability_registry is None:
        chat_manager = getattr(app.state, 'chat_manager', None)
        capability_registry = getattr(chat_manager, 'capability_registry', None)
    if capability_registry is None:
        return config_data
    return build_runtime_config_with_plugin_mcp(config_data, capability_registry)


@router.get("/config", response_model=Dict[str, Any])
async def get_config(config_manager: Config = Depends(get_config_manager)):
    """获取配置"""
    try:
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data
        return config_manager.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools/mcp/status", response_model=Dict[str, Any])
async def get_mcp_status(tool_manager: ToolManager = Depends(get_tool_manager)):
    """获取 MCP 运行时状态"""
    try:
        return await tool_manager.describe_inventory_async()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools/builtin/web/status", response_model=Dict[str, Any])
async def get_builtin_web_status(config_manager: Config = Depends(get_config_manager)):
    """获取内置联网工具配置和 SearXNG 可用性"""
    tools = config_manager.data.get("tools") or {}
    builtin = tools.get("builtin") or {}
    web_search = tools.get("web_search") or builtin.get("web_search") or {}
    searxng = web_search.get("searxng") or web_search.get("searxng_config") or {}
    enabled = (
        tools.get("enabled", True) is not False
        and builtin.get("enabled", True) is not False
        and web_search.get("enabled", True) is not False
    )
    searxng_url = str(searxng.get("searxng_url") or "http://localhost:8888").rstrip("/")
    result: Dict[str, Any] = {
        "enabled": enabled,
        "searxng_url": searxng_url,
        "available": False,
        "status_code": None,
        "error": None,
    }
    if not enabled:
        result["error"] = "web_search is disabled"
        return result
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(
                f"{searxng_url}/search",
                params={"q": "ChatTree", "format": "json", "language": searxng.get("language") or "zh-CN"},
                headers={"Accept": "application/json"},
            )
        result["status_code"] = response.status_code
        result["available"] = response.status_code < 400
        if not result["available"]:
            result["error"] = f"HTTP {response.status_code}"
    except Exception as e:
        result["error"] = str(e)
    return result


@router.post("/tools/mcp/servers/{server_name}/connect", response_model=Dict[str, Any])
async def connect_mcp_server(
    server_name: str,
    tool_manager: ToolManager = Depends(get_tool_manager),
):
    """连接或重连指定 MCP Server"""
    try:
        return await tool_manager.connect_mcp_server(server_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP Server {server_name} 不存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tools/mcp/servers/{server_name}/disconnect", response_model=Dict[str, Any])
async def disconnect_mcp_server(
    server_name: str,
    tool_manager: ToolManager = Depends(get_tool_manager),
):
    """断开指定 MCP Server 的运行时连接"""
    try:
        return await tool_manager.disconnect_mcp_server(server_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP Server {server_name} 不存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/config", response_model=Dict[str, str])
async def update_config(
    request: ConfigUpdateRequest,
    http_request: Request,
    config_manager: Config = Depends(get_config_manager)
):
    """更新配置"""
    try:
        rebuild_runtime = (
            bool(request.provider_configs)
            or request.tools is not None
            or request.projects is not None
        )
        if request.provider_configs:
            for provider, conf in request.provider_configs.items():
                config_manager.data['provider'][provider] = _provider_config_without_default_model(conf)
        if request.tools is not None:
            config_manager.data['tools'] = request.tools
        if request.projects is not None:
            config_manager.data['projects'] = normalize_projects_config(request.projects)
        if request.default_provider is not None:
            config_manager.data['default_provider'] = request.default_provider
        if request.default_model is not None:
            config_manager.data['default_model'] = request.default_model
        if 'context_window' in request.model_fields_set:
            config_manager.data['context_window'] = request.context_window
        config_manager.save()

        # 同步全局配置与运行中的模型管理器
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data

        if not rebuild_runtime:
            return {"message": "配置已更新"}

        model_manager = ModelManager()
        old_tool_manager = getattr(http_request.app.state, 'tool_manager', None)
        if old_tool_manager:
            await old_tool_manager.close()
        runtime_config = _runtime_config_for_app(http_request.app, config_manager.data)
        tool_manager = ToolManager(runtime_config)
        await tool_manager.init()
        _sync_runtime_managers(http_request.app, runtime_config, model_manager, tool_manager)

        return {"message": "配置已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/providers", response_model=Dict[str, str])
async def add_provider(
    request: AddProviderRequest,
    http_request: Request,
    config_manager: Config = Depends(get_config_manager)
):
    """添加新提供商"""
    try:
        provider_id = request.id.strip()
        if not provider_id:
            raise HTTPException(status_code=400, detail="提供商ID不能为空")

        # 检查是否已存在
        if provider_id in config_manager.data.get('provider', {}):
            raise HTTPException(status_code=409, detail=f"提供商 {provider_id} 已存在")

        new_config = {
            'name': request.name,
            'api_format': request.api_format,
            'base_url': request.base_url,
            'api_key': request.api_key,
        }
        if request.auth:
            new_config['auth'] = request.auth
        config_manager.add_provider(provider_id, new_config)

        # 同步
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data

        model_manager = ModelManager()
        old_tool_manager = getattr(http_request.app.state, 'tool_manager', None)
        if old_tool_manager:
            await old_tool_manager.close()
        runtime_config = _runtime_config_for_app(http_request.app, config_manager.data)
        tool_manager = ToolManager(runtime_config)
        await tool_manager.init()
        _sync_runtime_managers(http_request.app, runtime_config, model_manager, tool_manager)

        return {"message": f"提供商 {provider_id} 已添加"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/config/providers/{provider_id}", response_model=Dict[str, str])
async def delete_provider(
    provider_id: str,
    http_request: Request,
    config_manager: Config = Depends(get_config_manager)
):
    """删除提供商"""
    try:
        if not config_manager.delete_provider(provider_id):
            raise HTTPException(status_code=404, detail=f"提供商 {provider_id} 不存在")

        # 同步
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data

        model_manager = ModelManager()
        old_tool_manager = getattr(http_request.app.state, 'tool_manager', None)
        if old_tool_manager:
            await old_tool_manager.close()
        runtime_config = _runtime_config_for_app(http_request.app, config_manager.data)
        tool_manager = ToolManager(runtime_config)
        await tool_manager.init()
        _sync_runtime_managers(http_request.app, runtime_config, model_manager, tool_manager)

        return {"message": f"提供商 {provider_id} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 订阅式 provider：OAuth 登录、模型发现、额度查询 ────────────────────────
@router.post("/config/providers/{provider_id}/auth/login")
async def start_provider_login(
    provider_id: str,
    request: SubscriptionLoginRequest,
    config_manager: Config = Depends(get_config_manager),
):
    """启动订阅 OAuth 登录，返回 verification_uri + user_code + handle。"""
    try:
        handle = await sub_mod.start_login(
            request.subscription,
            enterprise_domain=request.enterprise_domain,
        )
        return handle
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/providers/{provider_id}/auth/poll")
async def poll_provider_login(
    provider_id: str,
    request: SubscriptionPollRequest,
    config_manager: Config = Depends(get_config_manager),
):
    """轮询登录结果；成功则写入 cfg.data['provider'][id]['auth']。"""
    try:
        auth = await sub_mod.poll_login(request.subscription, request.handle)
        if auth is None:
            return {"status": "pending"}
        if provider_id not in config_manager.data.get("provider", {}):
            raise HTTPException(status_code=404, detail=f"提供商 {provider_id} 不存在")
        config_manager.data["provider"][provider_id]["auth"] = auth
        config_manager.save()
        cfg.data = config_manager.data
        return {"status": "ok", "auth": auth}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/providers/{provider_id}/auth/cli-import")
async def import_cli_credentials(
    provider_id: str,
    request: CliImportRequest,
    config_manager: Config = Depends(get_config_manager),
):
    """从 CLI 工具的本地凭据文件导入（Claude/Codex/Gemini）。"""
    try:
        auth = await sub_mod.read_cli_credentials(request.subscription)
        if auth is None:
            raise HTTPException(status_code=404, detail="未找到 CLI 凭据")
        if provider_id not in config_manager.data.get("provider", {}):
            raise HTTPException(status_code=404, detail=f"提供商 {provider_id} 不存在")
        config_manager.data["provider"][provider_id]["auth"] = auth
        config_manager.save()
        cfg.data = config_manager.data
        return {"status": "ok", "auth": auth}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config/providers/{provider_id}/quota")
async def get_provider_quota(
    provider_id: str,
    config_manager: Config = Depends(get_config_manager),
):
    """查询订阅 provider 的额度。"""
    pc = config_manager.data.get("provider", {}).get(provider_id) or {}
    auth = pc.get("auth") or {}
    if not auth.get("subscription"):
        raise HTTPException(status_code=400, detail="非订阅 provider，不支持额度查询")
    try:
        return await sub_mod.query_quota(auth)
    except sub_mod.SubscriptionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/providers/{provider_id}/models/refresh")
async def refresh_provider_models(
    provider_id: str,
    config_manager: Config = Depends(get_config_manager),
):
    """强制刷新 provider 的模型列表（订阅 provider 走 OAuth 模型端点）。"""
    pc = config_manager.data.get("provider", {}).get(provider_id) or {}
    auth = pc.get("auth") or {}
    try:
        if auth.get("subscription"):
            models = await sub_mod.fetch_models(auth)
            ids = [m["id"] for m in models]
        else:
            from ...core.model.providers.model_fetch import fetch_models
            models = fetch_models(
                base_url=pc.get("base_url", ""),
                api_key=pc.get("api_key", ""),
                models_url_override=pc.get("models_url_override"),
                custom_user_agent=pc.get("custom_user_agent"),
            )
            ids = [m["id"] for m in models]
        if ids:
            pc["models"] = ids
            config_manager.save()
            cfg.data = config_manager.data
        return {"models": ids}
    except sub_mod.SubscriptionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
