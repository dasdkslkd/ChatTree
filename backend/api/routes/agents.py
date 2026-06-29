from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import get_capability_registry, get_subagent_executor
from backend.core.agents import SubagentExecutor
from backend.core.capabilities.registry import CapabilityRegistry

router = APIRouter()


class StartSubagentRequest(BaseModel):
    input: Any
    parent_node_id: Optional[str] = None
    parent_run_id: Optional[str] = None
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    permission_mode: Optional[str] = None
    workspace: Optional[Dict[str, Any]] = None


@router.get("/agents/{agent_name}", response_model=Dict[str, Any])
async def get_agent(
    agent_name: str,
    registry: CapabilityRegistry = Depends(get_capability_registry),
):
    agent = registry.get_agent(agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return {
        "name": agent.name,
        "description": agent.description,
        "tools": agent.tools,
        "skills": agent.skills,
        "model": agent.model,
        "model_id": agent.model_id,
        "provider_id": agent.provider_id,
        "permission_mode": agent.permission_mode,
        "max_turns": agent.max_turns,
        "max_tool_rounds": agent.max_tool_rounds,
        "timeout_seconds": agent.timeout_seconds,
        "output_mode": agent.output_mode,
        "input_schema": agent.input_schema,
        "output_schema": agent.output_schema,
        "metadata": agent.metadata,
    }


@router.post("/conversations/{conversation_id}/agents/{agent_name}/runs", response_model=Dict[str, Any])
async def start_agent_run(
    conversation_id: str,
    agent_name: str,
    request: StartSubagentRequest,
    executor: SubagentExecutor = Depends(get_subagent_executor),
):
    try:
        return await executor.start(
            conversation_id=conversation_id,
            agent_name=agent_name,
            input_data=request.input,
            parent_node_id=request.parent_node_id,
            parent_run_id=request.parent_run_id,
            provider_id=request.provider_id,
            model_id=request.model_id,
            permission_mode=request.permission_mode,
            workspace=request.workspace,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
