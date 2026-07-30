# backend/api/routes/models.py
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from ...core.model.model_manager import ModelManager
from ...core.model.model_metadata import ModelRouteError
from ...core.config.config import cfg
from ..dependencies import get_model_manager

router = APIRouter()


@router.get("/models/{provider}/metadata", response_model=Dict[str, Any])
async def get_models_metadata(
    provider: str,
    model_manager: ModelManager = Depends(get_model_manager)
):
    """获取指定提供商下所有模型的元数据（上下文长度/视觉/推理强度/思考开关）。"""
    try:
        return model_manager.get_provider_metadata(provider)
    except ModelRouteError as e:
        raise HTTPException(status_code=422, detail={"code": "route_invalid", "message": str(e)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models/{provider}", response_model=List[str])
async def list_models(
    provider: str,
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
    """获取所有已配置的提供商ID列表"""
    return list(cfg.get_all_providers().keys())
