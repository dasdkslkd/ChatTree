# backend/api/routes/config.py
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, Optional
from pydantic import BaseModel
from ...core.config.config import Config
from ..dependencies import get_config_manager

router = APIRouter()

class ConfigUpdateRequest(BaseModel):
    system_prompt: Optional[str] = None
    save_history: Optional[bool] = None
    max_history_messages: Optional[int] = None
    default_provider: Optional[str] = None

@router.get("/", response_model=Dict[str, Any])
async def get_config(config_manager: Config = Depends(get_config_manager)):
    """获取配置"""
    try:
        return config_manager.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/", response_model=Dict[str, str])
async def update_config(
    request: ConfigUpdateRequest,
    config_manager: Config = Depends(get_config_manager)
):
    """更新配置"""
    try:
        if request.system_prompt is not None:
            config_manager.data['system_prompt'] = request.system_prompt
        
        if request.save_history is not None:
            if 'chat_config' not in config_manager.data:
                config_manager.data['chat_config'] = {}
            config_manager.data['chat_config']['save_history'] = request.save_history
        
        if request.max_history_messages is not None:
            if 'chat_config' not in config_manager.data:
                config_manager.data['chat_config'] = {}
            config_manager.data['chat_config']['max_history_messages'] = request.max_history_messages
        
        if request.default_provider is not None:
            config_manager.data['default_provider'] = request.default_provider
        
        config_manager.save()
        return {"message": "配置已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))