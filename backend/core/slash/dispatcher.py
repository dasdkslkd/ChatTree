from __future__ import annotations

from .parser import parse_slash_command
from .registry import SlashCommandRegistry
from .types import SlashDispatchKind, SlashDispatchResult


class SlashCommandDispatcher:
    """Codex-style slash command dispatcher.

    Parsing and dispatch are intentionally separate from ChatManager. The chat
    loop consumes a structured result and decides which dispatch kinds it can
    execute today.
    """

    def __init__(self, registry: SlashCommandRegistry | None = None) -> None:
        self.registry = registry or SlashCommandRegistry.builtins()

    def dispatch(self, text: str) -> SlashDispatchResult:
        parsed = parse_slash_command(text)
        if parsed is None:
            return SlashDispatchResult.passthrough(text)

        definition = self.registry.get(parsed.name)
        if definition is None or not definition.enabled:
            return SlashDispatchResult.passthrough(text)

        if parsed.args and not definition.supports_inline_args:
            return SlashDispatchResult.passthrough(text)

        if definition.requires_args and not parsed.args.strip():
            return SlashDispatchResult(
                kind=SlashDispatchKind.ERROR,
                original_input=text,
                command=definition,
                command_name=parsed.name,
                canonical_name=definition.name,
                args=parsed.args,
                error=f"用法: /{definition.name} <参数>",
                tool_policy=definition.tool_policy,
                persistence_policy=definition.persistence_policy,
                run_kind=definition.run_kind,
                stream_target_policy=definition.stream_target_policy,
                blocks_main_thread=definition.blocks_main_thread,
                metadata=definition.to_public_dict(),
            )

        model_input = (
            definition.prompt_builder(parsed.args)
            if definition.prompt_builder is not None
            else None
        )

        return SlashDispatchResult(
            kind=definition.dispatch_kind,
            original_input=text,
            command=definition,
            command_name=parsed.name,
            canonical_name=definition.name,
            args=parsed.args,
            model_input=model_input,
            tool_policy=definition.tool_policy,
            persistence_policy=definition.persistence_policy,
            run_kind=definition.run_kind,
            stream_target_policy=definition.stream_target_policy,
            blocks_main_thread=definition.blocks_main_thread,
            metadata=definition.to_public_dict(),
            disable_tools=definition.tool_policy.value == "disabled",
        )
