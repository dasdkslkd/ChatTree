# backend/api/routes/config.py
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Dict, Any, Optional, List
from pydantic import BaseModel
from ...core.config.config import Config, cfg
from ...core.model.model_manager import ModelManager
from ..dependencies import get_config_manager

router = APIRouter()


class ConfigUpdateRequest(BaseModel):
    default_provider: Optional[str] = None
    provider_configs: Optional[Dict[str, Dict[str, Any]]] = None


class AddProviderRequest(BaseModel):
    id: str
    name: str
    api_format: str = "chat_completions"
    base_url: str = ""
    api_key: str = ""


@router.get("/config", response_model=Dict[str, Any])
async def get_config(config_manager: Config = Depends(get_config_manager)):
    """获取配置"""
    try:
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data
        return config_manager.data
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
        if request.provider_configs:
            for provider, conf in request.provider_configs.items():
                config_manager.data['provider'][provider] = conf
        if request.default_provider is not None:
            config_manager.data['default_provider'] = request.default_provider
        config_manager.save()

        # 同步全局配置与运行中的模型管理器
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data

        model_manager = ModelManager()
        http_request.app.state.model_manager = model_manager
        if hasattr(http_request.app.state, 'chat_manager'):
            http_request.app.state.chat_manager.model_manager = model_manager

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

        config_manager.add_provider(provider_id, {
            'name': request.name,
            'api_format': request.api_format,
            'base_url': request.base_url,
            'api_key': request.api_key,
        })

        # 同步
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data

        model_manager = ModelManager()
        http_request.app.state.model_manager = model_manager
        if hasattr(http_request.app.state, 'chat_manager'):
            http_request.app.state.chat_manager.model_manager = model_manager

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
        http_request.app.state.model_manager = model_manager
        if hasattr(http_request.app.state, 'chat_manager'):
            http_request.app.state.chat_manager.model_manager = model_manager

        return {"message": f"提供商 {provider_id} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
