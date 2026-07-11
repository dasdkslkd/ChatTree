import json
import os
import uuid
from time import time
from typing import Any, Dict, Optional

from backend.core.persistence.home import resolve_chattree_home
from .atomic import atomic_write_json
from ..utils.logger import setup_logger

logger = setup_logger("ToolResultStorage")


class ToolResultStorage:
    """Stores full tool outputs outside the chat message chain."""

    def __init__(self, storage_dir: str | None = None, sqlite_repository=None):
        self.storage_dir = str(storage_dir or resolve_chattree_home() / "tool_results")
        self.index_file = os.path.join(self.storage_dir, "index.json")
        self.sqlite_repository = sqlite_repository
        os.makedirs(self.storage_dir, exist_ok=True)
        self._load_index()

    def _load_index(self) -> None:
        if os.path.exists(self.index_file):
            with open(self.index_file, "r", encoding="utf-8") as f:
                self.index: Dict[str, Dict[str, Any]] = json.load(f)
        else:
            self.index = {}
            self._save_index()

    def _save_index(self) -> None:
        atomic_write_json(self.index_file, self.index)

    def _path_for(self, tool_result_id: str) -> str:
        return os.path.join(self.storage_dir, f"{tool_result_id}.json")

    def save_result(
        self,
        *,
        content: str,
        tool_name: str,
        conversation_id: str,
        node_id: Optional[str],
        tool_call_id: Optional[str],
        structured_metadata: Optional[Dict[str, Any]] = None,
        raw_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        tool_result_id = str(uuid.uuid4())
        path = self._path_for(tool_result_id)
        record = {
            "id": tool_result_id,
            "tool_name": tool_name,
            "conversation_id": conversation_id,
            "node_id": node_id,
            "tool_call_id": tool_call_id,
            "content": content,
            "created_at": int(time()),
            "total_chars": len(content),
        }
        if structured_metadata is not None:
            record["structured_metadata"] = structured_metadata
        if raw_metadata is not None:
            record["raw_metadata"] = raw_metadata
        atomic_write_json(path, record)
        self.index[tool_result_id] = {
            "id": tool_result_id,
            "path": path,
            "tool_name": tool_name,
            "conversation_id": conversation_id,
            "node_id": node_id,
            "tool_call_id": tool_call_id,
            "created_at": record["created_at"],
            "total_chars": record["total_chars"],
        }
        self._save_index()
        self._save_sqlite_copy(record)
        logger.debug(f"工具结果已落盘: {tool_result_id} ({len(content)} chars)")
        return record

    def _save_sqlite_copy(self, record: Dict[str, Any]) -> None:
        repository = getattr(self, "sqlite_repository", None)
        if repository is None:
            return
        try:
            call_id = repository.add_tool_call(
                record["conversation_id"],
                record["node_id"],
                tool_call_id=record.get("tool_call_id"),
                name=record.get("tool_name") or "",
                arguments=None,
                call_index=0,
                status="complete",
            )
            repository.add_tool_result(
                record["conversation_id"],
                record["node_id"],
                tool_result_id=record["id"],
                tool_call_id=call_id,
                output=record.get("content") or "",
                status="complete",
                metadata={
                    "tool_name": record.get("tool_name"),
                    "structured_metadata": record.get("structured_metadata"),
                    "raw_metadata": record.get("raw_metadata"),
                },
            )
        except Exception as exc:
            logger.warning(
                "SQLite 工具结果副本写入失败: %s",
                exc,
                exc_info=True,
            )

    def read_result(self, tool_result_id: str) -> Optional[Dict[str, Any]]:
        meta = self.index.get(tool_result_id)
        if not meta:
            self._load_index()
            meta = self.index.get(tool_result_id)
        if not meta:
            return None
        path = meta.get("path") or self._path_for(tool_result_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def read_slice(self, tool_result_id: str, offset: int = 0, limit: int = 16000) -> Optional[Dict[str, Any]]:
        record = self.read_result(tool_result_id)
        if not record:
            return None
        content = record.get("content") or ""
        offset = max(0, int(offset or 0))
        limit = max(1, int(limit or 16000))
        chunk = content[offset:offset + limit]
        next_offset = offset + len(chunk)
        total_chars = len(content)
        return {
            "tool_result_id": tool_result_id,
            "tool_name": record.get("tool_name"),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < total_chars else None,
            "total_chars": total_chars,
            "has_more": next_offset < total_chars,
            "content": chunk,
        }
