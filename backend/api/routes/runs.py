from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backend.api.dependencies import get_chat_manager, get_run_manager, get_subagent_executor
from backend.core.agents import SubagentExecutor
from backend.core.chat.chat_manager import ChatManager
from backend.core.runs import RunKind, RunManager

router = APIRouter()


def _format_sse_data(payload: Dict[str, Any] | str) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    import json
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _subscribe_sse(run_manager: RunManager, run_id: str, from_event: int = 0):
    async for payload in run_manager.subscribe(run_id, from_event):
        yield _format_sse_data(payload)
    yield _format_sse_data("[DONE]")


@router.get("/runs/active", response_model=List[Dict[str, Any]])
async def list_active_runs(
    conversation_id: Optional[str] = None,
    run_manager: RunManager = Depends(get_run_manager),
):
    return run_manager.list_active(conversation_id)


@router.get("/conversations/{conversation_id}/runs", response_model=List[Dict[str, Any]])
async def list_conversation_runs(
    conversation_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    return run_manager.list_runs(conversation_id)


@router.get("/runs/{run_id}", response_model=Dict[str, Any])
async def get_run(
    run_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    return run


@router.get("/runs/{run_id}/attach")
async def attach_run(
    run_id: str,
    from_event: int = 0,
    run_manager: RunManager = Depends(get_run_manager),
):
    if not run_manager.get_run(run_id):
        raise HTTPException(status_code=404, detail="运行不存在")
    return StreamingResponse(
        _subscribe_sse(run_manager, run_id, from_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: str,
    run_manager: RunManager = Depends(get_run_manager),
    chat_manager: ChatManager = Depends(get_chat_manager),
    subagent_executor: SubagentExecutor = Depends(get_subagent_executor),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    await run_manager.request_stop(run_id)
    if run.get("kind") == RunKind.CHAT.value and run.get("target_node_id"):
        await chat_manager.stop_stream(str(run["target_node_id"]))
    elif run.get("kind") == RunKind.SUBAGENT.value:
        await subagent_executor.stop(run_id)
    return {"detail": "运行已请求停止"}


@router.post("/conversations/{conversation_id}/runs/stop")
async def stop_conversation_runs(
    conversation_id: str,
    run_manager: RunManager = Depends(get_run_manager),
    chat_manager: ChatManager = Depends(get_chat_manager),
    subagent_executor: SubagentExecutor = Depends(get_subagent_executor),
):
    stopped: list[str] = []
    for run in run_manager.list_active(conversation_id):
        run_id = str(run["run_id"])
        await run_manager.request_stop(run_id)
        if run.get("kind") == RunKind.CHAT.value and run.get("target_node_id"):
            await chat_manager.stop_stream(str(run["target_node_id"]))
        elif run.get("kind") == RunKind.SUBAGENT.value:
            await subagent_executor.stop(run_id)
        stopped.append(run_id)
    return {"detail": "会话运行已请求停止", "run_ids": stopped}


@router.get("/runs/{run_id}/events", response_model=List[Dict[str, Any]])
async def get_run_events(
    run_id: str,
    from_event: int = 0,
    run_manager: RunManager = Depends(get_run_manager),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    events = run_manager.journal.read_from_index(
        str(run["conversation_id"]),
        run_id,
        from_event,
    )
    return [event["payload"] for event in events]
