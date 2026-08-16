from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.api.dependencies import (
    get_chat_manager,
    get_command_executor,
    get_run_manager,
    get_subagent_executor,
    get_transcript_assembler,
    get_workflow_manager,
)
from backend.core.agents import SubagentExecutor
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.config import cfg
from backend.core.perf import get_profiler
from backend.core.runs import RunManager
from backend.core.runs.types import FINISHED_RUN_STATUSES
from backend.core.runs.public import public_run_dict
from backend.core.transcript import TranscriptAssembler
from backend.core.workflows import WorkflowManager
from .run_control import stop_run_tree

router = APIRouter()


def _format_sse_data(payload: Dict[str, Any] | str) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    import json
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _public_run_payload(run: Dict[str, Any], command_executor: Any = None) -> Dict[str, Any]:
    item = public_run_dict(run)
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    if (
        command_executor is not None
        and run.get("kind") == "command"
        and metadata.get("shell_auto_backgrounded") is True
        and "result_observed_at" not in metadata
    ):
        snapshot = command_executor.snapshot(str(item.get("run_id") or ""))
        if snapshot is not None:
            item["command"] = {
                "command": snapshot.get("command"),
                "cwd": snapshot.get("cwd"),
                "stdout": snapshot.get("stdout_tail") or "",
                "stderr": snapshot.get("stderr_tail") or "",
                "exit_code": snapshot.get("exit_code"),
                "duration_seconds": snapshot.get("duration_seconds"),
                "status": snapshot.get("command_status") or snapshot.get("status"),
                "error": snapshot.get("error"),
            }
    return item


async def _subscribe_sse(
    run_manager: RunManager,
    transcript_assembler: TranscriptAssembler,
    run_id: str,
    from_event: int = 0,
):
    profiler = get_profiler()
    emitted = 0
    first_event = True
    patch_session = transcript_assembler.patch_session(run_id)
    run = run_manager.get_run(run_id)
    finished_status_values = {status.value for status in FINISHED_RUN_STATUSES}
    finished = bool(run and run.get("status") in finished_status_values)
    if finished:
        patch = patch_session.feed({
            "type": "run_finished",
            "run_id": run_id,
            "conversation_id": run.get("conversation_id"),
            "target_node_id": run.get("target_node_id") or run.get("anchor_node_id"),
            "status": run.get("status"),
            "error": (run.get("metadata") or {}).get("error"),
        })
        if patch is not None:
            emitted += 1
            yield _format_sse_data(patch)
        profiler.mark("sse.done", run_id=run_id, route="runs", emitted_events=emitted)
        yield _format_sse_data("[DONE]")
        return
    with profiler.span("sse.replay", run_id=run_id, from_event=from_event, route="runs"):
        for payload in run_manager.read_events(run_id, 0):
            if int(payload.get("event_index") or 0) >= max(0, int(from_event or 0)):
                break
            patch_session.feed(payload, emit=False)
    heartbeat_seconds = float(cfg.data["model_transport"]["sse_heartbeat_seconds"])
    with profiler.span("sse.subscribe", run_id=run_id, from_event=from_event, route="runs"):
        subscription = run_manager.subscribe(run_id, from_event)
        pending: asyncio.Task | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(subscription))
                done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                try:
                    payload = pending.result()
                except StopAsyncIteration:
                    break
                finally:
                    pending = None
                if first_event:
                    profiler.mark("sse.first_event", run_id=run_id, route="runs")
                    first_event = False
                patch = patch_session.feed(payload)
                if patch is None:
                    continue
                emitted += 1
                yield _format_sse_data(patch)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            await subscription.aclose()
    if first_event:
        run = run_manager.get_run(run_id)
        if run and run.get("status") in finished_status_values:
            for payload in run_manager.read_events(run_id, from_event):
                patch = patch_session.feed(payload)
                if patch is None:
                    continue
                emitted += 1
                yield _format_sse_data(patch)
    profiler.mark("sse.done", run_id=run_id, route="runs", emitted_events=emitted)
    yield _format_sse_data("[DONE]")


@router.get("/runs/active", response_model=List[Dict[str, Any]])
async def list_active_runs(
    conversation_id: Optional[str] = None,
    run_manager: RunManager = Depends(get_run_manager),
):
    runs = []
    for run in run_manager.list_active(conversation_id):
        item = public_run_dict(run)
        run_id = item.get("run_id")
        item["node_id"] = item.get("target_node_id")
        item["done"] = False
        if run_id:
            item["stream_url"] = f"/api/v1/runs/{run_id}/events"
        runs.append(item)
    return runs


@router.get("/conversations/{conversation_id}/runs", response_model=List[Dict[str, Any]])
async def list_conversation_runs(
    conversation_id: str,
    run_manager: RunManager = Depends(get_run_manager),
    command_executor: Any = Depends(get_command_executor),
):
    return [_public_run_payload(run, command_executor) for run in run_manager.list_runs(conversation_id)]


@router.get("/runs/{run_id}", response_model=Dict[str, Any])
async def get_run(
    run_id: str,
    run_manager: RunManager = Depends(get_run_manager),
    command_executor: Any = Depends(get_command_executor),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    return _public_run_payload(run, command_executor)


@router.post("/runs/{run_id}/observe", response_model=Dict[str, Any])
async def observe_run(
    run_id: str,
    run_manager: RunManager = Depends(get_run_manager),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    if run.get("kind") != "command":
        raise HTTPException(status_code=400, detail="只有 command 运行可以被消费")
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    if metadata.get("shell_auto_backgrounded") is not True:
        raise HTTPException(status_code=400, detail="只有自动转后台的 command 可以被消费")
    if run.get("status") not in {status.value for status in FINISHED_RUN_STATUSES}:
        raise HTTPException(status_code=409, detail="运行尚未结束")
    observed = await run_manager.mark_observed(run_id, via="side_panel")
    return public_run_dict(observed.to_dict())


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    request: Request,
    run_id: str,
    from_event: int = 0,
    run_manager: RunManager = Depends(get_run_manager),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    transcript_assembler = get_transcript_assembler(request)
    return StreamingResponse(
        _subscribe_sse(run_manager, transcript_assembler, run_id, from_event),
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
    command_executor: Any = Depends(get_command_executor),
    workflow_manager: WorkflowManager = Depends(get_workflow_manager),
):
    run = run_manager.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行不存在")
    await stop_run_tree(
        run_id,
        run_manager=run_manager,
        chat_manager=chat_manager,
        subagent_executor=subagent_executor,
        command_executor=command_executor,
        workflow_manager=workflow_manager,
    )
    return {"detail": "运行已请求停止"}


@router.post("/conversations/{conversation_id}/runs/stop")
async def stop_conversation_runs(
    conversation_id: str,
    run_manager: RunManager = Depends(get_run_manager),
    chat_manager: ChatManager = Depends(get_chat_manager),
    subagent_executor: SubagentExecutor = Depends(get_subagent_executor),
    command_executor: Any = Depends(get_command_executor),
    workflow_manager: WorkflowManager = Depends(get_workflow_manager),
):
    stopped: list[str] = []
    seen: set[str] = set()
    for run in run_manager.list_active(conversation_id):
        run_id = str(run["run_id"])
        stopped.extend(await stop_run_tree(
            run_id,
            run_manager=run_manager,
            chat_manager=chat_manager,
            subagent_executor=subagent_executor,
            command_executor=command_executor,
            workflow_manager=workflow_manager,
            _seen=seen,
        ))
    return {"detail": "会话运行已请求停止", "run_ids": stopped}
