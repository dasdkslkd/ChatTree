from pathlib import Path

import pytest

from backend.core.persistence.blob_store import BlobStore
from backend.core.persistence.content import INLINE_TEXT_LIMIT, store_text_content
from backend.core.persistence.database import SQLitePersistence


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
        row = conn.execute(
            "SELECT ref_count FROM blobs WHERE id = ?", (first.blob_id,)
        ).fetchone()
    assert row["ref_count"] == 2


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


def test_store_text_content_keeps_short_text_inline(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    stored = store_text_content(persistence, "short text", preview_limit=4)

    assert stored.inline == "short text"
    assert stored.blob_id is None
    assert stored.preview == "shor"
    assert stored.size == len("short text".encode("utf-8"))


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
