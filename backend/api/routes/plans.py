from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.api.dependencies import get_chat_manager, get_plan_ledger, get_run_manager
from backend.api.routes.messages import _subscribe_sse, build_stream_chunk_data
from backend.core.chat.chat_manager import ChatManager
from backend.core.plans import PlanLedger, PlanNotFoundError, PlanStatus
from backend.core.runs import RunKind, RunManager, RunStatus

router = APIRouter()


class ApprovePlanRequest(BaseModel):
    message: Optional[str] = None


class RejectPlanRequest(BaseModel):
    feedback: Optional[str] = None


class AnswerPlanQuestionRequest(BaseModel):
    answer: str


class PlanActionStreamRequest(BaseModel):
    node_id: Optional[str] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    tool_permission_mode: Optional[str] = None


class PlanAnswerStreamRequest(PlanActionStreamRequest):
    answer: str


class PlanRejectStreamRequest(PlanActionStreamRequest):
    feedback: Optional[str] = None


def _plan_payload(plan) -> Optional[Dict[str, Any]]:
    if not plan:
        return None
    payload = plan.to_dict()
    if payload.get("status") != PlanStatus.AWAITING_APPROVAL.value:
        payload["plan"] = ""
    return payload


async def _persist_plan_snapshot_if_available(request: Request, conversation_id: str) -> None:
    chat_manager = getattr(getattr(request.app, "state", None), "chat_manager", None)
    persist = getattr(chat_manager, "persist_plan_snapshot", None)
    if callable(persist):
        await persist(conversation_id)


async def _restore_plan_snapshot_if_available(request: Request, conversation_id: str) -> None:
    chat_manager = getattr(getattr(request.app, "state", None), "chat_manager", None)
    restore = getattr(chat_manager, "restore_plan_snapshot", None)
    if callable(restore):
        await restore(conversation_id)


async def _require_plan_stream_tool_call_id(
    plan_ledger: PlanLedger,
    *,
    conversation_id: str,
    plan_id: str,
    expected_status: PlanStatus,
    tool_call_attr: str,
    missing_detail: str,
) -> str:
    current = await plan_ledger.get_active_or_awaiting(conversation_id)
    if current is None or current.plan_id != plan_id:
        current = await plan_ledger.get_plan(conversation_id, plan_id)
    if current is None:
        raise HTTPException(status_code=404, detail="plan not found")
    if current.status != expected_status:
        if expected_status == PlanStatus.AWAITING_APPROVAL:
            raise HTTPException(status_code=400, detail="plan must be awaiting approval")
        raise HTTPException(status_code=400, detail="plan must be awaiting question")
    tool_call_id = str(getattr(current, tool_call_attr, "") or "")
    if not tool_call_id:
        raise HTTPException(status_code=409, detail=missing_detail)
    return tool_call_id


@router.get("/conversations/{conversation_id}/plans/current", response_model=Dict[str, Any])
async def get_current_plan(
    request_context: Request,
    conversation_id: str,
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    plan = await plan_ledger.get_active_or_awaiting(conversation_id)
    if plan is None:
        plan = await plan_ledger.get_latest(conversation_id)
    return {"plan": _plan_payload(plan)}


@router.get("/conversations/{conversation_id}/plans/active", response_model=Optional[Dict[str, Any]])
async def get_active_plan(
    request_context: Request,
    conversation_id: str,
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    plan = await plan_ledger.get_active_or_awaiting(conversation_id)
    return _plan_payload(plan)


@router.post("/conversations/{conversation_id}/plans/context/consume", response_model=Dict[str, Any])
async def consume_plan_context(
    request_context: Request,
    conversation_id: str,
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    context = await plan_ledger.consume_pending_context(conversation_id)
    return {"context": [item.to_dict() for item in context]}


@router.post("/conversations/{conversation_id}/plans/{plan_id}/approve", response_model=Dict[str, Any])
async def approve_plan(
    request_context: Request,
    conversation_id: str,
    plan_id: str,
    request: Optional[ApprovePlanRequest] = Body(None),
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    try:
        plan = await plan_ledger.approve_plan(conversation_id=conversation_id, plan_id=plan_id)
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail="plan not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _persist_plan_snapshot_if_available(request_context, conversation_id)
    return {**plan.to_dict(), "approval_message": (request.message if request else None), "next_permission_mode": plan.previous_permission_mode}


@router.post("/conversations/{conversation_id}/plans/{plan_id}/reject", response_model=Dict[str, Any])
async def reject_plan(
    request_context: Request,
    conversation_id: str,
    plan_id: str,
    request: Optional[RejectPlanRequest] = Body(None),
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    try:
        plan = await plan_ledger.reject_plan(
            conversation_id=conversation_id,
            plan_id=plan_id,
            feedback=(request.feedback if request else "") or "",
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail="plan not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _persist_plan_snapshot_if_available(request_context, conversation_id)
    return {**plan.to_dict(), "next_permission_mode": "plan"}


@router.post("/conversations/{conversation_id}/plans/{plan_id}/answer", response_model=Dict[str, Any])
async def answer_plan_question(
    request_context: Request,
    conversation_id: str,
    plan_id: str,
    request: AnswerPlanQuestionRequest,
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    try:
        plan = await plan_ledger.answer_question(
            conversation_id=conversation_id,
            plan_id=plan_id,
            answer=request.answer,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail="plan not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _persist_plan_snapshot_if_available(request_context, conversation_id)
    return {**plan.to_dict(), "next_permission_mode": "plan"}


async def _plan_action_stream(
    *,
    conversation_id: str,
    plan_id: str,
    request: PlanActionStreamRequest,
    content: str,
    message_subtype: str,
    chat_manager: ChatManager,
    run_manager: RunManager,
    plan_ledger: PlanLedger,
    tool_call_id: str,
) -> AsyncIterator[str]:
    is_approval = message_subtype == "plan_approval_response"
    is_rejection = message_subtype == "plan_rejection_response"
    run = await run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.CHAT,
        anchor_node_id=request.node_id,
        summary="批准计划" if is_approval else ("驳回计划" if is_rejection else "回答计划澄清"),
        metadata={
            "origin": "plan_approval" if is_approval else ("plan_rejection" if is_rejection else "plan_question_answer"),
            "plan_id": plan_id,
            "model_id": request.model_id,
            "provider_id": request.provider_id,
            "reasoning_effort": request.reasoning_effort,
            "thinking_enabled": request.thinking_enabled,
            "tool_permission_mode": request.tool_permission_mode,
        },
    )

    async def produce() -> None:
        final_status = RunStatus.COMPLETED
        final_error: str | None = None
        bound_node_id: str | None = None
        try:
            if is_approval:
                plan = await plan_ledger.approve_plan(conversation_id=conversation_id, plan_id=plan_id)
                tool_result_content = plan_ledger.approved_tool_result_content(plan)
                tool_name = "exit_plan_mode"
                continuation_permission_mode = plan.previous_permission_mode
            elif is_rejection:
                plan = await plan_ledger.reject_plan(
                    conversation_id=conversation_id,
                    plan_id=plan_id,
                    feedback=content,
                )
                tool_result_content = plan_ledger.rejected_tool_result_content(plan)
                tool_name = "exit_plan_mode"
                continuation_permission_mode = "plan"
            else:
                plan = await plan_ledger.answer_question(
                    conversation_id=conversation_id,
                    plan_id=plan_id,
                    answer=content,
                )
                tool_result_content = plan_ledger.question_answer_tool_result_content(plan)
                tool_name = "ask_user_question"
                continuation_permission_mode = "plan"
            await plan_ledger.consume_pending_context(conversation_id)
            async for chunk in chat_manager.continue_plan_tool_result_stream(
                conversation_id=conversation_id,
                plan_id=plan_id,
                tool_result_content=tool_result_content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                model_id=request.model_id,
                provider_id=request.provider_id,
                node_id=request.node_id,
                reasoning_effort=request.reasoning_effort,
                thinking_enabled=request.thinking_enabled,
                tool_permission_mode=continuation_permission_mode,
                run_id=run.run_id,
            ):
                chunk_data = build_stream_chunk_data(chunk, conversation_id)
                node_id = chunk_data.get("node_id")
                if node_id and node_id != bound_node_id:
                    bound_node_id = node_id
                    await run_manager.bind_target_node(run.run_id, node_id)
                    chunk_data["target_node_id"] = node_id
                    if await run_manager.is_stop_requested(run.run_id):
                        await chat_manager.stop_stream(node_id)
                await run_manager.append_event(run.run_id, chunk_data)
                if chunk_data.get("status") == "error":
                    final_status = RunStatus.FAILED
                    final_error = chunk_data.get("error")
                elif chunk_data.get("status") == "stopped":
                    final_status = RunStatus.CANCELLED
        except Exception as exc:
            final_status = RunStatus.FAILED
            final_error = str(exc)
            await run_manager.append_event(run.run_id, {
                "status": "error",
                "content": "",
                "node_id": None,
                "conversation_id": conversation_id,
                "run_id": run.run_id,
                "error": final_error,
                "tokens_used": 0,
            })
        finally:
            await run_manager.finish_run(run.run_id, final_status, final_error)

    import asyncio
    asyncio.create_task(produce())
    async for event in _subscribe_sse(run_manager, run.run_id, 0):
        yield event


@router.post("/conversations/{conversation_id}/plans/{plan_id}/approve/stream")
async def approve_plan_stream(
    request_context: Request,
    conversation_id: str,
    plan_id: str,
    request: Optional[PlanActionStreamRequest] = Body(None),
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    stream_request = request or PlanActionStreamRequest()
    tool_call_id = await _require_plan_stream_tool_call_id(
        plan_ledger,
        conversation_id=conversation_id,
        plan_id=plan_id,
        expected_status=PlanStatus.AWAITING_APPROVAL,
        tool_call_attr="exit_tool_call_id",
        missing_detail="plan has no exit_plan_mode tool_call_id",
    )
    return StreamingResponse(
        _plan_action_stream(
            conversation_id=conversation_id,
            plan_id=plan_id,
            request=stream_request,
            content="Plan approved.",
            message_subtype="plan_approval_response",
            chat_manager=chat_manager,
            run_manager=run_manager,
            plan_ledger=plan_ledger,
            tool_call_id=tool_call_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{conversation_id}/plans/{plan_id}/reject/stream")
async def reject_plan_stream(
    request_context: Request,
    conversation_id: str,
    plan_id: str,
    request: Optional[PlanRejectStreamRequest] = Body(None),
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    stream_request = request or PlanRejectStreamRequest()
    tool_call_id = await _require_plan_stream_tool_call_id(
        plan_ledger,
        conversation_id=conversation_id,
        plan_id=plan_id,
        expected_status=PlanStatus.AWAITING_APPROVAL,
        tool_call_attr="exit_tool_call_id",
        missing_detail="plan has no exit_plan_mode tool_call_id",
    )
    feedback = stream_request.feedback or ""
    return StreamingResponse(
        _plan_action_stream(
            conversation_id=conversation_id,
            plan_id=plan_id,
            request=stream_request,
            content=feedback,
            message_subtype="plan_rejection_response",
            chat_manager=chat_manager,
            run_manager=run_manager,
            plan_ledger=plan_ledger,
            tool_call_id=tool_call_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{conversation_id}/plans/{plan_id}/answer/stream")
async def answer_plan_question_stream(
    request_context: Request,
    conversation_id: str,
    plan_id: str,
    request: PlanAnswerStreamRequest,
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
    plan_ledger: PlanLedger = Depends(get_plan_ledger),
):
    await _restore_plan_snapshot_if_available(request_context, conversation_id)
    tool_call_id = await _require_plan_stream_tool_call_id(
        plan_ledger,
        conversation_id=conversation_id,
        plan_id=plan_id,
        expected_status=PlanStatus.AWAITING_QUESTION,
        tool_call_attr="question_tool_call_id",
        missing_detail="plan has no ask_user_question tool_call_id",
    )
    return StreamingResponse(
        _plan_action_stream(
            conversation_id=conversation_id,
            plan_id=plan_id,
            request=request,
            content=request.answer,
            message_subtype="plan_question_response",
            chat_manager=chat_manager,
            run_manager=run_manager,
            plan_ledger=plan_ledger,
            tool_call_id=tool_call_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
