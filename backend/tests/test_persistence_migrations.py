import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.migrations import (
    Migration,
    MigrationPathError,
    MigrationTransactionError,
    SchemaMigrationRunner,
    UnsupportedSchemaVersionError,
    execute_sql_script,
)
from backend.core.persistence.schema import CURRENT_SCHEMA_VERSION, SCHEMA_V1_SQL
from backend.tests.persistence_legacy_schema_fixtures import C1_INITIAL_SCHEMA_SQL


def _user_version(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _backup_files(home: Path) -> list[Path]:
    backup_dir = home / "backups"
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("*.sqlite"))


def _create_unversioned_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_V1_SQL)
        conn.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES ('sentinel', 'Sentinel', 1, 1)
            """
        )
        conn.execute(
            """
            INSERT INTO runs (
              id, conversation_id, kind, status, summary, event_count,
              created_at, updated_at
            ) VALUES ('run-v1', 'sentinel', 'chat', 'completed', 'kept', 0, 1, 1)
            """
        )


def _create_v1_run_database(
    db_path: Path,
    *,
    extra_columns: tuple[str, ...] = (),
    pair_check: str | None = None,
    pair_check_is_comment: bool = False,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_V1_SQL
    if pair_check is not None:
        columns_tail = """  finished_at INTEGER,
  UNIQUE(conversation_id, id),"""
        constrained_columns_tail = f"""  finished_at INTEGER,
  idempotency_key TEXT,
  request_fingerprint TEXT,
  UNIQUE(conversation_id, id),"""
        runs_tail = """  FOREIGN KEY (conversation_id, target_node_id)
    REFERENCES nodes(conversation_id, id)
);"""
        if pair_check_is_comment:
            pair_constraint = f"  /* CHECK ( {pair_check} ) */"
        else:
            pair_constraint = f"  CHECK ({pair_check})"
        constrained_runs_tail = f"""  FOREIGN KEY (conversation_id, target_node_id)
    REFERENCES nodes(conversation_id, id){',' if not pair_check_is_comment else ''}
{pair_constraint}
);"""
        assert schema_sql.count(columns_tail) == 1
        assert schema_sql.count(runs_tail) == 1
        schema_sql = schema_sql.replace(
            columns_tail,
            constrained_columns_tail,
        ).replace(
            runs_tail,
            constrained_runs_tail,
        )
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema_sql)
        for column in extra_columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) "
            "VALUES ('conv-v1', 'V1', 1, 1)"
        )
        conn.execute(
            """
            INSERT INTO runs (
              id, conversation_id, kind, status, summary, event_count,
              created_at, updated_at
            ) VALUES ('run-v1', 'conv-v1', 'chat', 'completed', 'kept', 0, 1, 1)
            """
        )


def _create_unversioned_conversations_only_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              root_node_id TEXT,
              current_node_id TEXT,
              project_id TEXT,
              provider_id TEXT,
              model_id TEXT,
              reasoning_effort TEXT,
              thinking_enabled INTEGER,
              multi_agent_mode TEXT NOT NULL DEFAULT 'explicit_request_only',
              workspace_json TEXT,
              settings_json TEXT,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES ('root-only', 'Root only', 1, 1);
            """
        )


def _run_idempotency_schema_state(
    conn: sqlite3.Connection,
) -> tuple[set[str], dict[str, tuple[bool, bool]], set[str]]:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    indexes = {
        row["name"]: (bool(row["unique"]), bool(row["partial"]))
        for row in conn.execute("PRAGMA index_list(runs)")
    }
    triggers = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'runs'"
        )
    }
    return columns, indexes, triggers


def _create_c1_initial_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(C1_INITIAL_SCHEMA_SQL)
        conn.executescript(
            """
            INSERT INTO conversations (
              id, title, root_node_id, current_node_id, created_at, updated_at
            ) VALUES ('legacy-conversation', 'Legacy', 'root-node', 'child-node', 1, 1);

            INSERT INTO nodes (
              id, conversation_id, parent_id, child_order, depth,
              status, created_at, updated_at
            ) VALUES
              ('root-node', 'legacy-conversation', NULL, 0, 0, 'complete', 1, 1),
              ('child-node', 'legacy-conversation', 'root-node', 0, 1, 'complete', 2, 2);

            INSERT INTO messages (
              id, conversation_id, node_id, role, content_inline, preview, created_at
            ) VALUES (
              'legacy-message', 'legacy-conversation', 'child-node',
              'assistant', 'kept message', 'kept message', 3
            );

            INSERT INTO runs (
              id, conversation_id, kind, status, parent_run_id, anchor_node_id,
              target_node_id, summary, event_count, created_at, updated_at
            ) VALUES
              (
                'parent-run', 'legacy-conversation', 'chat', 'completed', NULL,
                'root-node', 'root-node', 'parent', 0, 4, 4
              ),
              (
                'child-run', 'legacy-conversation', 'agent', 'completed', 'parent-run',
                'root-node', 'child-node', 'child', 1, 5, 5
              );

            INSERT INTO run_events (
              id, run_id, conversation_id, event_index, event_type,
              payload_inline, created_at
            ) VALUES (
              1, 'child-run', 'legacy-conversation', 0, 'completed', 'kept event', 6
            );

            INSERT INTO tool_calls (
              id, conversation_id, node_id, run_id, assistant_message_id,
              call_index, name, args_inline, status, created_at, updated_at
            ) VALUES (
              'legacy-call', 'legacy-conversation', 'child-node', 'child-run',
              'legacy-message', 0, 'read', '{}', 'complete', 7, 7
            );

            INSERT INTO tool_results (
              id, conversation_id, node_id, run_id, tool_call_id,
              status, output_preview, created_at
            ) VALUES (
              'legacy-result', 'legacy-conversation', 'child-node', 'child-run',
              'legacy-call', 'complete', 'kept result', 8
            );

            INSERT INTO plans (
              id, conversation_id, status, entered_node_id, submitted_node_id,
              entered_run_id, submitted_run_id, approved_run_id,
              previous_permission_mode, plan_inline, plan_preview, created_at, updated_at
            ) VALUES (
              'legacy-plan', 'legacy-conversation', 'approved', 'root-node', 'child-node',
              'parent-run', 'child-run', 'child-run', 'modify_only',
              'kept plan', 'kept plan', 9, 9
            );

            INSERT INTO plan_events (
              id, plan_id, conversation_id, event_type, payload_json, created_at
            ) VALUES (
              1, 'legacy-plan', 'legacy-conversation', 'approved', '{}', 10
            );

            INSERT INTO tasks (
              id, conversation_id, status, owner_type, title, created_at, updated_at
            ) VALUES (
              'obsolete-task', 'legacy-conversation', 'completed', 'main',
              'obsolete', 11, 11
            );

            INSERT INTO transcript_items (
              id, conversation_id, node_id, anchor_node_id, run_id, plan_id,
              task_id, message_id, item_type, local_order, summary, preview,
              created_at, updated_at
            ) VALUES (
              'legacy-transcript', 'legacy-conversation', 'child-node', 'root-node',
              'child-run', 'legacy-plan', 'obsolete-task', 'legacy-message',
              'assistant_answer', 1, 'kept transcript', 'kept transcript', 12, 12
            );
            """
        )


def _has_composite_foreign_key(
    conn: sqlite3.Connection,
    table: str,
    referenced_table: str,
    column_pairs: set[tuple[str, str]],
) -> bool:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        grouped.setdefault(row["id"], []).append(row)
    return any(
        rows[0]["table"] == referenced_table
        and {(row["from"], row["to"]) for row in rows} == column_pairs
        for rows in grouped.values()
    )


def test_empty_database_becomes_current_version_without_backup(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)

    persistence.initialize()

    assert _user_version(persistence.db_path) == CURRENT_SCHEMA_VERSION
    assert _backup_files(tmp_path) == []


def test_v1_run_migration_preserves_rows_and_enforces_pair_triggers(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(persistence.db_path)

    persistence.initialize()

    with persistence.connect() as conn:
        row = conn.execute(
            "SELECT summary, idempotency_key, request_fingerprint "
            "FROM runs WHERE id = 'run-v1'"
        ).fetchone()
        columns, indexes, trigger_names = _run_idempotency_schema_state(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE runs SET idempotency_key = 'op_half' WHERE id = 'run-v1'"
            )
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert row["summary"] == "kept"
    assert row["idempotency_key"] is None
    assert row["request_fingerprint"] is None
    assert {"idempotency_key", "request_fingerprint"} <= columns
    assert indexes["idx_runs_idempotency_key"] == (True, True)
    assert trigger_names == {
        "runs_idempotency_pair_insert",
        "runs_idempotency_pair_update",
    }
    assert _user_version(persistence.db_path) == 2


@pytest.mark.parametrize(
    "extra_columns",
    [
        ("idempotency_key",),
        ("request_fingerprint",),
        ("idempotency_key", "request_fingerprint"),
    ],
    ids=["key-only", "fingerprint-only", "both-without-invariant"],
)
def test_v1_partial_run_migration_repairs_each_missing_invariant(
    tmp_path: Path,
    extra_columns: tuple[str, ...],
):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(
        persistence.db_path,
        extra_columns=extra_columns,
    )

    persistence.initialize()

    with persistence.connect() as conn:
        columns, indexes, triggers = _run_idempotency_schema_state(conn)
        index_columns = [
            row["name"]
            for row in conn.execute("PRAGMA index_info(idx_runs_idempotency_key)")
        ]
        row = conn.execute(
            "SELECT idempotency_key, request_fingerprint "
            "FROM runs WHERE id = 'run-v1'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE runs SET request_fingerprint = 'sha-only' WHERE id = 'run-v1'"
            )

    assert {"idempotency_key", "request_fingerprint"} <= columns
    assert indexes["idx_runs_idempotency_key"] == (True, True)
    assert index_columns == ["idempotency_key"]
    assert triggers == {
        "runs_idempotency_pair_insert",
        "runs_idempotency_pair_update",
    }
    assert tuple(row) == (None, None)
    assert _user_version(persistence.db_path) == 2


def test_v1_run_migration_does_not_trust_non_equivalent_pair_check(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(
        persistence.db_path,
        pair_check=(
            "idempotency_key IS NULL OR request_fingerprint IS NULL"
        ),
    )

    persistence.initialize()

    with persistence.connect() as conn:
        _, _, triggers = _run_idempotency_schema_state(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE runs SET idempotency_key = 'op_half' WHERE id = 'run-v1'"
            )

    assert triggers == {
        "runs_idempotency_pair_insert",
        "runs_idempotency_pair_update",
    }
    assert _user_version(persistence.db_path) == 2


def test_v1_run_migration_does_not_trust_commented_pair_check(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(
        persistence.db_path,
        pair_check=(
            "(idempotency_key IS NULL AND request_fingerprint IS NULL) "
            "OR (idempotency_key IS NOT NULL "
            "AND request_fingerprint IS NOT NULL)"
        ),
        pair_check_is_comment=True,
    )

    persistence.initialize()

    with persistence.connect() as conn:
        _, _, triggers = _run_idempotency_schema_state(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE runs SET idempotency_key = 'op_half' WHERE id = 'run-v1'"
            )

    assert triggers == {
        "runs_idempotency_pair_insert",
        "runs_idempotency_pair_update",
    }
    assert _user_version(persistence.db_path) == 2


def test_v1_run_migration_replaces_trigger_impostors(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(
        persistence.db_path,
        extra_columns=("idempotency_key", "request_fingerprint"),
    )
    with sqlite3.connect(persistence.db_path) as conn:
        conn.executescript(
            """
            CREATE TRIGGER runs_idempotency_pair_insert
            BEFORE INSERT ON runs
            WHEN 0
            BEGIN
              SELECT RAISE(ABORT, 'never');
            END;
            CREATE TRIGGER runs_idempotency_pair_update
            BEFORE UPDATE OF idempotency_key, request_fingerprint ON runs
            WHEN 0
            BEGIN
              SELECT RAISE(ABORT, 'never');
            END;
            """
        )

    persistence.initialize()

    with persistence.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE runs SET request_fingerprint = 'sha-half' "
                "WHERE id = 'run-v1'"
            )

    assert _user_version(persistence.db_path) == 2


def test_v1_partial_run_migration_rolls_back_existing_pair_mismatch(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(
        persistence.db_path,
        extra_columns=("idempotency_key",),
    )
    with sqlite3.connect(persistence.db_path) as conn:
        conn.execute(
            "UPDATE runs SET idempotency_key = 'op-existing' WHERE id = 'run-v1'"
        )

    with pytest.raises(RuntimeError, match="pair mismatch"):
        persistence.initialize()

    with sqlite3.connect(persistence.db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        row = conn.execute(
            "SELECT idempotency_key FROM runs WHERE id = 'run-v1'"
        ).fetchone()

    assert columns >= {"idempotency_key"}
    assert "request_fingerprint" not in columns
    assert row[0] == "op-existing"
    assert _user_version(persistence.db_path) == 1


def test_v1_run_migration_rejects_non_unique_index_impostor(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(
        persistence.db_path,
        extra_columns=("idempotency_key", "request_fingerprint"),
    )
    with sqlite3.connect(persistence.db_path) as conn:
        conn.execute(
            "CREATE INDEX idx_runs_idempotency_key ON runs(idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )

    with pytest.raises(RuntimeError, match="unique"):
        persistence.initialize()

    with sqlite3.connect(persistence.db_path) as conn:
        index = next(
            row
            for row in conn.execute("PRAGMA index_list(runs)")
            if row[1] == "idx_runs_idempotency_key"
        )
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = 'runs'"
            )
        }

    assert index[2] == 0
    assert triggers == set()
    assert _user_version(persistence.db_path) == 1


@pytest.mark.parametrize(
    "index_tail",
    [
        (
            "ON runs(idempotency_key) "
            "WHERE idempotency_key IS NOT NULL AND status = 'running'"
        ),
        (
            "ON runs(idempotency_key COLLATE NOCASE) "
            "WHERE idempotency_key IS NOT NULL"
        ),
    ],
    ids=["extra-predicate", "non-canonical-collation"],
)
def test_v1_run_migration_rejects_unique_index_impostor_and_rolls_back(
    tmp_path: Path,
    index_tail: str,
):
    persistence = SQLitePersistence(tmp_path)
    _create_v1_run_database(
        persistence.db_path,
        extra_columns=("idempotency_key", "request_fingerprint"),
    )
    with sqlite3.connect(persistence.db_path) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX idx_runs_idempotency_key "
            f"{index_tail}"
        )

    with pytest.raises(RuntimeError, match="unique partial index"):
        persistence.initialize()

    with sqlite3.connect(persistence.db_path) as conn:
        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_runs_idempotency_key'"
        ).fetchone()[0]
        triggers = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'runs'"
        ).fetchall()

    assert index_tail in index_sql
    assert triggers == []
    assert _user_version(persistence.db_path) == 1


def test_unversioned_root_application_table_uses_legacy_schema_path(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_unversioned_conversations_only_database(persistence.db_path)

    persistence.initialize()

    with persistence.connect() as conn:
        title = conn.execute(
            "SELECT title FROM conversations WHERE id = 'root-only'"
        ).fetchone()[0]
        _, indexes, triggers = _run_idempotency_schema_state(conn)

    assert title == "Root only"
    assert indexes["idx_runs_idempotency_key"] == (True, True)
    assert triggers == {
        "runs_idempotency_pair_insert",
        "runs_idempotency_pair_update",
    }
    assert _user_version(persistence.db_path) == 2


def test_c1_initial_schema_is_rebuilt_with_data_and_scoped_constraints(
    tmp_path: Path,
):
    persistence = SQLitePersistence(tmp_path)
    _create_c1_initial_database(persistence.db_path)

    persistence.initialize()

    with persistence.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        child_run = conn.execute(
            """
            SELECT created_by_run_id, cancellation_parent_run_id
            FROM runs
            WHERE id = 'child-run'
            """
        ).fetchone()
        kept = {
            "message": conn.execute(
                "SELECT content_inline FROM messages WHERE id = 'legacy-message'"
            ).fetchone()[0],
            "event": conn.execute(
                "SELECT payload_inline FROM run_events WHERE id = 1"
            ).fetchone()[0],
            "tool": conn.execute(
                "SELECT output_preview FROM tool_results WHERE id = 'legacy-result'"
            ).fetchone()[0],
            "plan": conn.execute(
                "SELECT plan_inline FROM plans WHERE id = 'legacy-plan'"
            ).fetchone()[0],
            "plan_event": conn.execute(
                "SELECT event_type FROM plan_events WHERE id = 1"
            ).fetchone()[0],
            "transcript": conn.execute(
                "SELECT preview FROM transcript_items WHERE id = 'legacy-transcript'"
            ).fetchone()[0],
        }
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        expected_foreign_keys = (
            ("nodes", "nodes", {("conversation_id", "conversation_id"), ("parent_id", "id")}),
            ("messages", "nodes", {("conversation_id", "conversation_id"), ("node_id", "id")}),
            ("runs", "runs", {("conversation_id", "conversation_id"), ("created_by_run_id", "id")}),
            ("run_events", "runs", {("conversation_id", "conversation_id"), ("run_id", "id")}),
            ("tool_calls", "runs", {("conversation_id", "conversation_id"), ("run_id", "id")}),
            ("tool_results", "tool_calls", {("conversation_id", "conversation_id"), ("tool_call_id", "id")}),
            ("plans", "nodes", {("conversation_id", "conversation_id"), ("entered_node_id", "id")}),
            ("plan_events", "plans", {("conversation_id", "conversation_id"), ("plan_id", "id")}),
            ("transcript_items", "messages", {("conversation_id", "conversation_id"), ("message_id", "id")}),
        )
        for table, referenced_table, pairs in expected_foreign_keys:
            assert _has_composite_foreign_key(conn, table, referenced_table, pairs)

    assert child_run["created_by_run_id"] == "parent-run"
    assert child_run["cancellation_parent_run_id"] is None
    assert kept == {
        "message": "kept message",
        "event": "kept event",
        "tool": "kept result",
        "plan": "kept plan",
        "plan_event": "approved",
        "transcript": "kept transcript",
    }
    assert {"tasks", "task_steps", "task_events"}.isdisjoint(table_names)


def test_c1_migration_preserves_autoincrement_high_watermarks(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_c1_initial_database(persistence.db_path)
    with sqlite3.connect(persistence.db_path) as conn:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = 100 "
            "WHERE name IN ('run_events', 'plan_events')"
        )

    persistence.initialize()

    with persistence.connect() as conn:
        run_event_id = conn.execute(
            """
            INSERT INTO run_events (
              run_id, conversation_id, event_index, event_type, created_at
            ) VALUES ('child-run', 'legacy-conversation', 1, 'next', 20)
            """
        ).lastrowid
        plan_event_id = conn.execute(
            """
            INSERT INTO plan_events (
              plan_id, conversation_id, event_type, created_at
            ) VALUES ('legacy-plan', 'legacy-conversation', 'next', 20)
            """
        ).lastrowid

    assert run_event_id == 101
    assert plan_event_id == 101


def test_existing_unversioned_database_is_backed_up_and_preserved(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_unversioned_database(persistence.db_path)

    persistence.initialize()

    backups = _backup_files(tmp_path)
    assert len(backups) == 1
    assert _user_version(persistence.db_path) == CURRENT_SCHEMA_VERSION

    with sqlite3.connect(persistence.db_path) as conn:
        live_title = conn.execute(
            "SELECT title FROM conversations WHERE id = 'sentinel'"
        ).fetchone()[0]
    with sqlite3.connect(backups[0]) as conn:
        backup_version = conn.execute("PRAGMA user_version").fetchone()[0]
        backup_title = conn.execute(
            "SELECT title FROM conversations WHERE id = 'sentinel'"
        ).fetchone()[0]
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]

    assert live_title == "Sentinel"
    assert backup_version == 0
    assert backup_title == "Sentinel"
    assert quick_check == "ok"
    assert list((tmp_path / "backups").glob("*.tmp")) == []


def test_newer_schema_version_fails_closed_without_database_mutation(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(persistence.db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel (value) VALUES ('kept')")
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")

    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        persistence.initialize()

    with sqlite3.connect(persistence.db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        value = conn.execute("SELECT value FROM sentinel").fetchone()[0]

    assert version == CURRENT_SCHEMA_VERSION + 1
    assert names == {"sentinel"}
    assert value == "kept"
    assert _backup_files(tmp_path) == []


def test_migration_failure_rolls_back_schema_data_and_version(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel (value) VALUES ('before')")

    def first_migration(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE transient (id INTEGER PRIMARY KEY)")
        conn.execute("UPDATE sentinel SET value = 'during-first'")

    def fail_after_mutation(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE sentinel SET value = 'during-second'")
        raise RuntimeError("injected migration failure")

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=2,
        migrations=(
            Migration(0, 1, first_migration, destructive=True),
            Migration(1, 2, fail_after_mutation),
        ),
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(RuntimeError, match="injected migration failure"):
            runner.run(conn)
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        value = conn.execute("SELECT value FROM sentinel").fetchone()[0]

    assert version == 0
    assert names == {"sentinel"}
    assert value == "before"
    assert foreign_keys == 1
    assert len(_backup_files(tmp_path)) == 1


def test_repeated_initialize_does_not_migrate_or_back_up_again(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_unversioned_database(persistence.db_path)

    persistence.initialize()
    first_backups = _backup_files(tmp_path)
    persistence.initialize()

    assert len(first_backups) == 1
    assert _backup_files(tmp_path) == first_backups
    assert _user_version(persistence.db_path) == CURRENT_SCHEMA_VERSION
    with persistence.connect() as conn:
        run = conn.execute(
            "SELECT idempotency_key, request_fingerprint "
            "FROM runs WHERE id = 'run-v1'"
        ).fetchone()
    assert tuple(run) == (None, None)


def test_missing_adjacent_migration_path_fails_before_backup(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")

    applied = False

    def migration_with_missing_successor(conn: sqlite3.Connection) -> None:
        nonlocal applied
        applied = True
        conn.execute("DROP TABLE sentinel")

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=2,
        migrations=(
            Migration(0, 1, migration_with_missing_successor, destructive=True),
        ),
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(MigrationPathError, match="version 1"):
            runner.run(conn)

    assert _user_version(db_path) == 0
    assert applied is False
    assert _backup_files(tmp_path) == []


def test_migration_rejects_autocommit_connection_before_mutation(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL UNIQUE)")
        conn.execute("INSERT INTO sentinel (value) VALUES ('before')")

    callback_called = False

    def mutate_after_implicit_rollback(conn) -> None:
        nonlocal callback_called
        callback_called = True
        try:
            conn.execute("INSERT OR ROLLBACK INTO sentinel VALUES ('before')")
        except sqlite3.IntegrityError:
            pass
        conn.execute("UPDATE sentinel SET value = 'after'")

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, mutate_after_implicit_rollback, destructive=True),),
    )

    with sqlite3.connect(db_path, isolation_level=None) as conn:
        with pytest.raises(MigrationTransactionError, match="autocommit"):
            runner.run(conn)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "before"
    assert callback_called is False
    assert _backup_files(tmp_path) == []


def test_runner_holds_write_lock_before_starting_backup(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel (value) VALUES ('before')")

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, lambda conn: None, destructive=True),),
    )
    backup_started = threading.Event()
    continue_backup = threading.Event()
    original_create_backup = runner._create_backup

    def paused_backup(*args, **kwargs):
        backup_started.set()
        if not continue_backup.wait(timeout=5):
            raise TimeoutError("test did not release backup")
        return original_create_backup(*args, **kwargs)

    runner._create_backup = paused_backup
    errors: list[BaseException] = []

    def migrate() -> None:
        try:
            with sqlite3.connect(db_path) as conn:
                runner.run(conn)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=migrate)
    thread.start()
    assert backup_started.wait(timeout=5)
    competing_writer_was_blocked = False
    try:
        with sqlite3.connect(db_path, timeout=0.1) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc).lower()
                competing_writer_was_blocked = True
    finally:
        continue_backup.set()
        thread.join(timeout=5)

    assert competing_writer_was_blocked
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize(
    "escape",
    (
        "commit",
        "rollback",
        "execute_commit",
        "execute_prefixed_commit",
        "executescript",
    ),
)
def test_migration_callback_cannot_control_outer_transaction(
    tmp_path: Path,
    escape: str,
):
    db_path = tmp_path / f"{escape}.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel (value) VALUES ('before')")

    def illegal_migration(conn) -> None:
        conn.execute("UPDATE sentinel SET value = 'during'")
        if escape == "commit":
            conn.commit()
        elif escape == "rollback":
            conn.rollback()
        elif escape == "execute_commit":
            conn.execute("COMMIT")
        elif escape == "execute_prefixed_commit":
            conn.execute("; -- empty statement\n COMMIT")
        else:
            conn.executescript("COMMIT;")

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, illegal_migration),),
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(MigrationTransactionError):
            runner.run(conn)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "before"


def test_migration_cursor_iteration_does_not_expose_native_cursor(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"
    observed: dict[str, object] = {}

    def inspect_cursor(conn) -> None:
        cursor = conn.execute("SELECT 1")
        iterator = iter(cursor)
        observed["same_object"] = iterator is cursor
        observed["native_cursor"] = isinstance(iterator, sqlite3.Cursor)
        observed["row"] = next(iterator)[0]

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, inspect_cursor),),
    )
    with sqlite3.connect(db_path) as conn:
        runner.run(conn)

    assert observed == {
        "same_object": True,
        "native_cursor": False,
        "row": 1,
    }


@pytest.mark.parametrize(
    "escape_sql",
    (
        "\ufeff/* hidden */ COMMIT",
        'SAVEPOINT "migration_escape"',
        '/* hidden */ PRAGMA "user_version" = 99',
    ),
)
def test_sqlite_authorizer_blocks_raw_transaction_escape(
    tmp_path: Path,
    escape_sql: str,
):
    db_path = tmp_path / "chattree.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel (value) VALUES ('before')")

    def attempt_raw_escape(conn) -> None:
        conn.execute("UPDATE sentinel SET value = 'during'")
        raw_conn = object.__getattribute__(conn, "_MigrationConnection__conn")
        raw_conn.execute(escape_sql)

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, attempt_raw_escape),),
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(MigrationTransactionError, match="authorizer"):
            runner.run(conn)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "before"


def test_swallowed_insert_or_rollback_cannot_leak_followup_statements(
    tmp_path: Path,
):
    db_path = tmp_path / "chattree.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT NOT NULL UNIQUE)")
        conn.execute("INSERT INTO sentinel (value) VALUES ('before')")
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]

    def attempt_implicit_rollback(conn) -> None:
        try:
            conn.execute("INSERT OR ROLLBACK INTO sentinel VALUES ('before')")
        except sqlite3.IntegrityError:
            pass
        for statement in (
            "CREATE TABLE leaked (value TEXT)",
            "PRAGMA schema_version = 999",
        ):
            try:
                conn.execute(statement)
            except MigrationTransactionError:
                pass

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, attempt_implicit_rollback),),
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(MigrationTransactionError, match="outer transaction"):
            runner.run(conn)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_version
        assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "before"
        leaked = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'leaked'"
        ).fetchone()
        assert leaked is None


def test_sql_script_executes_multiple_statements_on_one_line(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"

    def create_tables(conn) -> None:
        execute_sql_script(
            conn,
            "CREATE TABLE first (id INTEGER); CREATE TABLE second (id INTEGER);",
        )

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, create_tables),),
    )
    with sqlite3.connect(db_path) as conn:
        runner.run(conn)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {"first", "second"} <= names


def test_sql_script_accepts_trailing_comments(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"

    def create_table(conn) -> None:
        execute_sql_script(
            conn,
            "CREATE TABLE kept (id INTEGER); -- trailing comment\n/* final */",
        )

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, create_table),),
    )
    with sqlite3.connect(db_path) as conn:
        runner.run(conn)
        kept = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kept'"
        ).fetchone()

    assert kept is not None


def test_wal_database_backup_includes_latest_committed_data(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with closing(sqlite3.connect(persistence.db_path)) as writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES ('wal-sentinel', 'In WAL', 1, 1)
            """
        )
        writer.execute("PRAGMA user_version = 0")
        writer.commit()
        assert persistence.db_path.with_name("chattree.sqlite-wal").exists()

        persistence.initialize()

    backups = _backup_files(tmp_path)
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        title = conn.execute(
            "SELECT title FROM conversations WHERE id = 'wal-sentinel'"
        ).fetchone()[0]
    assert title == "In WAL"


def test_failed_initialize_rolls_back_without_switching_to_wal(
    monkeypatch,
    tmp_path: Path,
):
    persistence = SQLitePersistence(tmp_path)
    _create_unversioned_database(persistence.db_path)
    with sqlite3.connect(persistence.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    def fail_migration(conn) -> None:
        conn.execute("CREATE TABLE transient (id INTEGER PRIMARY KEY)")
        raise RuntimeError("injected initialize failure")

    monkeypatch.setattr(persistence, "_migrate_0_to_1", fail_migration)

    with pytest.raises(RuntimeError, match="injected initialize failure"):
        persistence.initialize()

    with sqlite3.connect(persistence.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        transient = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transient'"
        ).fetchone()
    assert transient is None


def test_foreign_key_check_failure_rolls_back_migration(tmp_path: Path):
    db_path = tmp_path / "chattree.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (
              id INTEGER PRIMARY KEY,
              parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            """
        )

    def create_orphan(conn) -> None:
        conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")

    runner = SchemaMigrationRunner(
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        current_version=1,
        migrations=(Migration(0, 1, create_orphan),),
    )

    with sqlite3.connect(db_path) as conn:
        with pytest.raises(RuntimeError, match="foreign key violation"):
            runner.run(conn)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
