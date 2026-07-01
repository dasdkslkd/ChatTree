from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class SlashDispatchKind(str, Enum):
    PASSTHROUGH = "passthrough"
    MAIN_PROMPT = "main_prompt"
    SIDE_QUESTION = "side_question"
    SUBAGENT = "subagent"
    WORKFLOW = "workflow"
    LOCAL_UI = "local_ui"
    ERROR = "error"


class SlashToolPolicy(str, Enum):
    INHERIT = "inherit"
    DISABLED = "disabled"
    READ_ONLY = "read_only"


class SlashPersistencePolicy(str, Enum):
    MAIN_THREAD = "main_thread"
    SIDE_RUN = "side_run"
    BACKGROUND_RUN = "background_run"
    NONE = "none"


@dataclass(frozen=True)
class SlashParsedInput:
    raw: str
    name: str
    args: str
    command_text: str


@dataclass(frozen=True)
class SlashCommandDefinition:
    name: str
    description: str
    dispatch_kind: SlashDispatchKind
    tool_policy: SlashToolPolicy
    persistence_policy: SlashPersistencePolicy
    run_kind: Optional[str]
    prompt_builder: Optional[Callable[[str], str]] = None
    aliases: tuple[str, ...] = ()
    supports_inline_args: bool = True
    requires_args: bool = False
    stream_target_policy: str = "target_node"
    blocks_main_thread: bool = True
    enabled: bool = True

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "supports_inline_args": self.supports_inline_args,
            "requires_args": self.requires_args,
            "dispatch_kind": self.dispatch_kind.value,
            "tool_policy": self.tool_policy.value,
            "persistence_policy": self.persistence_policy.value,
            "run_kind": self.run_kind,
            "stream_target_policy": self.stream_target_policy,
            "blocks_main_thread": self.blocks_main_thread,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class SlashDispatchResult:
    kind: SlashDispatchKind
    original_input: str
    command: Optional[object] = None
    command_name: Optional[str] = None
    canonical_name: Optional[str] = None
    args: str = ""
    model_input: Optional[str] = None
    error: Optional[str] = None
    tool_policy: SlashToolPolicy = SlashToolPolicy.INHERIT
    persistence_policy: SlashPersistencePolicy = SlashPersistencePolicy.MAIN_THREAD
    run_kind: Optional[str] = "chat"
    stream_target_policy: str = "target_node"
    blocks_main_thread: bool = True
    metadata: dict[str, Any] | None = None
    disable_tools: bool = False

    @property
    def is_passthrough(self) -> bool:
        return self.kind == SlashDispatchKind.PASSTHROUGH

    @classmethod
    def passthrough(cls, text: str) -> "SlashDispatchResult":
        return cls(
            kind=SlashDispatchKind.PASSTHROUGH,
            original_input=text,
            model_input=text,
            metadata={},
        )
