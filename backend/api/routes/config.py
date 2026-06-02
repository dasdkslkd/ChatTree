# backend/api/routes/config.py
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Dict, Any, Optional
from pydantic import BaseModel
from ...core.config.config import Config, cfg
from ...core.model.model_manager import ModelManager
from ..dependencies import get_config_manager

router = APIRouter()

class ConfigUpdateRequest(BaseModel):
    default_provider: Optional[str] = None
    provider_configs: Optional[Dict[str, Dict[str, Any]]] = None

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

        # 同步全局配置与运行中的模型管理器，避免修改配置后必须重启后端。
        config_manager.data = config_manager._load_config()
        cfg.data = config_manager.data

        model_manager = ModelManager()
        http_request.app.state.model_manager = model_manager
        if hasattr(http_request.app.state, 'chat_manager'):
            http_request.app.state.chat_manager.model_manager = model_manager

        return {"message": "配置已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))