import gzip
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.core.persistence.blob_store import BlobStore
from backend.core.persistence.content import INLINE_TEXT_LIMIT, store_text_content
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository


def test_blob_store_deduplicates_large_content(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    store = BlobStore(persistence)
    text = "x" * (INLINE_TEXT_LIMIT + 100)

    first = store.put_text(text)
    second = store.put_text(text)

    assert first.blob_id == second.blob_id
    assert first.byte_size == len(text.encode("utf-8"))
    assert store.get_text(first.blob_id) == text
    with persistence.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM blobs WHERE id = ?", (first.blob_id,)
        ).fetchone()[0]
    assert count == 1


def test_blob_store_concurrent_duplicate_puts_keep_one_blob(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    store = BlobStore(persistence)
    text = "same concurrent blob" * 1000
    call_count = 16

    with ThreadPoolExecutor(max_workers=call_count) as executor:
        records = list(executor.map(lambda _: store.put_text(text), range(call_count)))

    blob_ids = {record.blob_id for record in records}
    assert len(blob_ids) == 1
    blob_id = records[0].blob_id
    assert store.get_text(blob_id) == text

    with persistence.connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM blobs WHERE id = ?", (blob_id,)
        ).fetchall()

    assert len(rows) == 1
    assert gzip.decompress(records[0].path.read_bytes()).decode("utf-8") == text


def test_blob_store_uses_prefixed_gzip_path(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    store = BlobStore(persistence)

    record = store.put_text("hello blob store")

    assert (
        record.path
        == persistence.blobs_dir / record.blob_id[:2] / f"{record.blob_id}.gz"
    )
    assert record.path.exists()
    assert record.path.suffix == ".gz"
    with persistence.connect() as conn:
        row = conn.execute(
            "SELECT path, compression FROM blobs WHERE id = ?", (record.blob_id,)
        ).fetchone()
    assert row["path"] == f"blobs/{record.blob_id[:2]}/{record.blob_id}.gz"
    assert row["compression"] == "gzip"


def test_blob_store_missing_blob_raises_key_error(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()

    with pytest.raises(KeyError):
        BlobStore(persistence).get_text("missing")


@pytest.mark.parametrize(
    "stored_path",
    [
        lambda outside: str(outside.resolve()),
        lambda outside: f"../{outside.name}",
    ],
)
def test_blob_store_rejects_db_paths_outside_home(tmp_path: Path, stored_path):
    persistence = SQLitePersistence(tmp_path / "home")
    persistence.initialize()
    outside = tmp_path / "outside.gz"
    outside.write_bytes(gzip.compress("outside secret".encode("utf-8")))
    blob_id = "escaped-blob"

    with persistence.connect() as conn:
        conn.execute(
            """
            INSERT INTO blobs (
              id,
              path,
              mime_type,
              compression,
              byte_size,
              stored_size,
              char_count,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                blob_id,
                stored_path(outside),
                "text/plain; charset=utf-8",
                "gzip",
                len("outside secret".encode("utf-8")),
                outside.stat().st_size,
                len("outside secret"),
            ),
        )

    with pytest.raises(ValueError, match="outside persistence home"):
        BlobStore(persistence).get_text(blob_id)


def test_store_text_content_keeps_short_text_inline(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    stored = store_text_content(persistence, "short text", preview_limit=4)

    assert stored.inline == "short text"
    assert stored.blob_id is None
    assert stored.preview == "shor"
    assert stored.size == len("short text".encode("utf-8"))


def test_delete_conversation_reclaims_unreferenced_blob(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation("blob gc")
    node_id = repository.create_node(conversation_id, None)
    repository.add_message(
        conversation_id,
        node_id,
        "user",
        "x" * (INLINE_TEXT_LIMIT + 100),
    )
    with persistence.connect() as conn:
        row = conn.execute("SELECT id, path FROM blobs").fetchone()
    blob_path = persistence.home / row["path"]

    assert repository.delete_conversation(conversation_id) is True

    with persistence.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    assert not blob_path.exists()


def test_store_text_content_moves_large_text_to_blob(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    text = "hello " * 4000
    stored = store_text_content(persistence, text, preview_limit=12)

    assert stored.inline is None
    assert stored.blob_id
    assert stored.preview == text[:12]
    assert stored.size == len(text.encode("utf-8"))
    assert BlobStore(persistence).get_text(stored.blob_id) == text
