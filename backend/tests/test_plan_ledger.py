from __future__ import annotations

import asyncio

import pytest

from backend.core.plans import PlanLedger, PlanStatus


def run(coro):
    return asyncio.run(coro)


async def _enter_exit_approve_plan_session_case():
    ledger = PlanLedger()

    active = await ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-1",
        previous_permission_mode="modify_only",
        run_id="run-1",
    )
    awaiting = await ledger.submit_plan(
        conversation_id="conv-1",
        plan="1. Read code\n2. Implement backend core\n3. Run focused tests",
        node_id="node-2",
        run_id="run-2",
    )
    approved = await ledger.approve_plan(
        conversation_id="conv-1",
        plan_id=awaiting.plan_id,
    )
    injections = await ledger.consume_pending_context("conv-1")

    assert active.status == PlanStatus.ACTIVE
    assert active.previous_permission_mode == "modify_only"
    assert awaiting.status == PlanStatus.AWAITING_APPROVAL
    assert approved.status == PlanStatus.APPROVED
    assert approved.approved_at is not None
    assert approved.previous_permission_mode == "modify_only"
    assert len(injections) == 1
    assert injections[0].kind == "approved_plan"
    assert injections[0].permission_mode == "modify_only"
    assert "Approved plan" in injections[0].content
    assert "Implement backend core" in injections[0].content
    assert await ledger.get_active_or_awaiting("conv-1") is None


def test_enter_exit_approve_plan_session():
    run(_enter_exit_approve_plan_session_case())


async def _reject_plan_keeps_session_active_and_records_feedback_case():
    ledger = PlanLedger()
    await ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-1",
        previous_permission_mode="ask_always",
    )
    awaiting = await ledger.submit_plan(
        conversation_id="conv-1",
        plan="Initial plan",
    )

    rejected = await ledger.reject_plan(
        conversation_id="conv-1",
        plan_id=awaiting.plan_id,
        feedback="Need a smaller backend-only slice.",
    )
    current = await ledger.get_active_or_awaiting("conv-1")
    injections = await ledger.consume_pending_context("conv-1")

    assert rejected.status == PlanStatus.ACTIVE
    assert current is not None
    assert current.plan_id == awaiting.plan_id
    assert current.status == PlanStatus.ACTIVE
    assert current.feedback[-1]["feedback"] == "Need a smaller backend-only slice."
    assert len(injections) == 1
    assert injections[0].kind == "plan_feedback"
    assert injections[0].permission_mode == "plan"
    assert "Need a smaller backend-only slice." in injections[0].content


def test_reject_plan_keeps_session_active_and_records_feedback():
    run(_reject_plan_keeps_session_active_and_records_feedback_case())


async def _ask_question_sets_awaiting_question_and_roundtrips_case():
    ledger = PlanLedger()
    active = await ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-1",
        previous_permission_mode="ask_always",
    )

    question = await ledger.ask_user_question(
        conversation_id="conv-1",
        question="应该隐藏归档项目吗？",
        options=[
            {"label": "隐藏", "description": "主页面不展示归档项目"},
            {"label": "保留", "description": "仍然展示所有项目"},
        ],
        node_id="node-2",
        run_id="run-2",
    )
    snapshot = await ledger.snapshot("conv-1")
    restored = PlanLedger()
    await restored.load_snapshot("conv-1", snapshot)
    current = await restored.get_active_or_awaiting("conv-1")

    assert question.plan_id == active.plan_id
    assert question.status == PlanStatus.AWAITING_QUESTION
    assert question.question is not None
    assert question.question["question"] == "应该隐藏归档项目吗？"
    assert current is not None
    assert current.status == PlanStatus.AWAITING_QUESTION
    assert current.question["options"][0]["label"] == "隐藏"


def test_ask_question_sets_awaiting_question_and_roundtrips():
    run(_ask_question_sets_awaiting_question_and_roundtrips_case())


async def _submit_requires_active_plan_session_case():
    ledger = PlanLedger()

    with pytest.raises(ValueError, match="active plan"):
        await ledger.submit_plan(conversation_id="conv-1", plan="No active plan")


def test_submit_requires_active_plan_session():
    run(_submit_requires_active_plan_session_case())


async def _snapshot_roundtrips_plan_sessions_case():
    ledger = PlanLedger()
    await ledger.enter_plan_mode(
        conversation_id="conv-1",
        node_id="node-1",
        previous_permission_mode="auto_approve",
    )
    awaiting = await ledger.submit_plan(conversation_id="conv-1", plan="Persist this")

    snapshot = await ledger.snapshot("conv-1")
    restored = PlanLedger()
    await restored.load_snapshot("conv-1", snapshot)
    current = await restored.get_active_or_awaiting("conv-1")

    assert current is not None
    assert current.plan_id == awaiting.plan_id
    assert current.status == PlanStatus.AWAITING_APPROVAL
    assert current.plan == "Persist this"
    assert current.previous_permission_mode == "auto_approve"


def test_snapshot_roundtrips_plan_sessions():
    run(_snapshot_roundtrips_plan_sessions_case())
