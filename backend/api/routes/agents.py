from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.dependencies import get_agent_mailbox, get_agent_runtime, get_capability_registry, get_run_manager
from backend.core.agents import AgentMailbox, AgentRuntime, AgentSource
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.runs import RunManager

router = APIRouter()


class StartSubagentRequest(BaseModel):
    input: Any
    parent_node_id: Optional[str] = None
    created_by_run_id: Optional[str] = None
    cancellation_parent_run_id: Optional[str] = None
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
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    try:
        task = request.input if isinstance(request.input, str) else str(request.input)
        return await agent_runtime.spawn_agent(
            source=AgentSource(
                conversation_id=conversation_id,
                run_id=request.created_by_run_id or request.parent_node_id or "",
                run_kind="chat",
                anchor_node_id=request.parent_node_id,
                root_run_id=request.created_by_run_id,
                task_summary=task[:160],
            ),
            agent_name=agent_name,
            task=task,
            context_mode="fresh",
            delivery_policy="auto",
            created_by_run_id=request.created_by_run_id,
            cancellation_parent_run_id=request.cancellation_parent_run_id,
            provider_id=request.provider_id,
            model_id=request.model_id,
            permission_mode=request.permission_mode,
            workspace=request.workspace,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/conversations/{conversation_id}/agents/runs", response_model=Dict[str, Any])
async def list_agent_runs(
    conversation_id: str,
    include_completed: bool = True,
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    return await agent_runtime.list_agents(
        conversation_id=conversation_id,
        include_completed=include_completed,
    )


@router.get("/agents/runs/{run_id}", response_model=Dict[str, Any])
async def get_agent_run(
    run_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run 不存在")
    return run


@router.post("/agents/runs/{run_id}/interrupt", response_model=Dict[str, Any])
async def interrupt_agent_run(
    run_id: str,
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    return await agent_runtime.interrupt_agent(run_id=run_id)


@router.post("/agents/runs/{run_id}/close", response_model=Dict[str, Any])
async def close_agent_run(
    run_id: str,
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    return await agent_runtime.close_agent(run_id=run_id)


@router.get("/conversations/{conversation_id}/agents/mailbox/pending", response_model=Dict[str, Any])
async def list_pending_agent_mailbox_messages(
    conversation_id: str,
    mailbox: AgentMailbox = Depends(get_agent_mailbox),
):
    return {
        "messages": await mailbox.list_pending_notifications(conversation_id),
    }
