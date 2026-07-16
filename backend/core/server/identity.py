from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from backend.core.persistence.database import SQLitePersistence


SERVER_INSTANCE_ID_KEY = "server_instance_id"


class ServerIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerIdentity:
    server_instance_id: str


class ServerIdentityStore:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self._persistence = persistence

    def get_or_create(self) -> ServerIdentity:
        candidate = str(uuid.uuid4())
        created_at = int(time.time())

        with self._persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO server_metadata (key, value, created_at)
                VALUES (?, ?, ?)
                """,
                (SERVER_INSTANCE_ID_KEY, candidate, created_at),
            )
            row = conn.execute(
                "SELECT value FROM server_metadata WHERE key = ?",
                (SERVER_INSTANCE_ID_KEY,),
            ).fetchone()

        if row is None:
            raise ServerIdentityError("server instance id was not persisted")

        value = str(row["value"])
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ServerIdentityError("invalid server instance id") from exc

        if parsed.version != 4 or str(parsed) != value:
            raise ServerIdentityError("invalid server instance id")

        return ServerIdentity(server_instance_id=value)
