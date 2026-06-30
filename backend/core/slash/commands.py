from __future__ import annotations

from .registry import SlashCommandRegistry
from .types import SlashCommandDefinition


def built_in_slash_commands() -> dict[str, SlashCommandDefinition]:
    return {
        definition.name: definition
        for definition in SlashCommandRegistry.builtins().list()
    }

