import sqlite3
from pathlib import Path

from backend.core.persistence.database import SQLitePersistence


REQUIRED_TABLES = {
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
    "tasks",
    "task_events",
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

    assert REQUIRED_TABLES <= names
    assert (tmp_path / "chattree.sqlite").exists()


def test_initialize_applies_wal_and_foreign_keys(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with persistence.connect() as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]

    assert journal.lower() == "wal"
    assert foreign_keys == 1


def _insert_conversation(conn, conversation_id: str):
    conn.execute(
        """
        INSERT INTO conversations (id, title, created_at, updated_at)
        VALUES (?, ?, 1, 1)
        """,
        (conversation_id, conversation_id),
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
