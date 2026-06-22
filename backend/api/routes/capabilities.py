from typing import Any, Dict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies import get_capability_registry, get_config_manager
from backend.api.routes.config import _sync_runtime_managers
from backend.core.capabilities.bootstrap import (
    build_capability_registry,
    build_runtime_config_with_plugin_mcp,
)
from backend.core.capabilities.prompting import build_available_capabilities_prompt
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.config.config import Config, cfg
from backend.core.model.model_manager import ModelManager
from backend.core.tools.tool_manager import ToolManager


router = APIRouter()


@router.get("/capabilities", response_model=Dict[str, Any])
async def get_capabilities(
    registry: CapabilityRegistry = Depends(get_capability_registry),
):
    """获取当前可用能力清单。"""
    return registry.inventory()


@router.get("/capabilities/summary", response_model=Dict[str, str])
async def get_capabilities_summary(
    registry: CapabilityRegistry = Depends(get_capability_registry),
):
    """获取可注入模型上下文的能力摘要。"""
    return {"summary": build_available_capabilities_prompt(registry)}


@router.post("/capabilities/reload", response_model=Dict[str, Any])
async def reload_capabilities(
    request: Request,
    config_manager: Config = Depends(get_config_manager),
):
    """重载项目能力，并刷新依赖能力注册表的运行时管理器。"""
    try:
        project_root = Path(getattr(request.app.state, "project_root", Path.cwd()))
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data

        registry = build_capability_registry(project_root, config_manager.data)
        runtime_config = build_runtime_config_with_plugin_mcp(
            config_manager.data,
            registry,
        )

        old_tool_manager = getattr(request.app.state, "tool_manager", None)
        if old_tool_manager is not None:
            await old_tool_manager.close()

        tool_manager = ToolManager(runtime_config)
        await tool_manager.init()

        request.app.state.capability_registry = registry
        chat_manager = getattr(request.app.state, "chat_manager", None)
        model_manager = getattr(request.app.state, "model_manager", None)
        if model_manager is None:
            model_manager = getattr(chat_manager, "model_manager", None)
        if model_manager is None:
            model_manager = ModelManager()

        _sync_runtime_managers(
            request.app,
            runtime_config,
            model_manager,
            tool_manager,
        )

        return registry.inventory()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
