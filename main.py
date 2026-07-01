#!/usr/bin/env python3
import asyncio
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ---------- 导入路由 ----------
from backend.api.routes import agents, capabilities, config, conversations, messages, models, prompts, runs, slash, tool_approvals, tool_results, workflows

# ---------- 导入核心 ----------
from backend.core.chat.chat_manager import ChatManager
from backend.core.capabilities.bootstrap import (
    build_capability_registry,
    build_runtime_config_with_plugin_mcp,
)
from backend.core.model.model_manager import ModelManager
from backend.core.config.config import Config
from backend.core.agents import AgentMailbox, AgentRuntime, SubagentExecutor
from backend.core.runs import RunManager
from backend.core.workflows import WorkflowManager
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.agent_tools import register_agent_management_tools
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine
from backend.core.tools.tool_manager import ToolManager

PROJECT_ROOT = Path(__file__).resolve().parent


def uvicorn_reload_options() -> dict:
    """限制开发热重载范围，避免工具工作区文件变化重启后端。"""
    return {
        "reload_dirs": [str(PROJECT_ROOT / "backend")],
        "reload_includes": ["*.py"],
        "reload_excludes": [
            "**/__pycache__/**",
        ],
    }


app = FastAPI(
    title="AI 对话树后端",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 挂载管理器 ----------
@app.on_event("startup")
async def startup_event():
    config_manager = Config()
    capability_registry = build_capability_registry(PROJECT_ROOT, config_manager.data)
    runtime_config = build_runtime_config_with_plugin_mcp(
        config_manager.data,
        capability_registry,
    )
    model_manager = ModelManager()
    chat_storage = ChatStorage()
    prompt_storage = PromptStorage()
    tool_manager = ToolManager(runtime_config)
    await tool_manager.init()
    approval_manager = ApprovalManager()
    run_manager = RunManager()
    agent_mailbox = AgentMailbox()
    run_manager.agent_mailbox = agent_mailbox
    logical_sandbox = LogicalSandbox.for_config(runtime_config, Path.cwd())
    tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=approval_manager,
        logical_sandbox=logical_sandbox,
    )
    chat_manager = ChatManager(model_manager, chat_storage, prompt_storage, tool_manager)
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
    )
    workflow_manager.agent_runtime = agent_runtime
    register_agent_management_tools(
        tool_manager,
        agent_runtime=agent_runtime,
        subagent_executor=subagent_executor,
        workflow_manager=workflow_manager,
    )
    synthetic_followup_scheduler = messages.SyntheticFollowupScheduler(
        chat_manager=chat_manager,
        run_manager=run_manager,
    )
    synthetic_followup_scheduler.install()

    app.state.config_manager = config_manager
    app.state.project_root = PROJECT_ROOT
    app.state.capability_registry = capability_registry
    app.state.model_manager = model_manager
    app.state.tool_manager = tool_manager
    app.state.approval_manager = approval_manager
    app.state.run_manager = run_manager
    app.state.agent_mailbox = agent_mailbox
    app.state.agent_runtime = agent_runtime
    app.state.tool_orchestrator = tool_orchestrator
    app.state.chat_manager = chat_manager
    app.state.subagent_executor = subagent_executor
    app.state.workflow_manager = workflow_manager
    app.state.synthetic_followup_scheduler = synthetic_followup_scheduler

@app.on_event("shutdown")
async def shutdown_event():
    tool_manager = getattr(app.state, "tool_manager", None)
    if tool_manager:
        await tool_manager.close()

# ---------- 注册路由 ----------
app.include_router(config.router,        prefix="",        tags=["配置"])
app.include_router(conversations.router, prefix="", tags=["对话"])
app.include_router(messages.router,      prefix="", tags=["消息"])
app.include_router(models.router,        prefix="",               tags=["模型"])
app.include_router(prompts.router,        prefix="",               tags=["提示词"])
app.include_router(tool_approvals.router, prefix="", tags=["工具审批"])
app.include_router(tool_results.router, prefix="", tags=["工具结果"])
app.include_router(capabilities.router, prefix="", tags=["能力"])
app.include_router(runs.router, prefix="", tags=["运行"])
app.include_router(slash.router, prefix="", tags=["Slash"])
app.include_router(agents.router, prefix="", tags=["Agent"])
app.include_router(workflows.router, prefix="", tags=["Workflow"])

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        **uvicorn_reload_options(),
    )
