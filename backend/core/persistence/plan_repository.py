from __future__ import annotations

import uuid
from time import time
from typing import Any

from .database import SQLitePersistence


ACTIVE_PLAN_STATUS_VALUES = ("active", "awaiting_question", "awaiting_approval")


class SQLitePlanRepository:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def create_plan(
        self,
        conversation_id: str,
        *,
        entered_node_id: str | None = None,
        previous_permission_mode: str = "modify_only",
        entered_run_id: str | None = None,
    ) -> str:
        plan_id = f"plan_{uuid.uuid4().hex}"
        now = time()
        with self.persistence.connect() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                  id,
                  conversation_id,
                  status,
                  entered_node_id,
                  entered_run_id,
                  previous_permission_mode,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    conversation_id,
                    entered_node_id,
                    entered_run_id,
                    previous_permission_mode,
                    now,
                    now,
                ),
            )
        return plan_id

    def ask_question(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        asked_node_id: str | None = None,
        tool_call_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'awaiting_question',
                    question_tool_call_id = ?,
                    blocking_node_id = ?,
                    blocking_run_id = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    tool_call_id,
                    asked_node_id,
                    run_id,
                    now,
                    conversation_id,
                    plan_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
        return self._require_plan(conversation_id, plan_id)

    def await_approval(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        tool_call_id: str,
        node_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'awaiting_approval',
                    exit_tool_call_id = ?,
                    blocking_node_id = ?,
                    blocking_run_id = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    tool_call_id,
                    node_id,
                    run_id,
                    now,
                    conversation_id,
                    plan_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
        return self._require_plan(conversation_id, plan_id)

    def answer_question(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        answer: str,
    ) -> dict[str, Any]:
        now = time()
        self._require_plan(conversation_id, plan_id)
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'active',
                    question_tool_call_id = NULL,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    now,
                    conversation_id,
                    plan_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
        return self._require_plan(conversation_id, plan_id)

    def approve_plan(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        approved_run_id: str | None = None,
    ) -> dict[str, Any]:
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'approved',
                    approved_run_id = ?,
                    approved_at = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (approved_run_id, now, now, conversation_id, plan_id),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
        return self._require_plan(conversation_id, plan_id)

    def update_approved_run_id(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        approved_run_id: str | None,
    ) -> dict[str, Any]:
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET approved_run_id = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ? AND status = 'approved'
                """,
                (approved_run_id, now, conversation_id, plan_id),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
        return self._require_plan(conversation_id, plan_id)

    def reject_plan(
        self,
        conversation_id: str,
        plan_id: str,
        *,
        feedback: str = "",
    ) -> dict[str, Any]:
        now = time()
        with self.persistence.connect() as conn:
            updated = conn.execute(
                """
                UPDATE plans
                SET status = 'active',
                    exit_tool_call_id = NULL,
                    rejected_at = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND id = ?
                """,
                (
                    now,
                    now,
                    conversation_id,
                    plan_id,
                ),
            )
            if updated.rowcount == 0:
                raise KeyError(plan_id)
        return self._require_plan(conversation_id, plan_id)

    def get_plan(self, conversation_id: str, plan_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM plans
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, plan_id),
            ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def get_active_or_awaiting(self, conversation_id: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_PLAN_STATUS_VALUES)
        with self.persistence.connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM plans
                WHERE conversation_id = ?
                  AND status IN ({placeholders})
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (conversation_id, *ACTIVE_PLAN_STATUS_VALUES),
            ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def get_latest(self, conversation_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM plans
                WHERE conversation_id = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def list_plans(self, conversation_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM plans
                WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
        return [self._plan_from_row(row) for row in rows]

    def _require_plan(self, conversation_id: str, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(conversation_id, plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    def _plan_from_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["plan_id"] = data["id"]
        return data
