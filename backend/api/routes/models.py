# backend/api/routes/models.py
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from pydantic import BaseModel
from ...core.model.model_manager import ModelManager
from ...core.config.types import ModelProvider
from ..dependencies import get_model_manager

router = APIRouter()

class SetModelRequest(BaseModel):
    provider: ModelProvider

@router.get("/", response_model=Dict[str, Any])
async def get_current_model(model_manager: ModelManager = Depends(get_model_manager)):
    """获取当前模型信息"""
    try:
        current = model_manager.current_model
        return {
            "current_provider": current,
            "available_models": model_manager.model_list
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/list/{provider}", response_model=List[str])
async def list_models(
    provider: ModelProvider,
    model_manager: ModelManager = Depends(get_model_manager)
):
    """获取指定提供商的模型列表"""
    try:
        models = model_manager.list_available_models(provider)
        return models
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/current")
async def set_current_model(
    request: SetModelRequest,
    model_manager: ModelManager = Depends(get_model_manager)
):
    """设置当前模型"""
    try:
        if model_manager.set_current_model(request.provider):
            return {"message": f"已切换到 {request.provider} 模型"}
        else:
            raise HTTPException(status_code=400, detail="无效的模型提供商")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/providers", response_model=List[str])
async def get_providers():
    """获取所有支持的提供商"""
    return [p.value for p in ModelProvider]