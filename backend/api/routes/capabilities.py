from typing import Any, Dict

from fastapi import APIRouter, Depends

from backend.api.dependencies import get_capability_registry
from backend.core.capabilities.prompting import build_available_capabilities_prompt
from backend.core.capabilities.registry import CapabilityRegistry


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
