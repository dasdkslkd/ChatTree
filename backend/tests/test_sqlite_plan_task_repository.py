from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.content import INLINE_TEXT_LIMIT
from backend.core.persistence.plan_repository import SQLitePlanRepository
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.task_repository import SQLiteTaskRepository
from backend.core.plans import PlanContextInjection, PlanLedger, PlanSession, PlanStatus
from backend.core.tasks import TaskLedger, TaskRecord, TaskStatus
from backend.core.persistence import task_repository as task_repository_module


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


def test_task_repository_and_ledger_keep_insertion_order_for_same_timestamp(tmp_path, monkeypatch):
    class FixedUuid:
        def __init__(self, value: str) -> None:
            self.hex = value

    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    repository = SQLiteTaskRepository(persistence)
    ledger = TaskLedger(repository=repository)
    conv_id = chat.create_conversation(title="Tasks")

    uuids = iter([FixedUuid("f" * 32), FixedUuid("0" * 32)])
    monkeypatch.setattr(task_repository_module.uuid, "uuid4", lambda: next(uuids))
    monkeypatch.setattr(task_repository_module, "time", lambda: 1234.0)

    first_id = repository.create_task(conv_id, title="First")
    second_id = repository.create_task(conv_id, title="Second")

    assert [task["id"] for task in repository.list_tasks(conv_id)] == [first_id, second_id]
    assert [task.task_id for task in run(ledger.list_open_tasks(conv_id))] == [first_id, second_id]


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


async def _task_ledger_repository_load_snapshot_persists_to_sqlite_case(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conv_id = chat.create_conversation(title="Task snapshot")
    repository = SQLiteTaskRepository(persistence)
    ledger = TaskLedger(repository=repository)
    record = TaskRecord(
        task_id="task_restored",
        conversation_id=conv_id,
        title="Restore me",
        detail="Visible through SQLite",
        status=TaskStatus.IN_PROGRESS,
        created_by_run_id="run_legacy",
        metadata={"source": "legacy"},
        created_at=11.0,
        updated_at=12.0,
    )

    await ledger.load_snapshot(conv_id, [record.to_dict()])

    listed = await ledger.list_tasks(conv_id)
    loaded = await ledger.get_task(conv_id, record.task_id)
    snapshot = await ledger.snapshot(conv_id)

    assert [task.task_id for task in listed] == [record.task_id]
    assert loaded == record
    assert snapshot == [record.to_dict()]


def test_task_ledger_repository_load_snapshot_persists_to_sqlite(tmp_path):
    run(_task_ledger_repository_load_snapshot_persists_to_sqlite_case(tmp_path))


async def _task_ledger_repository_snapshot_reflects_sqlite_after_mutation_case(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    chat = ChatRepository(persistence)
    conv_id = chat.create_conversation(title="Task snapshot")
    repository = SQLiteTaskRepository(persistence)
    ledger = TaskLedger(repository=repository)
    legacy = TaskRecord(
        task_id="task_legacy",
        conversation_id=conv_id,
        title="Legacy",
        detail="Loaded before new mutations",
        created_at=10.0,
        updated_at=10.0,
    )
    await ledger.load_snapshot(conv_id, [legacy.to_dict()])

    created = await ledger.create_task(
        conversation_id=conv_id,
        title="New task",
        detail="Created in SQLite",
    )
    updated = await ledger.update_task(
        conversation_id=conv_id,
        task_id=created.task_id,
        status=TaskStatus.COMPLETED,
        evidence_summary="done",
    )
    snapshot = await ledger.snapshot(conv_id)

    assert snapshot == [legacy.to_dict(), updated.to_dict()]


def test_task_ledger_repository_snapshot_reflects_sqlite_after_mutation(tmp_path):
    run(_task_ledger_repository_snapshot_reflects_sqlite_after_mutation_case(tmp_path))
