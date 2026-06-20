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
from backend.api.routes import config, conversations, messages, models, prompts, tool_approvals

# ---------- 导入核心 ----------
from backend.core.chat.chat_manager import ChatManager
from backend.core.model.model_manager import ModelManager
from backend.core.config.config import Config
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.tools.orchestrator import ToolOrchestrator
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
    model_manager = ModelManager()
    chat_storage = ChatStorage()
    prompt_storage = PromptStorage()
    tool_manager = ToolManager(config_manager.data)
    await tool_manager.init()
    approval_manager = ApprovalManager()
    logical_sandbox = LogicalSandbox.for_config(config_manager.data, Path.cwd())
    tool_orchestrator = ToolOrchestrator(
        tool_manager=tool_manager,
        permission_engine=PermissionEngine.default(),
        approval_manager=approval_manager,
        logical_sandbox=logical_sandbox,
    )
    chat_manager = ChatManager(model_manager, chat_storage, prompt_storage, tool_manager)
    chat_manager.tool_orchestrator = tool_orchestrator

    app.state.config_manager = config_manager
    app.state.model_manager = model_manager
    app.state.tool_manager = tool_manager
    app.state.approval_manager = approval_manager
    app.state.tool_orchestrator = tool_orchestrator
    app.state.chat_manager = chat_manager

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

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        **uvicorn_reload_options(),
    )
