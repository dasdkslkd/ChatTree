from __future__ import annotations

from dataclasses import dataclass

from .blob_store import BlobStore
from .database import SQLitePersistence


INLINE_TEXT_LIMIT = 16 * 1024


@dataclass(frozen=True)
class StoredText:
    inline: str | None
    blob_id: str | None
    preview: str
    size: int


def store_text_content(
    persistence: SQLitePersistence,
    text: str,
    *,
    preview_limit: int = 4096,
    inline_limit: int = INLINE_TEXT_LIMIT,
) -> StoredText:
    value = text or ""
    preview = value[:preview_limit]
    size = len(value.encode("utf-8"))
    if size <= inline_limit:
        return StoredText(
            inline=value,
            blob_id=None,
            preview=preview,
            size=size,
        )

    blob = BlobStore(persistence).put_text(value)
    return StoredText(
        inline=None,
        blob_id=blob.blob_id,
        preview=preview,
        size=size,
    )
