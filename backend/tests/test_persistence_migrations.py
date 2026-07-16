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
from backend.core.persistence.schema import CURRENT_SCHEMA_VERSION, SCHEMA_SQL


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
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES ('sentinel', 'Sentinel', 1, 1)
            """
        )


def test_empty_database_becomes_current_version_without_backup(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)

    persistence.initialize()

    assert _user_version(persistence.db_path) == CURRENT_SCHEMA_VERSION
    assert _backup_files(tmp_path) == []


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
