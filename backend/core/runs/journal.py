from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from backend.core.home import resolve_chattree_home


class RunJournal:
    """Append-only UTF-8 run event journal."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else resolve_chattree_home() / "conversations"

    def _path_for(self, conversation_id: str, run_id: str) -> Path:
        safe_conversation = conversation_id.replace("/", "_").replace("\\", "_")
        safe_run = run_id.replace("/", "_").replace("\\", "_")
        return self.root / safe_conversation / "runs" / f"{safe_run}.jsonl"

    def append_event(self, conversation_id: str, run_id: str, event: Dict[str, Any]) -> None:
        path = self._path_for(conversation_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")

    def read_events(self, conversation_id: str, run_id: str) -> List[Dict[str, Any]]:
        path = self._path_for(conversation_id, run_id)
        if not path.exists():
            return []
        events: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                events.append(json.loads(stripped))
        return events

    def read_from_index(
        self,
        conversation_id: str,
        run_id: str,
        from_event: int = 0,
    ) -> Iterable[Dict[str, Any]]:
        for event in self.read_events(conversation_id, run_id):
            if int(event.get("event_index") or 0) >= from_event:
                yield event

    def delete_journal(self, conversation_id: str, run_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path_for(conversation_id, run_id).unlink()

    def list_run_files(self) -> List[Path]:
        """List every residual `conversations/*/runs/*.jsonl` journal file."""
        files: List[Path] = []
        for runs_dir in self.root.glob("*/runs"):
            files.extend(runs_dir.glob("*.jsonl"))
        return files
