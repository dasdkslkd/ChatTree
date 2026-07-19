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
    "plan_events",
    "active_tasks",
    "active_task_steps",
    "task_run_bindings",
    "task_notifications",
    "transcript_items",
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


def test_initialize_applies_wal_and_foreign_keys(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with sqlite3.connect(persistence.db_path) as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    with persistence.connect() as conn:
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]

    assert journal.lower() == "wal"
    assert foreign_keys == 1


def test_initialize_replaces_obsolete_task_schema_without_losing_transcript(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_root_node(conn, "conversation-a", "node-a")
        conn.execute(
            """
            INSERT INTO transcript_items (
              id, conversation_id, node_id, item_type, local_order,
              visibility, summary, preview, created_at, updated_at
            ) VALUES ('item-a', 'conversation-a', 'node-a', 'assistant_answer', 1,
                      'main', 'kept', 'kept', 1, 1)
            """
        )
        conn.execute("ALTER TABLE task_notifications ADD COLUMN task_id TEXT")
        conn.execute("ALTER TABLE transcript_items ADD COLUMN task_id TEXT")
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
        notification_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_notifications)")
        }
        transcript_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(transcript_items)")
        }
        kept = conn.execute(
            "SELECT preview FROM transcript_items WHERE id = 'item-a'"
        ).fetchone()

    assert {"tasks", "task_steps", "task_events"}.isdisjoint(names)
    assert "task_id" not in notification_columns
    assert "task_id" not in transcript_columns
    assert kept["preview"] == "kept"


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


def test_transcript_message_id_rejects_cross_conversation_reference(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        _insert_conversation(conn, "conversation-a")
        _insert_conversation(conn, "conversation-b")
        conn.execute(
            """
            INSERT INTO messages (id, conversation_id, role, created_at)
            VALUES ('message-a', 'conversation-a', 'assistant', 1)
            """
        )

        try:
            conn.execute(
                """
                INSERT INTO transcript_items (
                  id,
                  conversation_id,
                  message_id,
                  item_type,
                  local_order,
                  created_at,
                  updated_at
                )
                VALUES (
                  'transcript-b',
                  'conversation-b',
                  'message-a',
                  'message',
                  1,
                  1,
                  1
                )
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("cross-conversation message_id was accepted")


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


def _create_legacy_tool_call_schema(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
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

            CREATE TABLE nodes (
              id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
              parent_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
              child_order INTEGER NOT NULL DEFAULT 0,
              depth INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'complete',
              model_id TEXT,
              provider_id TEXT,
              tool_permission_mode TEXT,
              turn_usage_json TEXT,
              branch_usage_json TEXT,
              active_context_usage_json TEXT,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              UNIQUE(conversation_id, id),
              FOREIGN KEY (conversation_id, parent_id) REFERENCES nodes(conversation_id, id)
            );

            CREATE TABLE blobs (
              id TEXT PRIMARY KEY,
              path TEXT NOT NULL,
              mime_type TEXT NOT NULL DEFAULT 'text/plain; charset=utf-8',
              compression TEXT NOT NULL DEFAULT 'zstd',
              byte_size INTEGER NOT NULL,
              stored_size INTEGER NOT NULL,
              char_count INTEGER,
              ref_count INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              last_accessed_at INTEGER
            );

            CREATE TABLE messages (
              id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
              node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
              role TEXT NOT NULL,
              subtype TEXT,
              name TEXT,
              content_inline TEXT,
              content_blob_id TEXT REFERENCES blobs(id),
              preview TEXT NOT NULL DEFAULT '',
              hidden INTEGER NOT NULL DEFAULT 0,
              transcript_only INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT,
              usage_json TEXT,
              created_at INTEGER NOT NULL,
              UNIQUE(conversation_id, id),
              FOREIGN KEY (conversation_id, node_id) REFERENCES nodes(conversation_id, id)
            );

            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              status TEXT NOT NULL,
              created_by_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
              cancellation_parent_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
              anchor_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
              target_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
              summary TEXT NOT NULL DEFAULT '',
              metadata_json TEXT,
              event_count INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              finished_at INTEGER,
              UNIQUE(conversation_id, id),
              FOREIGN KEY (conversation_id, created_by_run_id) REFERENCES runs(conversation_id, id),
              FOREIGN KEY (conversation_id, cancellation_parent_run_id) REFERENCES runs(conversation_id, id),
              FOREIGN KEY (conversation_id, anchor_node_id) REFERENCES nodes(conversation_id, id),
              FOREIGN KEY (conversation_id, target_node_id) REFERENCES nodes(conversation_id, id)
            );

            CREATE TABLE tool_calls (
              id TEXT PRIMARY KEY,
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
              UNIQUE(conversation_id, id),
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
              tool_call_id TEXT REFERENCES tool_calls(id) ON DELETE CASCADE,
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
            );

            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES ('legacy-conversation', 'Legacy', 1, 1);
            INSERT INTO nodes (id, conversation_id, created_at, updated_at)
            VALUES ('legacy-node', 'legacy-conversation', 1, 1);
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
            VALUES (
              'legacy-call',
              'legacy-conversation',
              'legacy-node',
              0,
              'legacy_tool',
              'complete',
              1,
              1
            );
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
              'legacy-result',
              'legacy-conversation',
              'legacy-node',
              'legacy-call',
              'complete',
              'legacy output',
              1
            );
            """
        )


def _create_legacy_run_lifecycle_schema(db_path: Path):
    # Frozen table definitions from 1f37f9b^, before parent_run_id was split.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
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

            CREATE TABLE nodes (
              id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
              parent_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
              child_order INTEGER NOT NULL DEFAULT 0,
              depth INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'complete',
              model_id TEXT,
              provider_id TEXT,
              tool_permission_mode TEXT,
              turn_usage_json TEXT,
              branch_usage_json TEXT,
              active_context_usage_json TEXT,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              UNIQUE(conversation_id, id),
              FOREIGN KEY (conversation_id, parent_id) REFERENCES nodes(conversation_id, id)
            );

            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              status TEXT NOT NULL,
              parent_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
              anchor_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
              target_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
              summary TEXT NOT NULL DEFAULT '',
              metadata_json TEXT,
              event_count INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              finished_at INTEGER,
              UNIQUE(conversation_id, id),
              FOREIGN KEY (conversation_id, parent_run_id) REFERENCES runs(conversation_id, id),
              FOREIGN KEY (conversation_id, anchor_node_id) REFERENCES nodes(conversation_id, id),
              FOREIGN KEY (conversation_id, target_node_id) REFERENCES nodes(conversation_id, id)
            );

            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES ('legacy-conversation', 'Legacy', 1, 1);
            INSERT INTO nodes (id, conversation_id, created_at, updated_at)
            VALUES ('legacy-node', 'legacy-conversation', 1, 1);
            INSERT INTO runs (
              id,
              conversation_id,
              kind,
              status,
              parent_run_id,
              anchor_node_id,
              summary,
              created_at,
              updated_at
            )
            VALUES
              (
                'parent-run',
                'legacy-conversation',
                'chat',
                'completed',
                NULL,
                'legacy-node',
                'parent run',
                1,
                1
              ),
              (
                'legacy-run',
                'legacy-conversation',
                'agent',
                'completed',
                'parent-run',
                'legacy-node',
                'legacy run',
                2,
                2
              );
            """
        )


def test_initialize_migrates_legacy_run_lifecycle_before_indexes(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_legacy_run_lifecycle_schema(persistence.db_path)

    persistence.initialize()

    with persistence.connect() as conn:
        run_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "created_by_run_id" in run_columns
        assert "cancellation_parent_run_id" in run_columns

        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(runs)").fetchall()
        }
        assert "idx_runs_created_by" in indexes
        assert "idx_runs_cancellation_parent" in indexes

        legacy = conn.execute(
            """
            SELECT id, created_by_run_id, cancellation_parent_run_id
            FROM runs
            WHERE id = 'legacy-run'
            """
        ).fetchone()
        assert legacy["created_by_run_id"] == "parent-run"
        assert legacy["cancellation_parent_run_id"] is None


def test_initialize_migrates_legacy_global_tool_call_primary_key(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    _create_legacy_tool_call_schema(persistence.db_path)

    persistence.initialize()

    with persistence.connect() as conn:
        tool_call_pk = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        assert tool_call_pk["conversation_id"] == 1
        assert tool_call_pk["id"] == 2

        legacy_result = conn.execute(
            """
            SELECT output_preview
            FROM tool_results
            WHERE conversation_id = ? AND tool_call_id = ?
            """,
            ("legacy-conversation", "legacy-call"),
        ).fetchone()
        assert legacy_result["output_preview"] == "legacy output"

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
                  'legacy-call',
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
