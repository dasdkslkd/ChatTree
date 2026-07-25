import sqlite3
from pathlib import Path

import pytest

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.schema import CURRENT_SCHEMA_VERSION


REQUIRED_TABLES = {
    "server_metadata",
    "blobs",
    "conversations",
    "nodes",
    "messages",
    "tool_calls",
    "tool_results",
    "runs",
    "run_events",
    "plans",
    "active_tasks",
    "active_task_steps",
    "task_run_bindings",
    "task_notifications",
}


def test_initialize_creates_database_tables(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        binding_fks = conn.execute("PRAGMA foreign_key_list(task_run_bindings)").fetchall()
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert REQUIRED_TABLES <= names
    assert {"tasks", "task_steps", "task_events"}.isdisjoint(names)
    assert {
        (row["table"], row["from"], row["to"], row["on_delete"])
        for row in binding_fks
    } >= {
        ("active_tasks", "conversation_id", "conversation_id", "CASCADE"),
        ("active_tasks", "task_generation_id", "generation_id", "CASCADE"),
    }
    assert (tmp_path / "chattree.sqlite").exists()
    assert schema_version == CURRENT_SCHEMA_VERSION


def test_fresh_runs_schema_has_canonical_idempotency_constraints(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        indexes = {
            row["name"]: (bool(row["unique"]), bool(row["partial"]))
            for row in conn.execute("PRAGMA index_list(runs)")
        }
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) "
            "VALUES ('conv-idem', 'Idempotency', 1, 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO runs (
                  id, conversation_id, kind, status, summary, event_count,
                  created_at, updated_at, idempotency_key
                ) VALUES ('run-invalid', 'conv-idem', 'chat', 'running', '', 0,
                          1, 1, 'op_invalid')
                """
            )

    assert {"idempotency_key", "request_fingerprint"} <= columns
    assert indexes["idx_runs_idempotency_key"] == (True, True)
    assert "CHECK" in table_sql.upper()
    assert "idempotency_key IS NULL" in table_sql
    assert "request_fingerprint IS NULL" in table_sql


def test_task_notifications_status_is_canonical_enum(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'task_notifications'"
        ).fetchone()[0]

    for status in (
        "unbound",
        "bound",
        "delivering",
        "delivered",
        "delivery_failed",
        "delivery_cancelled",
    ):
        assert status in table_sql


def test_initialize_applies_wal_and_foreign_keys(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with sqlite3.connect(persistence.db_path) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    with persistence.connect() as conn:
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]

    assert journal.lower() == "wal"
    assert foreign_keys == 1


def test_initialize_replaces_obsolete_task_schema(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_root_node(conn, "conversation-a", "node-a")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE task_steps (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 0")

    persistence.initialize()

    with persistence.connect() as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {
        "tasks",
        "task_steps",
        "task_events",
    }.isdisjoint(names)


def _insert_conversation(conn, conversation_id: str):
    conn.execute(
        """
        INSERT INTO conversations (id, title, created_at, updated_at)
        VALUES (?, ?, 1, 1)
        """,
        (conversation_id, conversation_id),
    )


def _insert_root_node(conn, conversation_id: str, node_id: str):
    conn.execute(
        """
        INSERT INTO nodes (id, conversation_id, created_at, updated_at)
        VALUES (?, ?, 1, 1)
        """,
        (node_id, conversation_id),
    )


def test_nodes_parent_id_rejects_cross_conversation_reference(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_conversation(conn, "conversation-b")
        conn.execute(
            """
            INSERT INTO nodes (id, conversation_id, created_at, updated_at)
            VALUES ('parent-a', 'conversation-a', 1, 1)
            """
        )

        try:
            conn.execute(
                """
                INSERT INTO nodes (
                  id, conversation_id, parent_id, created_at, updated_at
                )
                VALUES ('child-b', 'conversation-b', 'parent-a', 1, 1)
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("cross-conversation parent_id was accepted")


def test_nodes_rejects_second_root_in_same_conversation(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        conn.execute(
            """
            INSERT INTO nodes (id, conversation_id, created_at, updated_at)
            VALUES ('root-a', 'conversation-a', 1, 1)
            """
        )

        try:
            conn.execute(
                """
                INSERT INTO nodes (id, conversation_id, created_at, updated_at)
                VALUES ('root-b', 'conversation-a', 1, 1)
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("second root node in conversation was accepted")


def test_message_node_id_rejects_cross_conversation_reference(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_conversation(conn, "conversation-b")
        _insert_root_node(conn, "conversation-a", "node-a")
        conn.execute(
            """
            INSERT INTO messages (id, conversation_id, node_id, role, created_at)
            VALUES ('message-a', 'conversation-a', 'node-a', 'assistant', 1)
            """
        )

        try:
            conn.execute(
                """
                INSERT INTO messages (id, conversation_id, node_id, role, created_at)
                VALUES ('message-b', 'conversation-b', 'node-a', 'assistant', 1)
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("cross-conversation message node_id was accepted")


def test_tool_call_ids_are_scoped_to_conversation(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_conversation(conn, "conversation-b")
        _insert_root_node(conn, "conversation-a", "node-a")
        _insert_root_node(conn, "conversation-b", "node-b")

        conn.execute(
            """
            INSERT INTO tool_calls (
              id,
              conversation_id,
              node_id,
              call_index,
              name,
              status,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 1)
            """,
            ("call_0", "conversation-a", "node-a", 0, "shell", "complete"),
        )
        conn.execute(
            """
            INSERT INTO tool_calls (
              id,
              conversation_id,
              node_id,
              call_index,
              name,
              status,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 1)
            """,
            ("call_0", "conversation-b", "node-b", 0, "python", "complete"),
        )

        try:
            conn.execute(
                """
                INSERT INTO tool_calls (
                  id,
                  conversation_id,
                  node_id,
                  call_index,
                  name,
                  status,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, 1)
                """,
                ("call_0", "conversation-a", "node-a", 1, "duplicate", "complete"),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate tool_call id in conversation was accepted")


def test_tool_result_tool_call_fk_is_scoped_to_conversation(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_conversation(conn, "conversation-b")
        _insert_root_node(conn, "conversation-a", "node-a")
        _insert_root_node(conn, "conversation-b", "node-b")
        conn.execute(
            """
            INSERT INTO tool_calls (
              id,
              conversation_id,
              node_id,
              call_index,
              name,
              status,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 1)
            """,
            ("call_0", "conversation-a", "node-a", 0, "shell", "complete"),
        )

        try:
            conn.execute(
                """
                INSERT INTO tool_results (
                  id,
                  conversation_id,
                  node_id,
                  tool_call_id,
                  status,
                  output_preview,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "result-b",
                    "conversation-b",
                    "node-b",
                    "call_0",
                    "complete",
                    "wrong conversation",
                ),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("cross-conversation tool_call_id was accepted")

        conn.execute(
            """
            INSERT INTO tool_calls (
              id,
              conversation_id,
              node_id,
              call_index,
              name,
              status,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 1)
            """,
            ("call_0", "conversation-b", "node-b", 0, "python", "complete"),
        )
        conn.execute(
            """
            INSERT INTO tool_results (
              id,
              conversation_id,
              node_id,
              tool_call_id,
              status,
              output_preview,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                "result-b",
                "conversation-b",
                "node-b",
                "call_0",
                "complete",
                "right conversation",
            ),
        )


def test_tool_result_is_unique_per_tool_call(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_root_node(conn, "conversation-a", "node-a")
        conn.execute(
            """
            INSERT INTO tool_calls (
              id,
              conversation_id,
              node_id,
              call_index,
              name,
              status,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 1)
            """,
            ("call_0", "conversation-a", "node-a", 0, "shell", "complete"),
        )
        conn.execute(
            """
            INSERT INTO tool_results (
              id,
              conversation_id,
              node_id,
              tool_call_id,
              status,
              output_preview,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                "result-old",
                "conversation-a",
                "node-a",
                "call_0",
                "complete",
                "approved",
            ),
        )

        try:
            conn.execute(
                """
                INSERT INTO tool_results (
                  id,
                  conversation_id,
                  node_id,
                  tool_call_id,
                  status,
                  output_preview,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    "result-new",
                    "conversation-a",
                    "node-a",
                    "call_0",
                    "complete",
                    "rejected",
                ),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate result for one tool_call_id was accepted")


def test_tool_call_primary_key_is_scoped_by_conversation(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        tool_call_pk = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        assert tool_call_pk["conversation_id"] == 1
        assert tool_call_pk["id"] == 2

        _insert_conversation(conn, "conversation-a")
        _insert_conversation(conn, "conversation-b")
        _insert_root_node(conn, "conversation-a", "node-a")
        _insert_root_node(conn, "conversation-b", "node-b")
        for conversation_id, node_id, name in (
            ("conversation-a", "node-a", "shell"),
            ("conversation-b", "node-b", "python"),
        ):
            conn.execute(
                """
                INSERT INTO tool_calls (
                  id,
                  conversation_id,
                  node_id,
                  call_index,
                  name,
                  status,
                  created_at,
                  updated_at
                )
                VALUES ('call_0', ?, ?, 0, ?, 'complete', 1, 1)
                """,
                (conversation_id, node_id, name),
            )
            conn.execute(
                """
                INSERT INTO tool_results (
                  id,
                  conversation_id,
                  node_id,
                  tool_call_id,
                  status,
                  output_preview,
                  created_at
                )
                VALUES (?, ?, ?, 'call_0', 'complete', ?, 1)
                """,
                (
                    f"result-{conversation_id}",
                    conversation_id,
                    node_id,
                    f"result for {conversation_id}",
                ),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO tool_results (
                  id,
                  conversation_id,
                  node_id,
                  tool_call_id,
                  status,
                  output_preview,
                  created_at
                )
                VALUES (
                  'result-cross',
                  'conversation-b',
                  'node-b',
                  'missing-call',
                  'complete',
                  'wrong conversation',
                  1
                )
                """
            )

        conn.execute(
            """
            DELETE FROM tool_calls
            WHERE conversation_id = ? AND id = ?
            """,
            ("conversation-b", "call_0"),
        )
        remaining = conn.execute(
            """
            SELECT conversation_id
            FROM tool_results
            WHERE tool_call_id = 'call_0'
            ORDER BY conversation_id
            """
        ).fetchall()

    assert [row["conversation_id"] for row in remaining] == ["conversation-a"]
