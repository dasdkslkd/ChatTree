from __future__ import annotations

import gzip
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .database import SQLitePersistence


@dataclass(frozen=True)
class BlobRecord:
    blob_id: str
    path: Path
    byte_size: int
    stored_size: int
    compression: str


class BlobStore:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence

    def put_text(
        self, text: str, mime_type: str = "text/plain; charset=utf-8"
    ) -> BlobRecord:
        value = text or ""
        data = value.encode("utf-8")
        blob_id = hashlib.sha256(data).hexdigest()
        relative_path = f"blobs/{blob_id[:2]}/{blob_id}.gz"
        final_path = self.persistence.home / "blobs" / blob_id[:2] / f"{blob_id}.gz"
        compressed = gzip.compress(data)

        with self.persistence.connect() as conn:
            existing = conn.execute(
                """
                SELECT path, byte_size, stored_size, compression
                FROM blobs
                WHERE id = ?
                """,
                (blob_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE blobs
                    SET ref_count = ref_count + 1,
                        last_accessed_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (blob_id,),
                )
                return BlobRecord(
                    blob_id=blob_id,
                    path=self.persistence.home / existing["path"],
                    byte_size=existing["byte_size"],
                    stored_size=existing["stored_size"],
                    compression=existing["compression"],
                )

            final_path.parent.mkdir(parents=True, exist_ok=True)
            self.persistence.tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self.persistence.tmp_dir / f"{blob_id}.{os.getpid()}.tmp"
            tmp_path.write_bytes(compressed)
            os.replace(tmp_path, final_path)
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
                  ref_count,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, strftime('%s', 'now'))
                """,
                (
                    blob_id,
                    relative_path,
                    mime_type,
                    "gzip",
                    len(data),
                    len(compressed),
                    len(value),
                ),
            )

        return BlobRecord(
            blob_id=blob_id,
            path=final_path,
            byte_size=len(data),
            stored_size=len(compressed),
            compression="gzip",
        )

    def get_text(self, blob_id: str) -> str:
        with self.persistence.connect() as conn:
            row = conn.execute(
                "SELECT path, compression FROM blobs WHERE id = ?", (blob_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE blobs SET last_accessed_at = strftime('%s', 'now') WHERE id = ?",
                    (blob_id,),
                )

        if row is None:
            raise KeyError(blob_id)

        path = self.persistence.home / row["path"]
        if not path.exists():
            raise KeyError(blob_id)

        data = path.read_bytes()
        if row["compression"] == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8")
