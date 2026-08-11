from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.api.dependencies import (
    get_chat_manager,
    get_persistence,
    get_plan_ledger,
    get_run_manager,
    get_transcript_assembler,
)
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.types import Message
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.plans import PlanLedger
from backend.core.plans.ledger import PlanNotFoundError
from backend.core.runs import RunKind, RunManager, RunRecord, RunStatus
from backend.core.transcript import TranscriptAssembler


router = APIRouter()


class PlanAnswerRequest(BaseModel):
    answers: list[str]


class PlanRejectRequest(BaseModel):
    feedback: str = ""


PLAN_ACTION_EVENT_KEYS = (
    "event_type",
    "reasoning",
    "tool_call",
    "tool_calls",
    "tool_round",
    "tool_round_id",
    "approval",
    "child_run_id",
    "child_kind",
    "child_status",
    "child_summary",
    "payload",
    "plan_id",
    "tool_permission_mode",
    "task_context_mode",
    "metadata",
)


def _format_sse_data(payload: dict[str, Any] | str) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _tool_call_for_plan(
    persistence: SQLitePersistence,
    conversation_id: str,
    tool_call_id: str | None,
) -> dict[str, Any]:
    if not tool_call_id:
        raise KeyError("tool_call_id")
    with persistence.connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM tool_calls
            WHERE conversation_id = ? AND id = ?
            """,
            (conversation_id, tool_call_id),
        ).fetchone()
    if row is None:
        raise KeyError(tool_call_id)
    return dict(row)


def _existing_tool_result_id(
    persistence: SQLitePersistence,
    conversation_id: str,
    tool_call_id: str,
) -> str:
    with persistence.connect() as conn:
        row = conn.execute(
            """
            SELECT id
            FROM tool_results
            WHERE conversation_id = ? AND tool_call_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (conversation_id, tool_call_id),
        ).fetchone()
    return str(row["id"]) if row is not None else f"plan-result:{tool_call_id}"


def _write_plan_action_result(
    persistence: SQLitePersistence,
    conversation_id: str,
    call: dict[str, Any],
    payload: dict[str, Any],
) -> Message:
    repository = ChatRepository(persistence)
    tool_call_id = str(call["id"])
    output = json.dumps(payload, ensure_ascii=False)
    result_id = _existing_tool_result_id(persistence, conversation_id, tool_call_id)
    repository.add_tool_result(
        conversation_id,
        call.get("node_id"),
        tool_result_id=result_id,
        tool_call_id=tool_call_id,
        output=output,
        status="complete",
        run_id=call.get("run_id"),
        metadata={"source": "plan_action"},
    )
    return Message({
        "id": result_id,
        "role": "tool",
        "name": str(call.get("name") or ""),
        "content": output,
        "raw_content": output,
        "model_visible_content": output,
        "tool_call_id": tool_call_id,
        "tool_result_id": result_id,
    })


def _patch_for_tool_call(
    assembler: TranscriptAssembler,
    conversation_id: str,
    node_id: str | None,
    item_id: str,
) -> dict[str, Any]:
    item = None
    if node_id:
        snapshot = assembler.snapshot(conversation_id, node_id)
        item = next(
            (candidate for candidate in snapshot["items"] if candidate.get("id") == item_id),
            None,
        )
    operations = [{"op": "upsert", "item": item}] if item is not None else []
    return {
        "type": "transcript_patch",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "revision": assembler.next_revision(conversation_id, node_id),
        "operations": operations,
    }


def _stream_chunk_payload(chunk: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": chunk.get("status", "content"),
        "content": chunk.get("content", ""),
        "node_id": chunk.get("node_id"),
        "target_node_id": chunk.get("target_node_id") or chunk.get("node_id"),
        "anchor_node_id": chunk.get("anchor_node_id"),
        "run_id": chunk.get("run_id"),
        "assistant_message_id": chunk.get("assistant_message_id"),
        "event_index": chunk.get("event_index"),
        "conversation_id": chunk.get("conversation_id", conversation_id),
        "error": chunk.get("error"),
        "tokens_used": chunk.get("tokens_used", 0),
        "usage_info": chunk.get("usage_info"),
    }
    for key in PLAN_ACTION_EVENT_KEYS:
        value = chunk.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _set_node_permission_mode(
    chat_manager: ChatManager,
    conversation_id: str,
    node_id: str,
    mode: str,
) -> None:
    conversation = chat_manager.get_conversation(conversation_id)
    if conversation is None or node_id not in conversation.nodes:
        return
    conversation.nodes[node_id]["tool_permission_mode"] = mode
    save = getattr(chat_manager, "_save", None)
    if callable(save):
        save(conversation)


def _continuation_permission_mode(plan: Any, action: str) -> str:
    if action == "approve":
        return str(plan.previous_permission_mode or "modify_only")
    return "plan"


async def _plan_action_stream(
    *,
    conversation_id: str,
    node_id: str,
    call: dict[str, Any],
    tool_message: Message,
    first_patch: dict[str, Any],
    run: RunRecord,
    chat_manager: ChatManager,
    run_manager: RunManager,
    assembler: TranscriptAssembler,
    permission_mode: str,
) -> AsyncIterator[str]:
    yield _format_sse_data(first_patch)
    patch_session = assembler.patch_session(run.run_id, int(first_patch["revision"]))
    final_status = RunStatus.COMPLETED
    final_error: str | None = None
    try:
        async for chunk in chat_manager.send_message_stream(
            conversation_id=conversation_id,
            content="",
            parent_node_id=node_id,
            focus_new_node=False,
            tool_permission_mode=permission_mode,
            hidden_user_message=True,
            suppress_user_message=True,
            append_to_existing_node=True,
            continuation_messages=[tool_message],
            run_id=run.run_id,
        ):
            payload = _stream_chunk_payload(dict(chunk), conversation_id)
            persisted = await run_manager.append_event(run.run_id, payload)
            patch = patch_session.feed(persisted)
            if patch is not None:
                yield _format_sse_data(patch)
            if payload.get("status") == "error":
                final_status = RunStatus.FAILED
                final_error = str(payload.get("error") or "")
            elif payload.get("status") in {"stopped", "cancelled", "interrupted"}:
                final_status = RunStatus.CANCELLED
    except BaseException as exc:
        final_status = RunStatus.CANCELLED if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else RunStatus.FAILED
        final_error = None if final_status == RunStatus.CANCELLED else str(exc) or exc.__class__.__name__
        await run_manager.finish_run(run.run_id, final_status, final_error)
        raise
    terminal_from_event = int((run_manager.get_run(run.run_id) or {}).get("event_count") or 0)
    await run_manager.finish_run(run.run_id, final_status, final_error)
    for payload in run_manager.read_events(run.run_id, terminal_from_event):
        patch = patch_session.feed(payload)
        if patch is not None:
            yield _format_sse_data(patch)
    yield _format_sse_data("[DONE]")


def _create_plan_action_run(
    run_manager: RunManager,
    *,
    conversation_id: str,
    node_id: str,
    action: str,
    plan_id: str,
    source_run_id: str | None,
    source_tool_call_id: str,
):
    return run_manager.create_run(
        conversation_id=conversation_id,
        kind=RunKind.CHAT,
        anchor_node_id=node_id,
        target_node_id=node_id,
        created_by_run_id=source_run_id,
        summary=f"plan {action}",
        metadata={
            "plan_action": action,
            "plan_id": plan_id,
            "source_tool_call_id": source_tool_call_id,
        },
    )


async def _start_plan_action_response(
    *,
    conversation_id: str,
    plan_id: str,
    action: str,
    plan: Any,
    call: dict[str, Any],
    tool_message: Message,
    item_id: str,
    ledger: PlanLedger,
    chat_manager: ChatManager,
    run_manager: RunManager,
    assembler: TranscriptAssembler,
) -> StreamingResponse:
    node_id = str(call.get("node_id") or plan.blocking_node_id or "")
    if not node_id:
        raise KeyError("node_id")
    permission_mode = _continuation_permission_mode(plan, action)
    _set_node_permission_mode(chat_manager, conversation_id, node_id, permission_mode)
    run = await _create_plan_action_run(
        run_manager,
        conversation_id=conversation_id,
        node_id=node_id,
        action=action,
        plan_id=plan_id,
        source_run_id=call.get("run_id") or plan.blocking_run_id,
        source_tool_call_id=str(call["id"]),
    )
    if action == "approve":
        await ledger.update_approved_run_id(
            conversation_id=conversation_id,
            plan_id=plan_id,
            approved_run_id=run.run_id,
        )
    first_patch = _patch_for_tool_call(
        assembler,
        conversation_id,
        node_id,
        item_id,
    )
    return StreamingResponse(
        _plan_action_stream(
            conversation_id=conversation_id,
            node_id=node_id,
            call=call,
            tool_message=tool_message,
            first_patch=first_patch,
            run=run,
            chat_manager=chat_manager,
            run_manager=run_manager,
            assembler=assembler,
            permission_mode=permission_mode,
        ),
        media_type="text/event-stream",
    )


@router.get("/conversations/{conversation_id}/plans/current")
async def get_current_plan(
    conversation_id: str,
    ledger: PlanLedger = Depends(get_plan_ledger),
) -> dict[str, Any]:
    plan = await ledger.get_active_or_awaiting(conversation_id)
    return {"plan": plan.to_dict() if plan is not None else None}


@router.post("/conversations/{conversation_id}/plans/{plan_id}/answer")
async def answer_plan_question(
    conversation_id: str,
    plan_id: str,
    body: PlanAnswerRequest,
    ledger: PlanLedger = Depends(get_plan_ledger),
    persistence: SQLitePersistence = Depends(get_persistence),
    assembler: TranscriptAssembler = Depends(get_transcript_assembler),
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
):
    try:
        plan = await ledger.get_plan(conversation_id, plan_id)
        if plan is None:
            raise PlanNotFoundError(plan_id)
        call = _tool_call_for_plan(persistence, conversation_id, plan.question_tool_call_id)
        await ledger.answer_question(
            conversation_id=conversation_id,
            plan_id=plan_id,
            answers=body.answers,
        )
        tool_message = _write_plan_action_result(
            persistence,
            conversation_id,
            call,
            {
                "plan_id": plan_id,
                "status": "answered",
                "answers": body.answers,
            },
        )
        return await _start_plan_action_response(
            conversation_id=conversation_id,
            plan_id=plan_id,
            action="answer",
            plan=plan,
            call=call,
            tool_message=tool_message,
            item_id=f"plan-question:{call['id']}",
            ledger=ledger,
            chat_manager=chat_manager,
            run_manager=run_manager,
            assembler=assembler,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail="plan not found") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="plan tool call not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/plans/{plan_id}/approve")
async def approve_plan(
    conversation_id: str,
    plan_id: str,
    ledger: PlanLedger = Depends(get_plan_ledger),
    persistence: SQLitePersistence = Depends(get_persistence),
    assembler: TranscriptAssembler = Depends(get_transcript_assembler),
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
):
    try:
        plan = await ledger.get_plan(conversation_id, plan_id)
        if plan is None:
            raise PlanNotFoundError(plan_id)
        call = _tool_call_for_plan(persistence, conversation_id, plan.exit_tool_call_id)
        await ledger.approve_plan(conversation_id=conversation_id, plan_id=plan_id)
        tool_message = _write_plan_action_result(
            persistence,
            conversation_id,
            call,
            {
                "plan_id": plan_id,
                "status": "approved",
            },
        )
        return await _start_plan_action_response(
            conversation_id=conversation_id,
            plan_id=plan_id,
            action="approve",
            plan=plan,
            call=call,
            tool_message=tool_message,
            item_id=f"plan-approval:{call['id']}",
            ledger=ledger,
            chat_manager=chat_manager,
            run_manager=run_manager,
            assembler=assembler,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail="plan not found") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="plan tool call not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/conversations/{conversation_id}/plans/{plan_id}/reject")
async def reject_plan(
    conversation_id: str,
    plan_id: str,
    body: PlanRejectRequest,
    ledger: PlanLedger = Depends(get_plan_ledger),
    persistence: SQLitePersistence = Depends(get_persistence),
    assembler: TranscriptAssembler = Depends(get_transcript_assembler),
    chat_manager: ChatManager = Depends(get_chat_manager),
    run_manager: RunManager = Depends(get_run_manager),
):
    try:
        plan = await ledger.get_plan(conversation_id, plan_id)
        if plan is None:
            raise PlanNotFoundError(plan_id)
        call = _tool_call_for_plan(persistence, conversation_id, plan.exit_tool_call_id)
        await ledger.reject_plan(
            conversation_id=conversation_id,
            plan_id=plan_id,
            feedback=body.feedback,
        )
        tool_message = _write_plan_action_result(
            persistence,
            conversation_id,
            call,
            {
                "plan_id": plan_id,
                "status": "rejected",
                "feedback": body.feedback,
            },
        )
        return await _start_plan_action_response(
            conversation_id=conversation_id,
            plan_id=plan_id,
            action="reject",
            plan=plan,
            call=call,
            tool_message=tool_message,
            item_id=f"plan-approval:{call['id']}",
            ledger=ledger,
            chat_manager=chat_manager,
            run_manager=run_manager,
            assembler=assembler,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail="plan not found") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="plan tool call not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
