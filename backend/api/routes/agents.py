from __future__ import annotations

from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.dependencies import get_agent_runtime, get_capability_registry, get_run_manager
from backend.api.errors import ApiError
from backend.api.run_start import (
    RunStartResponse,
    require_idempotency_key,
    run_start_api_error,
    run_start_openapi_responses,
    run_start_response,
)
from backend.core.agents import AgentRuntime, AgentSource
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.runs import (
    RunIdempotency,
    RunIdempotencyConflictError,
    RunManager,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunRequestFingerprintError,
    RunStartReservationError,
    RunStartSchedulingError,
    RunStartValidationError,
    fingerprint_run_request,
)
from backend.core.runs.public import public_run_dict

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


@router.post(
    "/conversations/{conversation_id}/agents/{agent_name}/runs",
    response_model=RunStartResponse,
    responses=run_start_openapi_responses(),
)
async def start_agent_run(
    conversation_id: str,
    agent_name: str,
    body: StartSubagentRequest,
    http_request: Request,
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    try:
        agent_runtime.run_manager.validate_run_references(
            conversation_id,
            anchor_node_id=body.parent_node_id,
            created_by_run_id=body.created_by_run_id,
            cancellation_parent_run_id=body.cancellation_parent_run_id,
        )
        idempotency = RunIdempotency(
            key=idempotency_key,
            request_fingerprint=fingerprint_run_request(
                operation="agent",
                conversation_id=conversation_id,
                anchor_node_id=body.parent_node_id,
                payload={
                    "agent_name": agent_name,
                    "request": body.model_dump(mode="json"),
                },
            ),
        )
        result = await agent_runtime.spawn_agent_idempotent(
            source=AgentSource(
                conversation_id=conversation_id,
                run_id=body.created_by_run_id or "",
                run_kind="chat",
                anchor_node_id=body.parent_node_id,
                root_run_id=body.created_by_run_id,
                task_summary=(
                    body.input[:160] if isinstance(body.input, str) else ""
                ),
            ),
            agent_name=agent_name,
            input_data=body.input,
            context_mode="fresh",
            delivery_policy="auto",
            created_by_run_id=body.created_by_run_id,
            cancellation_parent_run_id=body.cancellation_parent_run_id,
            provider_id=body.provider_id,
            model_id=body.model_id,
            permission_mode=body.permission_mode,
            workspace=body.workspace,
            idempotency=idempotency,
            request_id=http_request.state.request_id,
        )
        return run_start_response(result)
    except KeyError as exc:
        if len(exc.args) != 1 or exc.args[0] != agent_name:
            raise
        raise ApiError(
            404,
            "agent_not_found",
            "Agent 不存在",
            False,
            {"agent_name": agent_name},
        ) from exc
    except (
        RunRequestFingerprintError,
        RunReferenceNotFoundError,
        RunReferenceConversationMismatchError,
        RunIdempotencyConflictError,
        RunStartReservationError,
        RunStartSchedulingError,
        RunStartValidationError,
    ) as exc:
        raise run_start_api_error(exc) from exc


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
    return public_run_dict(run)


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
