from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .home import resolve_chattree_home
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
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != CURRENT_SCHEMA_VERSION:
                self._reset_current_schema(conn)
            self._apply_storage_pragmas(conn)
        finally:
            conn.close()

    @staticmethod
    def _reset_current_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = OFF")
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
        ).fetchall()
        for row in rows:
            conn.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        conn.execute("PRAGMA foreign_keys = ON")

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
