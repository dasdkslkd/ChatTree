from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonlSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._disabled = False

    @property
    def disabled(self) -> bool:
        return self._disabled

    def write(self, event: dict[str, Any]) -> None:
        if self._disabled:
            return
        try:
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.write("\n")
        except Exception:
            self._disabled = True
            logger.exception("Disabling performance JSONL sink after write failure: %s", self.path)
