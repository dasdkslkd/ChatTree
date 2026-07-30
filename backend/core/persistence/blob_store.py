from __future__ import annotations

import gzip
import hashlib
import os
import uuid
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
        final_path = self.persistence.blobs_dir / blob_id[:2] / f"{blob_id}.gz"
        compressed = gzip.compress(data)

        with self.persistence.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT path, byte_size, stored_size, compression
                FROM blobs
                WHERE id = ?
                """,
                (blob_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE blobs
                    SET last_accessed_at = strftime('%s', 'now')
                    WHERE id = ?
                    """,
                    (blob_id,),
                )
            else:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                self.persistence.tmp_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = (
                    self.persistence.tmp_dir
                    / f"{blob_id}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                )
                tmp_path.write_bytes(compressed)
                try:
                    os.replace(tmp_path, final_path)
                finally:
                    if tmp_path.exists():
                        tmp_path.unlink()
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
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
                row = conn.execute(
                    """
                    SELECT path, byte_size, stored_size, compression
                    FROM blobs
                    WHERE id = ?
                    """,
                    (blob_id,),
                ).fetchone()

        return BlobRecord(
            blob_id=blob_id,
            path=self._resolve_stored_path(row["path"]),
            byte_size=row["byte_size"],
            stored_size=row["stored_size"],
            compression=row["compression"],
        )

    def get_text(self, blob_id: str) -> str:
        with self.persistence.connect() as conn:
            return self.get_text_in_connection(conn, blob_id)

    def get_text_in_connection(self, conn, blob_id: str) -> str:
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

        path = self._resolve_stored_path(row["path"])
        if not path.exists():
            raise KeyError(blob_id)

        data = path.read_bytes()
        if row["compression"] == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8")

    def _resolve_stored_path(self, stored_path: str) -> Path:
        relative_path = Path(stored_path)
        if relative_path.is_absolute():
            raise ValueError(f"Blob path is outside persistence home: {stored_path}")

        home = self.persistence.home.resolve()
        path = (home / relative_path).resolve()
        try:
            path.relative_to(home)
        except ValueError as exc:
            raise ValueError(
                f"Blob path is outside persistence home: {stored_path}"
            ) from exc
        return path
