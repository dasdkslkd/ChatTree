from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .home import resolve_chattree_home
from .schema import SCHEMA_SQL


class SQLitePersistence:
    def __init__(self, home: str | Path | None = None) -> None:
        self.home = resolve_chattree_home(home)
        self.db_path = self.home / "chattree.sqlite"
        self.blobs_dir = self.home / "blobs"
        self.tmp_dir = self.home / "tmp"

    def initialize(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._repair_run_lifecycle_schema(conn)
            conn.executescript(SCHEMA_SQL)
            self._repair_scoped_tool_call_schema(conn)
            self._repair_run_lifecycle_schema(conn)
            conn.executescript(SCHEMA_SQL)

    def _repair_scoped_tool_call_schema(self, conn: sqlite3.Connection) -> None:
        if not self._needs_scoped_tool_call_repair(conn):
            return

        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.execute("ALTER TABLE tool_results RENAME TO tool_results_old")
            conn.execute("ALTER TABLE tool_calls RENAME TO tool_calls_old")
            conn.executescript(_SCOPED_TOOL_CALL_REPAIR_TABLES_SQL)
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
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _needs_scoped_tool_call_repair(self, conn: sqlite3.Connection) -> bool:
        tool_call_columns = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        if not tool_call_columns:
            return False
        if tool_call_columns.get("conversation_id") != 1 or tool_call_columns.get("id") != 2:
            return True
        return self._tool_results_has_global_tool_call_fk(conn)

    def _tool_results_has_global_tool_call_fk(self, conn: sqlite3.Connection) -> bool:
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in conn.execute("PRAGMA foreign_key_list(tool_results)").fetchall():
            if row["table"] == "tool_calls":
                grouped.setdefault(row["id"], []).append(row)
        for rows in grouped.values():
            if len(rows) == 1 and rows[0]["from"] == "tool_call_id" and rows[0]["to"] == "id":
                return True
        return False

    def _repair_run_lifecycle_schema(self, conn: sqlite3.Connection) -> None:
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

    def _copy_common_columns(
        self,
        conn: sqlite3.Connection,
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
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


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
