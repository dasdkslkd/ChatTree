from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from backend.core.home import resolve_chattree_home
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
            else:
                # 幂等补齐新增表（如 usage_stats）：不递增版本号、不清空数据。
                conn.executescript(SCHEMA_SQL)
            self._apply_storage_pragmas(conn)
        finally:
            conn.close()
        self.reclaim_blobs()

    @staticmethod
    def _reset_current_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = OFF")
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT IN ('sqlite_sequence', 'server_metadata')
            """
        ).fetchall()
        for row in rows:
            conn.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')
        conn.commit()
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
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

    def reclaim_blobs(self, *, compact: bool = False) -> int:
        """按真实外键引用回收 Blob；不维护容易漂移的引用计数。"""
        with self.connect() as conn:
            referenced = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT content_blob_id FROM messages WHERE content_blob_id IS NOT NULL
                    UNION
                    SELECT payload_blob_id FROM model_state_items WHERE payload_blob_id IS NOT NULL
                    UNION
                    SELECT args_blob_id FROM tool_calls WHERE args_blob_id IS NOT NULL
                    UNION
                    SELECT output_blob_id FROM tool_results WHERE output_blob_id IS NOT NULL
                    UNION
                    SELECT payload_blob_id FROM run_events WHERE payload_blob_id IS NOT NULL
                    UNION
                    SELECT detail_blob_id FROM active_tasks WHERE detail_blob_id IS NOT NULL
                    UNION
                    SELECT detail_blob_id FROM active_task_steps WHERE detail_blob_id IS NOT NULL
                    """
                ).fetchall()
            }
            rows = conn.execute("SELECT id, path FROM blobs").fetchall()
            stale = [row for row in rows if str(row["id"]) not in referenced]
            if stale:
                conn.executemany(
                    "DELETE FROM blobs WHERE id = ?",
                    [(str(row["id"]),) for row in stale],
                )

        tracked_paths = set()
        with self.connect() as conn:
            for row in conn.execute("SELECT path FROM blobs").fetchall():
                tracked_paths.add((self.home / str(row["path"])).resolve())
        if self.blobs_dir.exists():
            for path in self.blobs_dir.rglob("*.gz"):
                if path.resolve() not in tracked_paths:
                    path.unlink(missing_ok=True)
        for directory in sorted(
            (path for path in self.blobs_dir.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

        if compact:
            # 全量 VACUUM 真正回收散布的空闲页（incremental_vacuum 只能回收文件末尾的连续空闲页，实测无效）。
            # VACUUM 不能在事务内执行，须用独立连接，且要求无其他活跃写连接。
            vacuum_conn = sqlite3.connect(self.db_path)
            try:
                vacuum_conn.execute("VACUUM")
                vacuum_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                vacuum_conn.close()
        return len(stale)

    def stats(self) -> dict:
        """返回存储占用统计（字节）。freelist 为可回收的空闲页字节。"""
        db_bytes = 0
        page_size = freelist_pages = logical_pages = 0
        if self.db_path.exists():
            db_bytes = self.db_path.stat().st_size
            with self.connect() as conn:
                page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
                freelist_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
                logical_pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        return {
            "db_file_bytes": db_bytes,
            "logical_bytes": logical_pages * page_size,
            "freelist_bytes": freelist_pages * page_size,
            "blobs_bytes": sum(
                path.stat().st_size
                for path in self.blobs_dir.rglob("*.gz")
            ),
            "blobs_count": sum(1 for _ in self.blobs_dir.rglob("*.gz")),
        }
