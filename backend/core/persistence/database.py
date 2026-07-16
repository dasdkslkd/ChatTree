from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .home import resolve_chattree_home
from .migrations import (
    Migration,
    MigrationConnection,
    SchemaMigrationRunner,
    execute_sql_script,
)
from .schema import CURRENT_SCHEMA_VERSION, SCHEMA_SQL


class SQLitePersistence:
    def __init__(self, home: str | Path | None = None) -> None:
        self.home = resolve_chattree_home(home)
        self.db_path = self.home / "chattree.sqlite"
        self.blobs_dir = self.home / "blobs"
        self.tmp_dir = self.home / "tmp"
        self.backup_dir = self.home / "backups"

    def initialize(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            runner = SchemaMigrationRunner(
                db_path=self.db_path,
                backup_dir=self.backup_dir,
                current_version=CURRENT_SCHEMA_VERSION,
                migrations=(
                    Migration(
                        0,
                        1,
                        self._migrate_0_to_1,
                        destructive=True,
                    ),
                ),
            )
            runner.run(conn)
            self._apply_storage_pragmas(conn)
        finally:
            conn.close()

    def _migrate_0_to_1(self, conn: MigrationConnection) -> None:
        self._repair_run_lifecycle_schema(conn)
        self._ensure_node_context_schema(conn)
        self._replace_obsolete_task_schema(conn)
        execute_sql_script(conn, SCHEMA_SQL)
        self._repair_scoped_tool_call_schema(conn)
        execute_sql_script(conn, SCHEMA_SQL)

    def _replace_obsolete_task_schema(self, conn: MigrationConnection) -> None:
        notification_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_notifications)")
        }
        transcript_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(transcript_items)")
        }
        legacy_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('tasks', 'task_steps', 'task_events')"
            )
        }
        active_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('active_tasks', 'active_task_steps', 'task_run_bindings')"
            )
        }
        reset_active_schema = self._active_task_schema_is_obsolete(conn, active_tables)
        if (
            not legacy_tables
            and not reset_active_schema
            and "task_id" not in notification_columns
            and "task_id" not in transcript_columns
        ):
            return

        if legacy_tables or reset_active_schema:
            conn.execute("DROP TABLE IF EXISTS task_run_bindings")
        if reset_active_schema:
            conn.execute("DROP TABLE IF EXISTS active_task_steps")
            conn.execute("DROP TABLE IF EXISTS active_tasks")
        if "task_id" in notification_columns:
            conn.execute("ALTER TABLE task_notifications RENAME TO task_notifications_obsolete")
            conn.execute(_TASK_NOTIFICATIONS_TABLE_SQL)
            conn.execute(
                """
                INSERT INTO task_notifications (
                  id, conversation_id, source_run_id, source_run_kind, status,
                  delivery_node_id, bound_at, bound_by, summary, content, payload_json,
                  delivered_run_id, delivered_node_id, created_at, updated_at
                )
                SELECT
                  id, conversation_id, source_run_id, source_run_kind, status,
                  delivery_node_id, bound_at, bound_by, summary, content, payload_json,
                  delivered_run_id, delivered_node_id, created_at, updated_at
                FROM task_notifications_obsolete
                """
            )
            conn.execute("DROP TABLE task_notifications_obsolete")
        if "task_id" in transcript_columns:
            conn.execute("ALTER TABLE transcript_items RENAME TO transcript_items_obsolete")
            conn.execute(_TRANSCRIPT_ITEMS_TABLE_SQL)
            conn.execute(
                """
                INSERT INTO transcript_items (
                  id, conversation_id, node_id, anchor_node_id, run_id, plan_id,
                  message_id, item_type, local_order, visibility, status, summary,
                  preview, props_json, created_at, updated_at
                )
                SELECT
                  id, conversation_id, node_id, anchor_node_id, run_id, plan_id,
                  message_id, item_type, local_order, visibility, status, summary,
                  preview, props_json, created_at, updated_at
                FROM transcript_items_obsolete
                """
            )
            conn.execute("DROP TABLE transcript_items_obsolete")
        conn.execute("DROP TABLE IF EXISTS task_events")
        conn.execute("DROP TABLE IF EXISTS task_steps")
        conn.execute("DROP TABLE IF EXISTS tasks")

    def _active_task_schema_is_obsolete(
        self,
        conn: MigrationConnection,
        active_tables: set[str],
    ) -> bool:
        if not active_tables:
            return False
        if active_tables != {"active_tasks", "active_task_steps", "task_run_bindings"}:
            return True
        expected_columns = {
            "active_tasks": {
                "conversation_id", "generation_id", "revision", "title", "detail_inline",
                "detail_blob_id", "created_by_run_id", "created_by_tool_call_id",
                "created_at", "updated_at",
            },
            "active_task_steps": {
                "conversation_id", "position", "title", "detail_inline", "detail_blob_id",
                "status", "evidence_run_id", "evidence_summary", "updated_at",
            },
            "task_run_bindings": {
                "run_id", "conversation_id", "task_generation_id", "step_position",
                "base_revision", "created_at",
            },
        }
        for table, expected in expected_columns.items():
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if columns != expected:
                return True
        binding_fks = conn.execute("PRAGMA foreign_key_list(task_run_bindings)").fetchall()
        generation_fk = {
            (row["table"], row["from"], row["to"], row["on_delete"])
            for row in binding_fks
            if row["table"] == "active_tasks"
        }
        return generation_fk != {
            ("active_tasks", "conversation_id", "conversation_id", "CASCADE"),
            ("active_tasks", "task_generation_id", "generation_id", "CASCADE"),
        }

    def _repair_scoped_tool_call_schema(self, conn: MigrationConnection) -> None:
        if not self._needs_scoped_tool_call_repair(conn):
            return

        conn.execute("ALTER TABLE tool_results RENAME TO tool_results_old")
        conn.execute("ALTER TABLE tool_calls RENAME TO tool_calls_old")
        execute_sql_script(conn, _SCOPED_TOOL_CALL_REPAIR_TABLES_SQL)
        self._copy_common_columns(
            conn,
            "tool_calls_old",
            "tool_calls",
            (
                "id",
                "conversation_id",
                "node_id",
                "run_id",
                "assistant_message_id",
                "call_index",
                "name",
                "args_inline",
                "args_blob_id",
                "args_preview",
                "status",
                "created_at",
                "updated_at",
            ),
        )
        self._copy_common_columns(
            conn,
            "tool_results_old",
            "tool_results",
            (
                "id",
                "conversation_id",
                "node_id",
                "run_id",
                "tool_call_id",
                "status",
                "output_preview",
                "output_blob_id",
                "output_size",
                "truncated",
                "metadata_json",
                "created_at",
            ),
        )
        conn.execute("DROP TABLE tool_results_old")
        conn.execute("DROP TABLE tool_calls_old")

    def _needs_scoped_tool_call_repair(self, conn: MigrationConnection) -> bool:
        tool_call_columns = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        if not tool_call_columns:
            return False
        if tool_call_columns.get("conversation_id") != 1 or tool_call_columns.get("id") != 2:
            return True
        return self._tool_results_has_global_tool_call_fk(conn)

    def _tool_results_has_global_tool_call_fk(self, conn: MigrationConnection) -> bool:
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in conn.execute("PRAGMA foreign_key_list(tool_results)").fetchall():
            if row["table"] == "tool_calls":
                grouped.setdefault(row["id"], []).append(row)
        for rows in grouped.values():
            if len(rows) == 1 and rows[0]["from"] == "tool_call_id" and rows[0]["to"] == "id":
                return True
        return False

    def _repair_run_lifecycle_schema(self, conn: MigrationConnection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if not columns:
            return
        if "created_by_run_id" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN created_by_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL")
        if "cancellation_parent_run_id" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN cancellation_parent_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL")

    def _ensure_node_context_schema(self, conn: MigrationConnection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(nodes)").fetchall()
        }
        if columns and "task_context_mode" not in columns:
            conn.execute(
                "ALTER TABLE nodes ADD COLUMN task_context_mode TEXT NOT NULL DEFAULT 'attached'"
            )

    def _copy_common_columns(
        self,
        conn: MigrationConnection,
        source_table: str,
        target_table: str,
        columns: tuple[str, ...],
    ) -> None:
        source_columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({source_table})")
        }
        target_columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({target_table})")
        }
        common_columns = [
            column
            for column in columns
            if column in source_columns and column in target_columns
        ]
        column_sql = ", ".join(common_columns)
        conn.execute(
            f"""
            INSERT INTO {target_table} ({column_sql})
            SELECT {column_sql}
            FROM {source_table}
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.home.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        self._apply_storage_pragmas(conn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _apply_storage_pragmas(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")


_TASK_NOTIFICATIONS_TABLE_SQL = """
CREATE TABLE task_notifications (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  source_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  source_run_kind TEXT NOT NULL,
  status TEXT NOT NULL,
  delivery_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  bound_at INTEGER,
  bound_by TEXT,
  summary TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  payload_json TEXT,
  delivered_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  delivered_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(conversation_id, source_run_id),
  FOREIGN KEY (conversation_id, source_run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, delivery_node_id) REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, delivered_run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, delivered_node_id) REFERENCES nodes(conversation_id, id)
);
"""

_TRANSCRIPT_ITEMS_TABLE_SQL = """
CREATE TABLE transcript_items (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  anchor_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  plan_id TEXT REFERENCES plans(id) ON DELETE SET NULL,
  message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
  item_type TEXT NOT NULL,
  local_order INTEGER NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'main',
  status TEXT,
  summary TEXT NOT NULL DEFAULT '',
  preview TEXT NOT NULL DEFAULT '',
  props_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(conversation_id, id),
  FOREIGN KEY (conversation_id, node_id) REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, anchor_node_id) REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, plan_id) REFERENCES plans(conversation_id, id),
  FOREIGN KEY (conversation_id, message_id) REFERENCES messages(conversation_id, id)
);
"""

_SCOPED_TOOL_CALL_REPAIR_TABLES_SQL = """
CREATE TABLE tool_calls (
  id TEXT NOT NULL,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
  call_index INTEGER NOT NULL,
  name TEXT NOT NULL,
  args_inline TEXT,
  args_blob_id TEXT REFERENCES blobs(id),
  args_preview TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (conversation_id, id),
  FOREIGN KEY (conversation_id, node_id) REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, assistant_message_id)
    REFERENCES messages(conversation_id, id)
);

CREATE TABLE tool_results (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  tool_call_id TEXT,
  status TEXT NOT NULL,
  output_preview TEXT NOT NULL DEFAULT '',
  output_blob_id TEXT REFERENCES blobs(id),
  output_size INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(conversation_id, id),
  FOREIGN KEY (conversation_id, node_id) REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, tool_call_id)
    REFERENCES tool_calls(conversation_id, id)
    ON DELETE CASCADE
);
"""
