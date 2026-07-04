from __future__ import annotations

import asyncio

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.plan_repository import SQLitePlanRepository
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.task_repository import SQLiteTaskRepository
from backend.core.plans import PlanLedger, PlanStatus
from backend.core.tasks import TaskLedger, TaskStatus


def run(coro):
    return asyncio.run(coro)


def test_plan_repository_persists_plan_card_state(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    plans = SQLitePlanRepository(persistence)
    conv_id = chat.create_conversation(title="Plans")
    node_id = chat.create_node(conv_id, parent_id=None)

    plan_id = plans.create_plan(
        conv_id,
        entered_node_id=node_id,
        previous_permission_mode="modify_only",
    )
    plans.submit_plan(
        conv_id,
        plan_id,
        plan="1. Do it",
        submitted_node_id=node_id,
    )
    plans.approve_plan(conv_id, plan_id)

    loaded = plans.get_plan(conv_id, plan_id)

    assert loaded["status"] == "approved"
    assert loaded["plan_preview"] == "1. Do it"
    assert loaded["submitted_node_id"] == node_id


def test_task_repository_persists_open_tasks(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    tasks = SQLiteTaskRepository(persistence)
    conv_id = chat.create_conversation(title="Tasks")

    task_id = tasks.create_task(
        conv_id,
        title="Build storage",
        detail="Implement SQLite",
    )
    tasks.update_task(
        conv_id,
        task_id,
        status="blocked",
        evidence_summary="waiting",
    )

    open_tasks = tasks.list_tasks(conv_id, statuses=["blocked"])

    assert len(open_tasks) == 1
    assert open_tasks[0]["id"] == task_id
    assert open_tasks[0]["evidence_summary"] == "waiting"


async def _plan_ledger_repository_active_approve_reject_context_case(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conv_id = chat.create_conversation(title="Plan ledger")
    node_id = chat.create_node(conv_id, parent_id=None)
    ledger = PlanLedger(repository=SQLitePlanRepository(persistence))

    active = await ledger.enter_plan_mode(
        conversation_id=conv_id,
        node_id=node_id,
        previous_permission_mode="auto_approve",
    )
    awaiting = await ledger.submit_plan(
        conversation_id=conv_id,
        plan="1. Persist the plan",
        node_id=node_id,
    )
    approved = await ledger.approve_plan(
        conversation_id=conv_id,
        plan_id=awaiting.plan_id,
    )
    approved_context = await ledger.consume_pending_context(conv_id)

    assert active.status == PlanStatus.ACTIVE
    assert awaiting.status == PlanStatus.AWAITING_APPROVAL
    assert approved.status == PlanStatus.APPROVED
    assert approved.previous_permission_mode == "auto_approve"
    assert approved_context[0].kind == "approved_plan"
    assert approved_context[0].permission_mode == "auto_approve"
    assert await ledger.get_active_or_awaiting(conv_id) is None
    assert await ledger.consume_pending_context(conv_id) == []

    await ledger.enter_plan_mode(conversation_id=conv_id, node_id=node_id)
    second_awaiting = await ledger.submit_plan(
        conversation_id=conv_id,
        plan="1. Try a smaller slice",
        node_id=node_id,
    )
    rejected = await ledger.reject_plan(
        conversation_id=conv_id,
        plan_id=second_awaiting.plan_id,
        feedback="Keep it backend-only.",
    )
    feedback_context = await ledger.consume_pending_context(conv_id)
    current = await ledger.get_active_or_awaiting(conv_id)

    assert rejected.status == PlanStatus.ACTIVE
    assert current is not None
    assert current.plan_id == second_awaiting.plan_id
    assert rejected.feedback[-1]["feedback"] == "Keep it backend-only."
    assert feedback_context[0].kind == "plan_feedback"
    assert "backend-only" in feedback_context[0].content


def test_plan_ledger_repository_active_approve_reject_context(tmp_path):
    run(_plan_ledger_repository_active_approve_reject_context_case(tmp_path))


async def _task_ledger_repository_open_task_listing_and_update_case(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conv_id = chat.create_conversation(title="Task ledger")
    ledger = TaskLedger(repository=SQLiteTaskRepository(persistence))

    first = await ledger.create_task(
        conversation_id=conv_id,
        title="Keep open",
        detail="Still pending",
    )
    second = await ledger.create_task(
        conversation_id=conv_id,
        title="Finish me",
        detail="Needs evidence",
    )
    completed = await ledger.update_task(
        conversation_id=conv_id,
        task_id=second.task_id,
        status=TaskStatus.COMPLETED,
        evidence_summary="SQLite-backed ledger test passed",
    )
    open_tasks = await ledger.list_open_tasks(conv_id)
    all_tasks = await ledger.list_tasks(conv_id, include_finished=True)

    assert completed.status == TaskStatus.COMPLETED
    assert completed.finished_at is not None
    assert [task.task_id for task in open_tasks] == [first.task_id]
    assert [task.task_id for task in all_tasks] == [first.task_id, second.task_id]
    assert all_tasks[1].evidence_summary == "SQLite-backed ledger test passed"


def test_task_ledger_repository_open_task_listing_and_update(tmp_path):
    run(_task_ledger_repository_open_task_listing_and_update_case(tmp_path))
