from __future__ import annotations

import sqlite3
import uuid
from time import time
from typing import Any, Iterable

from .blob_store import BlobStore
from .content import store_text_content
from .database import SQLitePersistence


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted", "stopped"}


class SQLiteTaskRepository:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def create_task(
        self,
        conversation_id: str,
        *,
        title: str,
        detail: str,
        steps: Iterable[dict[str, Any]],
        created_by_run_id: str | None = None,
        created_by_tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        generation_id = f"taskgen_{uuid.uuid4().hex}"
        task_detail = store_text_content(self.persistence, detail)
        prepared_steps = []
        for position, item in enumerate(steps, start=1):
            data = dict(item)
            step_detail = store_text_content(self.persistence, str(data.get("detail") or ""))
            prepared_steps.append((position, data, step_detail))

        now = time()
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = self._get_active_task_in_connection(conn, conversation_id)
            if existing is not None:
                if (
                    created_by_tool_call_id
                    and existing.get("created_by_tool_call_id") == created_by_tool_call_id
                ):
                    return existing
                raise RuntimeError("conversation already has an active task")
            conn.execute(
                """
                INSERT INTO active_tasks (
                  conversation_id,
                  generation_id,
                  revision,
                  title,
                  detail_inline,
                  detail_blob_id,
                  created_by_run_id,
                  created_by_tool_call_id,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    generation_id,
                    title,
                    task_detail.inline,
                    task_detail.blob_id,
                    created_by_run_id,
                    created_by_tool_call_id,
                    now,
                    now,
                ),
            )
            for position, item, step_detail in prepared_steps:
                conn.execute(
                    """
                    INSERT INTO active_task_steps (
                      conversation_id,
                      position,
                      title,
                      detail_inline,
                      detail_blob_id,
                      status,
                      evidence_summary,
                      updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending', '', ?)
                    """,
                    (
                        conversation_id,
                        position,
                        str(item.get("title") or ""),
                        step_detail.inline,
                        step_detail.blob_id,
                        now,
                    ),
                )
            created = self._get_active_task_in_connection(conn, conversation_id)
            if created is None:
                raise RuntimeError("active task insert did not produce a row")
            return created

    def get_active_task(self, conversation_id: str) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            return self._get_active_task_in_connection(conn, conversation_id)

    def set_step_result(
        self,
        conversation_id: str,
        *,
        step: int,
        status: str,
        evidence_summary: str,
        evidence_run_id: str | None,
        expected_generation: str | None,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._require_version(
                conn,
                conversation_id,
                expected_generation,
                expected_revision,
            )
            self._ensure_no_active_binding(conn, conversation_id)
            current = conn.execute(
                """
                SELECT status
                FROM active_task_steps
                WHERE conversation_id = ? AND position = ?
                """,
                (conversation_id, step),
            ).fetchone()
            if current is None:
                raise KeyError(step)
            unfinished_previous = conn.execute(
                """
                SELECT position
                FROM active_task_steps
                WHERE conversation_id = ?
                  AND position < ?
                  AND status != 'completed'
                LIMIT 1
                """,
                (conversation_id, step),
            ).fetchone()
            if unfinished_previous is not None:
                raise RuntimeError("previous task steps must be completed first")
            if current["status"] == "completed" and status != "completed":
                raise ValueError("completed task steps are immutable")
            now = time()
            conn.execute(
                """
                UPDATE active_task_steps
                SET status = ?,
                    evidence_run_id = ?,
                    evidence_summary = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND position = ?
                """,
                (status, evidence_run_id, evidence_summary, now, conversation_id, step),
            )
            conn.execute(
                """
                UPDATE active_tasks
                SET revision = revision + 1,
                    updated_at = ?
                WHERE conversation_id = ? AND generation_id = ?
                """,
                (now, conversation_id, task["generation_id"]),
            )
            updated_task = self._get_active_task_in_connection(conn, conversation_id)
            if updated_task is None:
                raise RuntimeError("task step update did not produce a task snapshot")
            task_snapshot = self._public_task_snapshot(updated_task)
            if self._all_steps_completed(conn, conversation_id):
                conn.execute("DELETE FROM active_tasks WHERE conversation_id = ?", (conversation_id,))
                return {
                    "completed": True,
                    "task": None,
                    "task_snapshot": task_snapshot,
                }
            return {
                "completed": False,
                "task": updated_task,
                "task_snapshot": task_snapshot,
            }

    def cancel_task(
        self,
        conversation_id: str,
        *,
        expected_generation: str | None,
        expected_revision: int | None,
    ) -> bool:
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = self._require_version(
                conn,
                conversation_id,
                expected_generation,
                expected_revision,
            )
            deleted = conn.execute(
                "DELETE FROM active_tasks WHERE conversation_id = ? AND generation_id = ?",
                (conversation_id, task["generation_id"]),
            )
            return deleted.rowcount > 0

    def bind_run_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        conversation_id = str(binding.get("conversation_id") or "")
        expected_generation = str(binding.get("task_generation_id") or "")
        step = int(binding.get("step_position") or 0)
        task = self._require_version(
            conn,
            conversation_id,
            expected_generation,
            int(binding.get("base_revision", -1)),
        )
        expected_revision = int(binding.get("base_revision", -1))
        if expected_revision != int(task["revision"]):
            raise RuntimeError("active task revision changed")
        step_row = conn.execute(
            """
            SELECT status
            FROM active_task_steps
            WHERE conversation_id = ? AND position = ?
            """,
            (conversation_id, step),
        ).fetchone()
        if step_row is None:
            raise KeyError(step)
        if step_row["status"] == "completed":
            raise RuntimeError("task step is already completed")
        unfinished_previous = conn.execute(
            """
            SELECT position
            FROM active_task_steps
            WHERE conversation_id = ?
              AND position < ?
              AND status != 'completed'
            LIMIT 1
            """,
            (conversation_id, step),
        ).fetchone()
        if unfinished_previous is not None:
            raise RuntimeError("previous task steps must be completed first")
        self._ensure_no_active_binding(conn, conversation_id)
        conn.execute(
            """
            INSERT INTO task_run_bindings (
              run_id,
              conversation_id,
              task_generation_id,
              step_position,
              base_revision,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                conversation_id,
                task["generation_id"],
                step,
                expected_revision,
                time(),
            ),
        )
        return {
            "task_generation_id": task["generation_id"],
            "task_step_position": step,
        }

    def finish_run_binding_in_connection(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        terminal_status: str,
        error: str | None,
        summary: str = "",
    ) -> dict[str, Any] | None:
        binding = conn.execute(
            """
            SELECT *
            FROM task_run_bindings
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if binding is None:
            return None
        now = time()
        task = conn.execute(
            """
            SELECT generation_id
            FROM active_tasks
            WHERE conversation_id = ?
            """,
            (binding["conversation_id"],),
        ).fetchone()
        if task is None or task["generation_id"] != binding["task_generation_id"]:
            conn.execute(
                "DELETE FROM task_run_bindings WHERE run_id = ?",
                (run_id,),
            )
            return None

        next_status = None
        if terminal_status == "completed":
            next_status = "completed"
        elif terminal_status == "failed":
            next_status = "blocked"
        if next_status is not None:
            evidence = error or (f"Run completed: {summary}" if summary else f"Run {terminal_status}")
            conn.execute(
                """
                UPDATE active_task_steps
                SET status = ?,
                    evidence_run_id = ?,
                    evidence_summary = ?,
                    updated_at = ?
                WHERE conversation_id = ? AND position = ?
                """,
                (
                    next_status,
                    run_id,
                    evidence,
                    now,
                    binding["conversation_id"],
                    binding["step_position"],
                ),
            )
        conn.execute("DELETE FROM task_run_bindings WHERE run_id = ?", (run_id,))
        conn.execute(
            """
            UPDATE active_tasks
            SET revision = revision + 1, updated_at = ?
            WHERE conversation_id = ? AND generation_id = ?
            """,
            (now, binding["conversation_id"], binding["task_generation_id"]),
        )
        task_completed = next_status == "completed" and self._all_steps_completed(
            conn,
            binding["conversation_id"],
        )
        updated_task = self._get_active_task_in_connection(conn, binding["conversation_id"])
        if updated_task is None:
            raise RuntimeError("run completion did not produce a task snapshot")
        task_snapshot = self._public_task_snapshot(updated_task)
        if task_completed:
            conn.execute(
                "DELETE FROM active_tasks WHERE conversation_id = ? AND generation_id = ?",
                (binding["conversation_id"], binding["task_generation_id"]),
            )
        return {
            "kind": "run_finished",
            "task_status": "completed" if task_completed else "active",
            "step": int(binding["step_position"]),
            "step_status": next_status or "released",
            "run_status": terminal_status,
            "task_snapshot": task_snapshot,
        }

    def finish_run_binding(
        self,
        *,
        run_id: str,
        terminal_status: str,
        error: str | None,
        summary: str = "",
    ) -> dict[str, Any] | None:
        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.finish_run_binding_in_connection(
                conn,
                run_id=run_id,
                terminal_status=terminal_status,
                error=error,
                summary=summary,
            )

    def _get_active_task_in_connection(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM active_tasks WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["detail"] = self._read_text(
            conn,
            data.pop("detail_inline"),
            data.pop("detail_blob_id"),
        )
        step_rows = conn.execute(
            """
            SELECT *
            FROM active_task_steps
            WHERE conversation_id = ?
            ORDER BY position
            """,
            (conversation_id,),
        ).fetchall()
        data["steps"] = [self._step_from_row(conn, step) for step in step_rows]
        binding = conn.execute(
            """
            SELECT binding.run_id, binding.step_position, run.status
            FROM task_run_bindings AS binding
            JOIN runs AS run ON run.id = binding.run_id
            WHERE binding.conversation_id = ?
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        data["active_run_id"] = binding["run_id"] if binding is not None else None
        data["active_step"] = int(binding["step_position"]) if binding is not None else None
        run_status = str(binding["status"] or "") if binding is not None else ""
        if run_status == "stopping":
            data["execution_state"] = "stopping"
        elif binding is not None and run_status not in TERMINAL_RUN_STATUSES:
            data["execution_state"] = "running"
        else:
            data["execution_state"] = "idle"
        return data

    def _step_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["detail"] = self._read_text(
            conn,
            data.pop("detail_inline"),
            data.pop("detail_blob_id"),
        )
        data["evidence_summary"] = str(data.get("evidence_summary") or "")
        return data

    @staticmethod
    def _public_task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": str(task.get("title") or ""),
            "steps": [
                {
                    "position": int(step["position"]),
                    "title": str(step.get("title") or ""),
                    "status": str(step.get("status") or "pending"),
                }
                for step in task.get("steps") or []
            ],
        }

    def _read_text(
        self,
        conn: sqlite3.Connection,
        inline: str | None,
        blob_id: str | None,
    ) -> str:
        if inline is not None:
            return inline
        if blob_id:
            return BlobStore(self.persistence).get_text_in_connection(conn, blob_id)
        return ""

    def _require_version(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        expected_generation: str | None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        task = conn.execute(
            "SELECT * FROM active_tasks WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if task is None:
            raise KeyError(conversation_id)
        data = dict(task)
        if expected_generation and data["generation_id"] != expected_generation:
            raise RuntimeError("active task generation changed")
        if expected_revision is not None and int(data["revision"]) != expected_revision:
            raise RuntimeError("active task revision changed")
        return data

    def _ensure_no_active_binding(self, conn: sqlite3.Connection, conversation_id: str) -> None:
        active = conn.execute(
            """
            SELECT run_id
            FROM task_run_bindings
            WHERE conversation_id = ?
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if active is not None:
            raise RuntimeError("active task already has a running step")

    def _all_steps_completed(self, conn: sqlite3.Connection, conversation_id: str) -> bool:
        row = conn.execute(
            """
            SELECT COUNT(*) AS remaining
            FROM active_task_steps
            WHERE conversation_id = ? AND status != 'completed'
            """,
            (conversation_id,),
        ).fetchone()
        return row is not None and int(row["remaining"]) == 0
