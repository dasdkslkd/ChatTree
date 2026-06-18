# tools/tool_filter.py - tool allow/deny filtering
from typing import Iterable, Optional


class ToolFilter:
    """Configuration-driven allow/deny filter for model-visible tools."""

    def __init__(
        self,
        enabled: Optional[Iterable[str]] = None,
        disabled: Optional[Iterable[str]] = None,
    ):
        self.enabled = set(enabled) if enabled else None
        self.disabled = set(disabled or [])

    def is_allowed(self, tool_name: str, aliases: Optional[Iterable[str]] = None) -> bool:
        names = {tool_name, *(aliases or [])}
        if self.enabled is not None and not (names & self.enabled):
            return False
        if names & self.disabled:
            return False
        return True
