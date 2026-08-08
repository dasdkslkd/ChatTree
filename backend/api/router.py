from __future__ import annotations

from fastapi import APIRouter

from backend.api.errors import ErrorEnvelope
from backend.api.routes import (
    agents,
    capabilities,
    config,
    conversations,
    files,
    messages,
    models,
    notifications,
    openai_proxy,
    perf,
    plans,
    prompts,
    runs,
    slash,
    storage,
    tasks,
    tool_approvals,
    tool_results,
    workflows,
)
from backend.api.routes import server as server_routes


_BUSINESS_ROUTERS = (
    (config.router, ["配置"]),
    (conversations.router, ["对话"]),
    (files.router, ["文件"]),
    (messages.router, ["消息"]),
    (models.router, ["模型"]),
    (prompts.router, ["提示词"]),
    (tool_approvals.router, ["工具审批"]),
    (tool_results.router, ["工具结果"]),
    (capabilities.router, ["能力"]),
    (runs.router, ["运行"]),
    (plans.router, ["计划"]),
    (tasks.router, ["任务"]),
    (notifications.router, ["任务通知"]),
    (perf.router, ["Performance"]),
    (slash.router, ["Slash"]),
    (storage.router, ["存储"]),
    (agents.router, ["Agent"]),
    (workflows.router, ["Workflow"]),
    (openai_proxy.router, ["反向代理"]),
)

def _build_business_router() -> APIRouter:
    router = APIRouter()
    for child_router, tags in _BUSINESS_ROUTERS:
        router.include_router(child_router, tags=tags)
    return router

api_v1_router = APIRouter(
    prefix="/api/v1",
    responses={422: {"model": ErrorEnvelope}},
)
api_v1_router.include_router(server_routes.router, tags=["Server"])
api_v1_router.include_router(_build_business_router())
