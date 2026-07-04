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
