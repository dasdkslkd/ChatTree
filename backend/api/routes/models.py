# backend/api/routes/models.py
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from pydantic import BaseModel
from ...core.model.model_manager import ModelManager
from ...core.config.types import ModelProvider
from ..dependencies import get_model_manager

router = APIRouter()


@router.get("/models/{provider}", response_model=List[str])
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

@router.get("/models", response_model=List[str])
async def get_providers():
    """获取所有支持的提供商"""
    return [p.value for p in ModelProvider]