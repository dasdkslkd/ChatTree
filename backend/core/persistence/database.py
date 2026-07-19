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
from .schema import CURRENT_SCHEMA_VERSION, SCHEMA_SQL, SCHEMA_V1_SQL


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
                    Migration(1, 2, self._migrate_1_to_2),
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
        schema_sql = (
            SCHEMA_V1_SQL
            if existing_tables & _APPLICATION_TABLES
            else SCHEMA_SQL
        )
        legacy_sequences = self._read_autoincrement_sequences(conn, existing_tables)
        for table in staged_tables:
            staged = _staged_table_name(table)
            conn.execute(
                f'CREATE TEMP TABLE "{staged}" AS SELECT * FROM main."{table}"'
            )

        for table in _DROP_ORDER:
            conn.execute(f'DROP TABLE IF EXISTS main."{table}"')

        execute_sql_script(conn, schema_sql)

        copy_tables = set(staged_tables)
        if not self._active_task_snapshots_are_copyable(conn, copy_tables):
            copy_tables.difference_update(_ACTIVE_TASK_TABLES)
        for table in _RELATIONAL_TABLES:
            if table in copy_tables:
                self._copy_staged_table(conn, table)
        self._restore_autoincrement_sequences(conn, legacy_sequences)

        for table in reversed(staged_tables):
            conn.execute(f'DROP TABLE temp."{_staged_table_name(table)}"')

    def _migrate_1_to_2(self, conn: MigrationConnection) -> None:
        columns = set(self._table_columns(conn, "main", "runs"))
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN idempotency_key TEXT")
        if "request_fingerprint" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN request_fingerprint TEXT")

        mismatched = conn.execute(
            """
            SELECT id
            FROM runs
            WHERE (idempotency_key IS NULL) <> (request_fingerprint IS NULL)
            LIMIT 1
            """
        ).fetchone()
        if mismatched is not None:
            raise RuntimeError(
                "runs idempotency pair mismatch in existing row "
                f"{mismatched['id']}"
            )

        table_row = conn.execute(
            "SELECT sql FROM main.sqlite_master "
            "WHERE type = 'table' AND name = 'runs'"
        ).fetchone()
        if table_row is None:
            raise RuntimeError("runs table is missing from schema version 1")
        table_sql = str(table_row["sql"] or "")
        normalized_table_sql = _normalize_schema_sql(table_sql)
        has_pair_check = (
            _RUNS_IDEMPOTENCY_PAIR_CHECK_SQL in normalized_table_sql
        )
        execute_sql_script(
            conn,
            """
            DROP TRIGGER IF EXISTS runs_idempotency_pair_insert;
            DROP TRIGGER IF EXISTS runs_idempotency_pair_update;
            """,
        )
        if not has_pair_check:
            execute_sql_script(
                conn,
                """
                CREATE TRIGGER runs_idempotency_pair_insert
                BEFORE INSERT ON runs
                WHEN (NEW.idempotency_key IS NULL) <>
                     (NEW.request_fingerprint IS NULL)
                BEGIN
                  SELECT RAISE(ABORT, 'runs idempotency pair mismatch');
                END;
                CREATE TRIGGER runs_idempotency_pair_update
                BEFORE UPDATE OF idempotency_key, request_fingerprint ON runs
                WHEN (NEW.idempotency_key IS NULL) <>
                     (NEW.request_fingerprint IS NULL)
                BEGIN
                  SELECT RAISE(ABORT, 'runs idempotency pair mismatch');
                END;
                """,
            )

        execute_sql_script(
            conn,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency_key
              ON runs(idempotency_key)
              WHERE idempotency_key IS NOT NULL;
            """,
        )
        self._validate_run_idempotency_index(conn)

    @staticmethod
    def _validate_run_idempotency_index(conn: MigrationConnection) -> None:
        index_row = next(
            (
                row
                for row in conn.execute("PRAGMA main.index_list('runs')")
                if row["name"] == "idx_runs_idempotency_key"
            ),
            None,
        )
        index_columns = [
            row["name"]
            for row in conn.execute(
                "PRAGMA main.index_info('idx_runs_idempotency_key')"
            )
        ]
        index_sql_row = conn.execute(
            "SELECT sql FROM main.sqlite_master "
            "WHERE type = 'index' AND name = 'idx_runs_idempotency_key'"
        ).fetchone()
        index_sql = str(index_sql_row["sql"] or "") if index_sql_row else ""
        normalized_index_sql = _normalize_schema_sql(index_sql)
        if (
            index_row is None
            or not bool(index_row["unique"])
            or not bool(index_row["partial"])
            or index_columns != ["idempotency_key"]
            or normalized_index_sql != _RUNS_IDEMPOTENCY_INDEX_SQL
        ):
            raise RuntimeError(
                "idx_runs_idempotency_key must be a unique partial index "
                "on runs(idempotency_key)"
            )

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

_RUNS_IDEMPOTENCY_PAIR_CHECK_SQL = (
    "check ( (idempotency_key is null and request_fingerprint is null) "
    "or (idempotency_key is not null and request_fingerprint is not null) )"
)
_RUNS_IDEMPOTENCY_INDEX_SQL = (
    "create unique index idx_runs_idempotency_key "
    "on runs(idempotency_key) where idempotency_key is not null"
)

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

_APPLICATION_TABLES = {
    "server_metadata",
    "blobs",
    "conversations",
    "tasks",
    "task_steps",
    "task_events",
    *_RELATIONAL_TABLES,
}

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


def _normalize_schema_sql(sql: str) -> str:
    tokens: list[str] = []
    position = 0
    while position < len(sql):
        if sql.startswith("--", position):
            newline = sql.find("\n", position + 2)
            position = len(sql) if newline < 0 else newline + 1
            tokens.append(" ")
            continue
        if sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            position = len(sql) if end < 0 else end + 2
            tokens.append(" ")
            continue

        character = sql[position]
        if character in {"'", '"', "`"}:
            quote = character
            position += 1
            while position < len(sql):
                if sql[position] != quote:
                    position += 1
                    continue
                if position + 1 < len(sql) and sql[position + 1] == quote:
                    position += 2
                    continue
                position += 1
                break
            tokens.append(" ")
            continue
        if character == "[":
            position += 1
            while position < len(sql):
                if sql[position] != "]":
                    position += 1
                    continue
                if position + 1 < len(sql) and sql[position + 1] == "]":
                    position += 2
                    continue
                position += 1
                break
            tokens.append(" ")
            continue

        tokens.append(character.lower())
        position += 1

    return " ".join("".join(tokens).split())
