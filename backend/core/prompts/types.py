from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class PromptSection:
    """A model-visible prompt block inserted into the message list."""

    name: str
    role: str
    content: str
    priority: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.metadata:
            message["metadata"] = dict(self.metadata)
        return message


@dataclass(frozen=True)
class PromptBuildRequest:
    """Inputs needed to build provider-ready prompt messages."""

    base_messages: Iterable[dict[str, Any]]
    active_skill_names: Iterable[str] = ()
    extra_sections: Iterable[PromptSection] = ()
    capability_char_budget: Optional[int] = None
    include_available_capabilities: bool = True
    include_core_prompt: bool = True
    custom_system_prompt: Optional[str] = None
    custom_system_prompt_mode: str = "override"
    runtime_mode: str = "main"
