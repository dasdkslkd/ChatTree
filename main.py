#!/usr/bin/env python3
import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ---------- 导入路由 ----------
from backend.api.router import api_v1_router
from backend.api.errors import RequestBoundaryMiddleware, install_error_handlers

# ---------- 导入核心 ----------
from backend.core.chat.chat_manager import ChatManager
from backend.core.capabilities.bootstrap import (
    build_capability_registry,
    build_runtime_config_with_plugin_mcp,
)
from backend.core.model.model_manager import ModelManager
from backend.core.config.config import Config, cfg
from backend.core.agents import AgentMailbox, AgentRuntime, SubagentExecutor
from backend.core.runs import RunManager
from backend.core.plans import PlanLedger
from backend.core.persistence import (
    ChatRepository,
    SQLitePersistence,
    SQLitePlanRepository,
    SQLiteRunRepository,
    SQLiteTaskRepository,
    SQLiteTaskNotificationRepository,
    TranscriptProjection,
)
from backend.core.tasks import ActiveTaskService
from backend.core.workflows import WorkflowManager
from backend.core.notifications import TaskNotificationService
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.agent_tools import register_agent_management_tools
from backend.core.tools.plan_tools import register_plan_tools
from backend.core.tools.task_tools import register_task_tools
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine
from backend.core.tools.tool_manager import ToolManager
from backend.core.storage.tool_result_storage import ToolResultStorage
from backend.core.command_runtime import CommandExecutor
from backend.core.perf import configure_profiler, get_profiler, load_perf_config
from backend.core.server import SERVER_VERSION, ServerHomeLock, ServerIdentityStore

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SERVER_PORT = 8001
SERVER_ALLOWED_ORIGINS = ("http://localhost:5173",)
SERVER_ALLOWED_ORIGIN_PATTERN = r"http://(localhost|127\.0\.0\.1):\d+"
logger = logging.getLogger(__name__)


def uvicorn_reload_options() -> dict:
    """限制开发热重载范围，避免工具工作区文件变化重启后端。"""
    return {
        "reload_dirs": [str(PROJECT_ROOT / "backend")],
        "reload_includes": ["*.py"],
        "reload_excludes": [
            "**/__pycache__/**",
        ],
    }


def uvicorn_server_options(
    environ: Mapping[str, str] | None = None,
) -> dict:
    values = os.environ if environ is None else environ
    raw_port = values.get("CHATTREE_SERVER_PORT", str(DEFAULT_SERVER_PORT))
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "CHATTREE_SERVER_PORT must be an integer from 1 to 65535"
        ) from exc
    if not 1 <= port <= 65535:
        raise ValueError(
            "CHATTREE_SERVER_PORT must be an integer from 1 to 65535"
        )
    return {"host": "127.0.0.1", "port": port}


def run_server(environ: Mapping[str, str] | None = None) -> None:
    uvicorn.run(
        "main:app",
        reload=True,
        workers=1,
        **uvicorn_reload_options(),
        **uvicorn_server_options(environ),
    )


app = FastAPI(
    title="AI 对话树后端",
    version=SERVER_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=SERVER_ALLOWED_ORIGINS,
    allow_origin_regex=SERVER_ALLOWED_ORIGIN_PATTERN,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def performance_middleware(request: Request, call_next):
    profiler = get_profiler()
    route = getattr(request.scope.get("route"), "path", None) or request.url.path
    with profiler.span(
        "http.request",
        method=request.method,
        path=route,
    ):
        response = await call_next(request)
    profiler.mark(
        "http.response",
        method=request.method,
        path=route,
        status_code=response.status_code,
    )
    return response


install_error_handlers(app)
app.add_middleware(
    RequestBoundaryMiddleware,
    allowed_origins=SERVER_ALLOWED_ORIGINS,
    allowed_origin_pattern=SERVER_ALLOWED_ORIGIN_PATTERN,
)

# ---------- 挂载管理器 ----------
async def _close_server_resources() -> list[BaseException]:
    errors: list[BaseException] = []
    for state_name in ("run_manager", "tool_manager"):
        resource = getattr(app.state, state_name, None)
        if resource is None:
            continue
        try:
            await resource.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if getattr(app.state, state_name, None) is resource:
                setattr(app.state, state_name, None)
    return errors


def _log_cleanup_errors(errors: list[BaseException]) -> None:
    for error in errors:
        logger.error(
            "ChatTree Server resource cleanup failed",
            exc_info=(type(error), error, error.__traceback__),
        )


async def _initialize_server() -> None:
    config_manager = Config()
    cfg.config_path = config_manager.config_path
    cfg.data = config_manager.data
    perf_profiler = configure_profiler(load_perf_config(config_manager.data))
    persistence = SQLitePersistence()
    persistence.initialize()
    server_identity_store = ServerIdentityStore(persistence)
    server_identity = server_identity_store.get_or_create()
    chat_repository = ChatRepository(persistence)
    transcript_projection = TranscriptProjection(persistence)
    plan_repository = SQLitePlanRepository(persistence)
    task_repository = SQLiteTaskRepository(persistence)
    run_repository = SQLiteRunRepository(persistence, task_repository=task_repository)
    task_notification_repository = SQLiteTaskNotificationRepository(persistence)
    capability_registry = build_capability_registry(PROJECT_ROOT, config_manager.data)
    runtime_config = build_runtime_config_with_plugin_mcp(
        config_manager.data,
        capability_registry,
    )
    interrupted_run_ids = run_repository.mark_unfinished_as_interrupted()
    model_manager = ModelManager()
    chat_storage = ChatStorage(str(persistence.home / "conversations"))
    prompt_storage = PromptStorage(str(persistence.home / "prompts"))
    tool_result_store = ToolResultStorage(
        str(persistence.home / "tool_results"),
        sqlite_repository=chat_repository,
    )
    tool_manager = ToolManager(runtime_config, tool_result_store=tool_result_store)
    app.state.tool_manager = tool_manager
    await tool_manager.init()
    approval_manager = ApprovalManager()
    run_manager = RunManager(repository=run_repository)
    app.state.run_manager = run_manager
    plan_ledger = PlanLedger(repository=plan_repository)
    task_service = ActiveTaskService(repository=task_repository)
    run_manager.task_service = task_service
    task_service.run_manager = run_manager
    command_executor = CommandExecutor(run_manager, task_service=task_service)
    tool_manager.command_executor = command_executor
    agent_mailbox = AgentMailbox()
    run_manager.agent_mailbox = agent_mailbox
    logical_sandbox = LogicalSandbox.for_config(runtime_config, Path.cwd())
    tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=approval_manager,
        logical_sandbox=logical_sandbox,
    )
    chat_manager = ChatManager(
        model_manager,
        chat_storage,
        prompt_storage,
        tool_manager,
        task_service=task_service,
        plan_ledger=plan_ledger,
        chat_repository=chat_repository,
        transcript_projection=transcript_projection,
    )
    chat_manager.plan_ledger = plan_ledger
    chat_manager.capability_registry = capability_registry
    chat_manager.tool_orchestrator = tool_orchestrator
    subagent_executor = SubagentExecutor(
        chat_manager=chat_manager,
        run_manager=run_manager,
        capability_registry=capability_registry,
        mailbox=agent_mailbox,
    )
    workflow_manager = WorkflowManager(
        run_manager=run_manager,
        subagent_executor=subagent_executor,
        mailbox=agent_mailbox,
    )
    agent_runtime = AgentRuntime(
        run_manager=run_manager,
        mailbox=agent_mailbox,
        subagent_executor=subagent_executor,
        workflow_manager=workflow_manager,
        capability_registry=capability_registry,
        task_service=task_service,
    )
    workflow_manager.agent_runtime = agent_runtime
    task_notification_service = TaskNotificationService(
        repository=task_notification_repository,
        run_manager=run_manager,
        chat_manager=chat_manager,
    )
    run_manager.notification_service = task_notification_service
    run_manager.add_finish_listener(task_notification_service.handle_run_finished)
    register_agent_management_tools(
        tool_manager,
        agent_runtime=agent_runtime,
        subagent_executor=subagent_executor,
        workflow_manager=workflow_manager,
    )
    register_plan_tools(tool_manager, plan_ledger)
    register_task_tools(tool_manager, task_service)
    await task_notification_service.reconcile_terminal_publications()
    app.state.persistence = persistence
    app.state.server_identity_store = server_identity_store
    app.state.server_identity = server_identity
    app.state.chat_repository = chat_repository
    app.state.transcript_projection = transcript_projection
    app.state.run_repository = run_repository
    app.state.interrupted_run_ids = interrupted_run_ids
    app.state.plan_repository = plan_repository
    app.state.task_repository = task_repository
    app.state.task_notification_repository = task_notification_repository
    app.state.config_manager = config_manager
    app.state.perf_profiler = perf_profiler
    app.state.project_root = PROJECT_ROOT
    app.state.capability_registry = capability_registry
    app.state.model_manager = model_manager
    app.state.tool_manager = tool_manager
    app.state.approval_manager = approval_manager
    app.state.run_manager = run_manager
    app.state.plan_ledger = plan_ledger
    app.state.task_service = task_service
    app.state.command_executor = command_executor
    app.state.agent_mailbox = agent_mailbox
    app.state.agent_runtime = agent_runtime
    app.state.tool_orchestrator = tool_orchestrator
    app.state.chat_manager = chat_manager
    app.state.subagent_executor = subagent_executor
    app.state.workflow_manager = workflow_manager
    app.state.task_notification_service = task_notification_service


@app.on_event("startup")
async def startup_event() -> None:
    home_lock = ServerHomeLock()
    home_lock.acquire()
    app.state.server_home_lock = home_lock
    app.state.run_manager = None
    app.state.tool_manager = None
    try:
        await _initialize_server()
    except BaseException:
        try:
            _log_cleanup_errors(await _close_server_resources())
        finally:
            home_lock.release()
            if getattr(app.state, "server_home_lock", None) is home_lock:
                app.state.server_home_lock = None
        raise


@app.on_event("shutdown")
async def shutdown_event() -> None:
    cleanup_errors: list[BaseException] = []
    try:
        cleanup_errors = await _close_server_resources()
    finally:
        home_lock = getattr(app.state, "server_home_lock", None)
        if home_lock:
            home_lock.release()
            if getattr(app.state, "server_home_lock", None) is home_lock:
                app.state.server_home_lock = None
    if cleanup_errors:
        _log_cleanup_errors(cleanup_errors[1:])
        raise cleanup_errors[0]

# ---------- 注册路由 ----------
app.include_router(api_v1_router)

if __name__ == "__main__":
    run_server()
