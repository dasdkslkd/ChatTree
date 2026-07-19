from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.content import INLINE_TEXT_LIMIT
from backend.core.persistence.plan_repository import SQLitePlanRepository
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.task_repository import SQLiteTaskRepository
from backend.core.plans import PlanContextInjection, PlanLedger, PlanSession, PlanStatus
from backend.core.tasks import ActiveTaskVersionConflictError


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


def test_plan_repository_persists_proposal_revisions(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    plans = SQLitePlanRepository(persistence)
    conv_id = chat.create_conversation(title="Plan proposals")
    node_id = chat.create_node(conv_id, parent_id=None)

    plan_id = plans.create_plan(conv_id, entered_node_id=node_id)
    first = plans.submit_plan(
        conv_id,
        plan_id,
        plan="First plan",
        submitted_node_id=node_id,
        submitted_run_id="run-1",
        tool_call_id="call-exit-1",
    )
    plans.reject_plan(conv_id, plan_id, feedback="Try again.")
    second = plans.submit_plan(
        conv_id,
        plan_id,
        plan="Second plan",
        submitted_node_id=node_id,
        submitted_run_id="run-2",
        tool_call_id="call-exit-2",
    )

    assert first["proposal_revision"] == 1
    assert second["proposal_revision"] == 2
    assert [proposal["status"] for proposal in second["proposals"]] == [
        "rejected",
        "awaiting_approval",
    ]
    assert second["proposals"][0]["tool_call_id"] == "call-exit-1"
    assert second["proposals"][1]["tool_call_id"] == "call-exit-2"


async def _plan_ledger_repository_loads_legacy_snapshot_case(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conv_id = chat.create_conversation(title="Plans")
    repository = SQLitePlanRepository(persistence)
    ledger = PlanLedger(repository=repository)
    session = PlanSession(
        plan_id="plan_legacy",
        conversation_id=conv_id,
        status=PlanStatus.AWAITING_APPROVAL,
        previous_permission_mode="auto_approve",
        plan="1. Restore legacy plan",
        created_at=10.0,
        updated_at=20.0,
    )
    context = PlanContextInjection(
        kind="approved_plan",
        conversation_id=conv_id,
        plan_id=session.plan_id,
        content="Continue from restored plan",
        permission_mode="auto_approve",
        created_at=21.0,
    )

    await ledger.load_snapshot(
        conv_id,
        {
            "plans": [session.to_dict()],
            "pending_context": [context.to_dict()],
        },
    )
    reloaded = PlanLedger(repository=repository)

    active = await reloaded.get_active_or_awaiting(conv_id)
    latest = await reloaded.get_latest(conv_id)
    snapshot = await reloaded.snapshot(conv_id)
    consumed = await reloaded.consume_pending_context(conv_id)

    assert active is not None
    assert active.plan_id == session.plan_id
    assert active.plan == session.plan
    assert latest is not None
    assert latest.plan_id == session.plan_id
    assert snapshot["plans"] == [session.to_dict()]
    assert snapshot["pending_context"] == [context.to_dict()]
    assert consumed == [context]
    assert await reloaded.consume_pending_context(conv_id) == []


def test_plan_ledger_repository_loads_legacy_snapshot(tmp_path):
    run(_plan_ledger_repository_loads_legacy_snapshot_case(tmp_path))


async def _plan_ledger_repository_loads_long_snapshot_case(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conv_id = chat.create_conversation(title="Long plan")
    repository = SQLitePlanRepository(persistence)
    ledger = PlanLedger(repository=repository)
    long_plan = "restore this plan\n" + ("x" * INLINE_TEXT_LIMIT)
    session = PlanSession(
        plan_id="plan_long_snapshot",
        conversation_id=conv_id,
        status=PlanStatus.AWAITING_APPROVAL,
        previous_permission_mode="modify_only",
        plan=long_plan,
        created_at=10.0,
        updated_at=20.0,
    )

    await ledger.load_snapshot(conv_id, {"plans": [session.to_dict()]})
    reloaded = PlanLedger(repository=repository)

    active = await reloaded.get_active_or_awaiting(conv_id)
    latest = await reloaded.get_latest(conv_id)

    assert active is not None
    assert active.plan == long_plan
    assert latest is not None
    assert latest.plan == long_plan


def test_plan_ledger_repository_loads_long_snapshot_without_locking(tmp_path):
    run(_plan_ledger_repository_loads_long_snapshot_case(tmp_path))


def test_plan_repository_rejects_snapshot_pending_context_from_other_conversation(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    repository = SQLitePlanRepository(persistence)
    conv_id = chat.create_conversation(title="Plans")
    other_conv_id = chat.create_conversation(title="Other plans")
    plan_id = "plan_owned_by_snapshot"
    plan = PlanSession(
        plan_id=plan_id,
        conversation_id=conv_id,
        status=PlanStatus.AWAITING_APPROVAL,
        previous_permission_mode="modify_only",
        plan="1. Keep ownership tight",
        created_at=10.0,
        updated_at=11.0,
    )

    with pytest.raises(ValueError):
        repository.replace_snapshot(
            conv_id,
            plans=[plan.to_dict()],
            pending_context=[
                {
                    "kind": "approved_plan",
                    "conversation_id": other_conv_id,
                    "plan_id": plan_id,
                    "content": "This context belongs elsewhere",
                    "permission_mode": "modify_only",
                    "created_at": 12.0,
                }
            ],
        )

    assert repository.peek_pending_context(conv_id) == []


class _BarrierCursor:
    def __init__(self, cursor, barrier: Barrier) -> None:
        self._cursor = cursor
        self._barrier = barrier

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._barrier.wait(timeout=5)
        return rows


class _BarrierConnection:
    def __init__(self, conn, barrier: Barrier) -> None:
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql, parameters=()):
        cursor = self._conn.execute(sql, parameters)
        normalized = " ".join(str(sql).split()).lower()
        if normalized.startswith("select id, payload_json from plan_events"):
            return _BarrierCursor(cursor, self._barrier)
        return cursor

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _BarrierPersistence:
    def __init__(self, persistence: SQLitePersistence, barrier: Barrier) -> None:
        self._persistence = persistence
        self._barrier = barrier

    @contextmanager
    def connect(self):
        with self._persistence.connect() as conn:
            yield _BarrierConnection(conn, self._barrier)

    def __getattr__(self, name):
        return getattr(self._persistence, name)


def test_plan_repository_consume_pending_context_is_atomic_across_connections(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conv_id = chat.create_conversation(title="Plans")
    seed_repository = SQLitePlanRepository(persistence)
    plan_id = seed_repository.create_plan(conv_id)
    seed_repository.append_pending_context(
        conv_id,
        plan_id,
        {
            "kind": "approved_plan",
            "conversation_id": conv_id,
            "plan_id": plan_id,
            "content": "Only once",
            "permission_mode": "modify_only",
        },
    )
    barrier = Barrier(2)
    delayed_persistence = _BarrierPersistence(persistence, barrier)
    repositories = [
        SQLitePlanRepository(delayed_persistence),
        SQLitePlanRepository(delayed_persistence),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda repo: repo.consume_pending_context(conv_id), repositories))

    returned_payloads = [payload for result in results for payload in result]
    assert len(returned_payloads) == 1
    assert returned_payloads[0]["content"] == "Only once"


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


def test_task_repository_version_conflicts_report_actual_current_version(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conversation_id = chat.create_conversation(title="Versioned tasks")
    repository = SQLiteTaskRepository(persistence)
    first = repository.create_task(
        conversation_id,
        title="First",
        detail="",
        steps=[{"title": "Step", "detail": ""}],
    )
    updated = repository.set_step_result(
        conversation_id,
        step=1,
        status="blocked",
        evidence_summary="retry",
        evidence_run_id=None,
        expected_generation=first["generation_id"],
        expected_revision=first["revision"],
    )["task"]

    with pytest.raises(ActiveTaskVersionConflictError) as revision_conflict:
        repository.set_step_result(
            conversation_id,
            step=1,
            status="blocked",
            evidence_summary="stale revision",
            evidence_run_id=None,
            expected_generation=first["generation_id"],
            expected_revision=first["revision"],
        )

    assert revision_conflict.value.current_generation_id == updated["generation_id"]
    assert revision_conflict.value.current_revision == updated["revision"]

    repository.cancel_task(
        conversation_id,
        expected_generation=updated["generation_id"],
        expected_revision=updated["revision"],
    )
    replacement = repository.create_task(
        conversation_id,
        title="Replacement",
        detail="",
        steps=[{"title": "Keep", "detail": ""}],
    )

    with pytest.raises(ActiveTaskVersionConflictError) as generation_conflict:
        repository.set_step_result(
            conversation_id,
            step=1,
            status="completed",
            evidence_summary="late result",
            evidence_run_id=None,
            expected_generation=first["generation_id"],
            expected_revision=first["revision"],
        )

    assert generation_conflict.value.current_generation_id == replacement[
        "generation_id"
    ]
    assert generation_conflict.value.current_revision == replacement["revision"]
