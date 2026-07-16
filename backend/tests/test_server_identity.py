from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest

from backend.core.persistence.database import SQLitePersistence
from backend.core.server.identity import (
    SERVER_INSTANCE_ID_KEY,
    ServerIdentityError,
    ServerIdentityStore,
)


def _persistence(tmp_path: Path) -> SQLitePersistence:
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    return persistence


def test_get_or_create_persists_canonical_uuid4(tmp_path: Path):
    persistence = _persistence(tmp_path)

    first = ServerIdentityStore(persistence).get_or_create()
    second = ServerIdentityStore(persistence).get_or_create()

    parsed = UUID(first.server_instance_id)
    assert parsed.version == 4
    assert str(parsed) == first.server_instance_id
    assert second == first


def test_identity_is_scoped_to_chattree_home(tmp_path: Path):
    first = ServerIdentityStore(
        _persistence(tmp_path / "first")
    ).get_or_create()
    second = ServerIdentityStore(
        _persistence(tmp_path / "second")
    ).get_or_create()

    assert first.server_instance_id != second.server_instance_id


def test_concurrent_creation_returns_one_identity(tmp_path: Path):
    persistence = _persistence(tmp_path)

    def load_identity() -> str:
        return ServerIdentityStore(persistence).get_or_create().server_instance_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = set(executor.map(lambda _: load_identity(), range(16)))

    assert len(identities) == 1


def test_invalid_persisted_identity_fails_without_replacing_it(tmp_path: Path):
    persistence = _persistence(tmp_path)
    with persistence.connect() as conn:
        conn.execute(
            """
            INSERT INTO server_metadata (key, value, created_at)
            VALUES (?, ?, ?)
            """,
            (SERVER_INSTANCE_ID_KEY, "not-a-valid-uuid", 1),
        )

    with pytest.raises(ServerIdentityError, match="invalid server instance id"):
        ServerIdentityStore(persistence).get_or_create()

    with persistence.connect() as conn:
        row = conn.execute(
            "SELECT value FROM server_metadata WHERE key = ?",
            (SERVER_INSTANCE_ID_KEY,),
        ).fetchone()
    assert row["value"] == "not-a-valid-uuid"
