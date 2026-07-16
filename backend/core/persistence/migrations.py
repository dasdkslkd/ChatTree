from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


class UnsupportedSchemaVersionError(RuntimeError):
    pass


class MigrationPathError(RuntimeError):
    pass


class MigrationTransactionError(RuntimeError):
    pass


class MigrationCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.__cursor = cursor

    def __iter__(self):
        return iter(self.__cursor)

    def fetchone(self):
        return self.__cursor.fetchone()

    def fetchall(self):
        return self.__cursor.fetchall()

    @property
    def lastrowid(self):
        return self.__cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self.__cursor.rowcount


class MigrationConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.__conn = conn

    def execute(self, sql: str, parameters=()) -> MigrationCursor:
        _reject_transaction_control(sql)
        return MigrationCursor(self.__conn.execute(sql, parameters))

    def executemany(self, sql: str, parameters) -> MigrationCursor:
        _reject_transaction_control(sql)
        return MigrationCursor(self.__conn.executemany(sql, parameters))

    def commit(self) -> None:
        raise MigrationTransactionError("migration callback cannot commit")

    def rollback(self) -> None:
        raise MigrationTransactionError("migration callback cannot roll back")

    def executescript(self, script: str) -> None:
        raise MigrationTransactionError(
            "migration callback cannot use executescript because it commits implicitly"
        )

    @property
    def in_transaction(self) -> bool:
        return self.__conn.in_transaction


@dataclass(frozen=True)
class Migration:
    from_version: int
    to_version: int
    apply: Callable[[MigrationConnection], None]
    destructive: bool = False


class SchemaMigrationRunner:
    def __init__(
        self,
        *,
        db_path: str | Path,
        backup_dir: str | Path,
        current_version: int,
        migrations: Iterable[Migration],
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        self.current_version = current_version
        self.migrations = tuple(migrations)

    def run(self, conn: sqlite3.Connection) -> Path | None:
        if conn.in_transaction:
            raise RuntimeError("schema migration requires an idle connection")

        conn.execute("PRAGMA foreign_keys = OFF")
        backup_path = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > self.current_version:
                raise UnsupportedSchemaVersionError(
                    f"database schema version {version} is newer than supported "
                    f"version {self.current_version}"
                )

            path = self._build_path(version)
            if not path:
                conn.execute("COMMIT")
                return None

            if (
                any(migration.destructive for migration in path)
                and self._has_user_tables(conn)
            ):
                backup_path = self._create_backup(version, path[-1].to_version)

            migration_conn = MigrationConnection(conn)
            for migration in path:
                migration.apply(migration_conn)
                conn.execute(f"PRAGMA user_version = {migration.to_version}")

            violation = conn.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise RuntimeError(
                    "foreign key violation after schema migration: "
                    f"{tuple(violation)}"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

        return backup_path

    def _build_path(self, start_version: int) -> tuple[Migration, ...]:
        by_version: dict[int, Migration] = {}
        for migration in self.migrations:
            if migration.from_version < 0 or migration.to_version != migration.from_version + 1:
                raise MigrationPathError(
                    "schema migrations must be adjacent, forward-only steps"
                )
            if migration.from_version in by_version:
                raise MigrationPathError(
                    f"duplicate migration from version {migration.from_version}"
                )
            by_version[migration.from_version] = migration

        path: list[Migration] = []
        version = start_version
        while version < self.current_version:
            migration = by_version.get(version)
            if migration is None:
                raise MigrationPathError(
                    f"no migration registered from schema version {version}"
                )
            if migration.to_version > self.current_version:
                raise MigrationPathError(
                    f"migration from version {version} exceeds supported schema version"
                )
            path.append(migration)
            version = migration.to_version
        return tuple(path)

    @staticmethod
    def _has_user_tables(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        return row is not None

    def _create_backup(
        self,
        from_version: int,
        to_version: int,
    ) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = self.backup_dir / (
            f"chattree-v{from_version}-before-v{to_version}-{timestamp}.sqlite"
        )
        temporary_path = backup_path.with_suffix(backup_path.suffix + ".tmp")

        try:
            with closing(sqlite3.connect(self.db_path)) as source:
                source.execute("PRAGMA query_only = ON")
                with closing(sqlite3.connect(temporary_path)) as destination:
                    source.backup(destination)
                    result = destination.execute("PRAGMA quick_check").fetchone()
                    if result is None or result[0] != "ok":
                        raise RuntimeError("database backup failed quick_check")
            os.replace(temporary_path, backup_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        return backup_path


def execute_sql_script(conn: MigrationConnection, script: str) -> None:
    parts: list[str] = []
    for character in script:
        parts.append(character)
        if character == ";":
            statement = "".join(parts)
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                parts.clear()

    remainder = "".join(parts)
    if remainder.strip():
        raise ValueError("incomplete SQL statement in schema script")


_TRANSACTION_KEYWORDS = {
    "BEGIN",
    "COMMIT",
    "END",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
    "VACUUM",
}
_RUNNER_OWNED_PRAGMAS = {
    "foreign_keys",
    "journal_mode",
    "locking_mode",
    "user_version",
    "writable_schema",
}


def _reject_transaction_control(sql: str) -> None:
    statement = _strip_leading_comments(sql)
    keyword_match = re.match(r"[A-Za-z]+", statement)
    if keyword_match is None:
        return
    keyword = keyword_match.group(0).upper()
    if keyword in _TRANSACTION_KEYWORDS:
        raise MigrationTransactionError(
            f"migration callback cannot execute transaction control SQL: {keyword}"
        )
    if keyword == "PRAGMA":
        pragma_match = re.match(
            r"PRAGMA\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)",
            statement,
            flags=re.IGNORECASE,
        )
        if pragma_match and pragma_match.group(1).lower() in _RUNNER_OWNED_PRAGMAS:
            raise MigrationTransactionError(
                f"migration callback cannot set runner-owned PRAGMA "
                f"{pragma_match.group(1)}"
            )


def _strip_leading_comments(sql: str) -> str:
    position = 0
    while True:
        while position < len(sql) and (
            sql[position].isspace() or sql[position] == ";"
        ):
            position += 1
        if sql.startswith("--", position):
            newline = sql.find("\n", position + 2)
            if newline < 0:
                return ""
            position = newline + 1
            continue
        if sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            if end < 0:
                return sql[position:]
            position = end + 2
            continue
        return sql[position:]
