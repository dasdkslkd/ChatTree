from __future__ import annotations

from .prompts import INIT_PROMPT, btw_prompt, review_prompt
from .types import (
    SlashCommandDefinition,
    SlashDispatchKind,
    SlashPersistencePolicy,
    SlashToolPolicy,
)


def built_in_slash_definitions() -> list[SlashCommandDefinition]:
    return [
        SlashCommandDefinition(
            name="init",
            aliases=(),
            description="create project agent instructions",
            supports_inline_args=False,
            requires_args=False,
            dispatch_kind=SlashDispatchKind.MAIN_PROMPT,
            tool_policy=SlashToolPolicy.INHERIT,
            persistence_policy=SlashPersistencePolicy.MAIN_THREAD,
            run_kind="chat",
            prompt_builder=lambda _args: INIT_PROMPT,
            stream_target_policy="target_node",
            blocks_main_thread=True,
        ),
        SlashCommandDefinition(
            name="review",
            aliases=(),
            description="review current changes or custom target",
            supports_inline_args=True,
            requires_args=False,
            dispatch_kind=SlashDispatchKind.MAIN_PROMPT,
            tool_policy=SlashToolPolicy.INHERIT,
            persistence_policy=SlashPersistencePolicy.MAIN_THREAD,
            run_kind="chat",
            prompt_builder=review_prompt,
            stream_target_policy="target_node",
            blocks_main_thread=True,
        ),
        SlashCommandDefinition(
            name="refer",
            aliases=(),
            description="continue this turn with referenced historical evidence",
            supports_inline_args=True,
            requires_args=True,
            usage_args_label="selector... 本轮问题或指令",
            dispatch_kind=SlashDispatchKind.REFER_PROMPT,
            tool_policy=SlashToolPolicy.INHERIT,
            persistence_policy=SlashPersistencePolicy.MAIN_THREAD,
            run_kind="chat",
            prompt_builder=None,
            stream_target_policy="target_node",
            blocks_main_thread=True,
        ),
        SlashCommandDefinition(
            name="btw",
            aliases=(),
            description="ask a side question without interrupting the main conversation",
            supports_inline_args=True,
            requires_args=True,
            usage_args_label="旁路问题",
            dispatch_kind=SlashDispatchKind.SIDE_QUESTION,
            tool_policy=SlashToolPolicy.DISABLED,
            persistence_policy=SlashPersistencePolicy.SIDE_RUN,
            run_kind="side_question",
            prompt_builder=btw_prompt,
            stream_target_policy="anchor_only",
            blocks_main_thread=False,
        ),
        SlashCommandDefinition(
            name="fork",
            aliases=(),
            description="start a background fork",
            supports_inline_args=True,
            requires_args=True,
            dispatch_kind=SlashDispatchKind.SUBAGENT,
            tool_policy=SlashToolPolicy.INHERIT,
            persistence_policy=SlashPersistencePolicy.BACKGROUND_RUN,
            run_kind="subagent",
            prompt_builder=None,
            stream_target_policy="anchor_only",
            blocks_main_thread=False,
        ),
        SlashCommandDefinition(
            name="workflow",
            aliases=(),
            description="run a dynamic workflow",
            supports_inline_args=True,
            requires_args=True,
            dispatch_kind=SlashDispatchKind.WORKFLOW,
            tool_policy=SlashToolPolicy.INHERIT,
            persistence_policy=SlashPersistencePolicy.BACKGROUND_RUN,
            run_kind="workflow",
            prompt_builder=None,
            stream_target_policy="anchor_only",
            blocks_main_thread=False,
        ),
        SlashCommandDefinition(
            name="status",
            aliases=(),
            description="show current ChatTree runtime status",
            supports_inline_args=False,
            requires_args=False,
            dispatch_kind=SlashDispatchKind.DIRECT_RESPONSE,
            tool_policy=SlashToolPolicy.DISABLED,
            persistence_policy=SlashPersistencePolicy.SIDE_RUN,
            run_kind="direct_response",
            prompt_builder=None,
            stream_target_policy="none",
            blocks_main_thread=False,
        ),
        SlashCommandDefinition(
            name="help",
            aliases=(),
            description="show available slash commands",
            supports_inline_args=False,
            requires_args=False,
            dispatch_kind=SlashDispatchKind.DIRECT_RESPONSE,
            tool_policy=SlashToolPolicy.DISABLED,
            persistence_policy=SlashPersistencePolicy.SIDE_RUN,
            run_kind="direct_response",
            prompt_builder=None,
            stream_target_policy="none",
            blocks_main_thread=False,
        ),
        SlashCommandDefinition(
            name="capabilities",
            aliases=(),
            description="show registered ChatTree slash capabilities",
            supports_inline_args=False,
            requires_args=False,
            dispatch_kind=SlashDispatchKind.DIRECT_RESPONSE,
            tool_policy=SlashToolPolicy.DISABLED,
            persistence_policy=SlashPersistencePolicy.SIDE_RUN,
            run_kind="direct_response",
            prompt_builder=None,
            stream_target_policy="none",
            blocks_main_thread=False,
        ),
        SlashCommandDefinition(
            name="prune-summary",
            aliases=("prune",),
            description="summarize child branches under the current or specified node",
            supports_inline_args=True,
            requires_args=False,
            usage_args_label="node:<节点ID> 可选引导",
            dispatch_kind=SlashDispatchKind.DIRECT_RESPONSE,
            tool_policy=SlashToolPolicy.DISABLED,
            persistence_policy=SlashPersistencePolicy.SIDE_RUN,
            run_kind="direct_response",
            prompt_builder=None,
            stream_target_policy="anchor_only",
            blocks_main_thread=False,
        ),
    ]


class SlashCommandRegistry:
    def __init__(self, definitions: list[SlashCommandDefinition]) -> None:
        self._definitions = list(definitions)
        self._by_name: dict[str, SlashCommandDefinition] = {}
        for definition in self._definitions:
            self._by_name[definition.name] = definition
            for alias in definition.aliases:
                self._by_name[alias] = definition

    @classmethod
    def builtins(cls) -> "SlashCommandRegistry":
        return cls(built_in_slash_definitions())

    def get(self, name: str) -> SlashCommandDefinition | None:
        return self._by_name.get(name)

    def list(self) -> list[SlashCommandDefinition]:
        return list(self._definitions)

    def public_definitions(self) -> list[dict]:
        return [definition.to_public_dict() for definition in self._definitions]
