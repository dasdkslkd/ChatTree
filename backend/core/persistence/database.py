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
from .schema import CURRENT_SCHEMA_VERSION, SCHEMA_V1_SQL


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
        existing_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM main.sqlite_master WHERE type = 'table'"
            )
        }
        staged_tables = [
            table for table in _RELATIONAL_TABLES if table in existing_tables
        ]
        legacy_sequences = self._read_autoincrement_sequences(conn, existing_tables)
        for table in staged_tables:
            staged = _staged_table_name(table)
            conn.execute(
                f'CREATE TEMP TABLE "{staged}" AS SELECT * FROM main."{table}"'
            )

        for table in _DROP_ORDER:
            conn.execute(f'DROP TABLE IF EXISTS main."{table}"')

        execute_sql_script(conn, SCHEMA_V1_SQL)

        copy_tables = set(staged_tables)
        if not self._active_task_snapshots_are_copyable(conn, copy_tables):
            copy_tables.difference_update(_ACTIVE_TASK_TABLES)
        for table in _RELATIONAL_TABLES:
            if table in copy_tables:
                self._copy_staged_table(conn, table)
        self._restore_autoincrement_sequences(conn, legacy_sequences)

        for table in reversed(staged_tables):
            conn.execute(f'DROP TABLE temp."{_staged_table_name(table)}"')

    @staticmethod
    def _read_autoincrement_sequences(
        conn: MigrationConnection,
        existing_tables: set[str],
    ) -> dict[str, int]:
        if "sqlite_sequence" not in existing_tables:
            return {}
        placeholders = ", ".join("?" for _ in _AUTOINCREMENT_TABLES)
        return {
            row["name"]: int(row["seq"] or 0)
            for row in conn.execute(
                f"SELECT name, seq FROM main.sqlite_sequence "
                f"WHERE name IN ({placeholders})",
                _AUTOINCREMENT_TABLES,
            )
        }

    @staticmethod
    def _restore_autoincrement_sequences(
        conn: MigrationConnection,
        legacy_sequences: dict[str, int],
    ) -> None:
        for table, legacy_sequence in legacy_sequences.items():
            row = conn.execute(
                "SELECT seq FROM main.sqlite_sequence WHERE name = ?",
                (table,),
            ).fetchone()
            current_sequence = int(row["seq"] or 0) if row is not None else 0
            restored_sequence = max(current_sequence, legacy_sequence)
            if row is None:
                conn.execute(
                    "INSERT INTO main.sqlite_sequence (name, seq) VALUES (?, ?)",
                    (table, restored_sequence),
                )
            else:
                conn.execute(
                    "UPDATE main.sqlite_sequence SET seq = ? WHERE name = ?",
                    (restored_sequence, table),
                )

    def _active_task_snapshots_are_copyable(
        self,
        conn: MigrationConnection,
        staged_tables: set[str],
    ) -> bool:
        if not _ACTIVE_TASK_TABLES <= staged_tables:
            return False
        for table, required_columns in _ACTIVE_TASK_REQUIRED_COLUMNS.items():
            columns = set(
                self._table_columns(conn, "temp", _staged_table_name(table))
            )
            if not required_columns <= columns:
                return False
        return True

    def _copy_staged_table(
        self,
        conn: MigrationConnection,
        table: str,
    ) -> None:
        staged = _staged_table_name(table)
        source_columns = set(self._table_columns(conn, "temp", staged))
        target_columns = self._table_columns(conn, "main", table)
        insert_columns: list[str] = []
        select_expressions: list[str] = []

        for column in target_columns:
            expression = self._legacy_column_expression(
                table,
                column,
                source_columns,
            )
            if expression is None:
                continue
            insert_columns.append(f'"{column}"')
            select_expressions.append(expression)

        conn.execute(
            f'INSERT INTO main."{table}" ({", ".join(insert_columns)}) '
            f'SELECT {", ".join(select_expressions)} FROM temp."{staged}"'
        )

    @staticmethod
    def _legacy_column_expression(
        table: str,
        column: str,
        source_columns: set[str],
    ) -> str | None:
        if table == "runs" and column == "created_by_run_id":
            candidates = [
                candidate
                for candidate in ("created_by_run_id", "parent_run_id")
                if candidate in source_columns
            ]
            if not candidates:
                return None
            quoted = [f'"{candidate}"' for candidate in candidates]
            if len(quoted) == 1:
                return quoted[0]
            return f'COALESCE({", ".join(quoted)})'
        if column in source_columns:
            return f'"{column}"'
        return None

    @staticmethod
    def _table_columns(
        conn: MigrationConnection,
        schema: str,
        table: str,
    ) -> list[str]:
        return [
            row["name"]
            for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')
        ]

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


_STAGE_PREFIX = "__chattree_v0_"

_AUTOINCREMENT_TABLES = (
    "run_events",
    "plan_events",
)

_RELATIONAL_TABLES = (
    "nodes",
    "messages",
    "runs",
    "run_events",
    "tool_calls",
    "tool_results",
    "plans",
    "plan_proposals",
    "plan_events",
    "active_tasks",
    "active_task_steps",
    "task_run_bindings",
    "task_notifications",
    "transcript_items",
)

_DROP_ORDER = (
    "transcript_items",
    "task_notifications",
    "task_run_bindings",
    "active_task_steps",
    "active_tasks",
    "task_events",
    "task_steps",
    "tasks",
    "plan_proposals",
    "plan_events",
    "tool_results",
    "tool_calls",
    "run_events",
    "plans",
    "runs",
    "messages",
    "nodes",
)

_ACTIVE_TASK_TABLES = {
    "active_tasks",
    "active_task_steps",
    "task_run_bindings",
}

_ACTIVE_TASK_REQUIRED_COLUMNS = {
    "active_tasks": {
        "conversation_id",
        "generation_id",
        "title",
        "created_at",
        "updated_at",
    },
    "active_task_steps": {
        "conversation_id",
        "position",
        "title",
        "status",
        "updated_at",
    },
    "task_run_bindings": {
        "run_id",
        "conversation_id",
        "task_generation_id",
        "step_position",
        "base_revision",
        "created_at",
    },
}


def _staged_table_name(table: str) -> str:
    return f"{_STAGE_PREFIX}{table}"
