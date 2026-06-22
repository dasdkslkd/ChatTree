from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from backend.core.capabilities.paths import read_text_utf8
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import CapabilityKind, SkillInjection


TRUNCATION_NOTICE = "[Capability list truncated to fit context budget.]"


def build_available_capabilities_prompt(
    registry: CapabilityRegistry,
    char_budget: int = 8000,
) -> str:
    if char_budget <= 0:
        return ""

    lines = [
        "## Available Capabilities",
        "",
        "Skills are instruction packs, not callable tool functions.",
        "When asked which skills are available or whether a skill exists, answer from this loaded skill list even if there is no same-named tool function.",
        "Some skills operate through commands, local daemons, HTTP endpoints, or MCP servers named differently from the skill.",
        "When a skill is injected, follow its SKILL.md health checks and operation instructions before deciding the capability is unavailable.",
        "",
        "### Skills",
    ]
    skills = registry.skills()
    if skills:
        for skill in skills:
            description = skill.description.strip()
            if skill.when_to_use:
                description = f"{description} {skill.when_to_use.strip()}".strip()
            if skill.aliases:
                description = (
                    f"{description} (aliases: {', '.join(skill.aliases)})"
                ).strip()
            lines.append(f"- {skill.name}: {description}")
    else:
        lines.append("- None")

    agents = registry.agents()
    if agents:
        lines.extend(["", "### Agents"])
        for agent in agents:
            description = agent.description.strip()
            lines.append(f"- {agent.name}: {description}")

    plugins = registry.plugins()
    if plugins:
        lines.extend(["", "### Plugins"])
        for plugin in plugins:
            description = plugin.description.strip()
            lines.append(f"- {plugin.name}: {description}")

    prompt = "\n".join(lines)
    if len(prompt) <= char_budget:
        return prompt

    suffix = f"\n{TRUNCATION_NOTICE}"
    if char_budget <= len(TRUNCATION_NOTICE):
        return TRUNCATION_NOTICE[:char_budget]
    return prompt[: char_budget - len(suffix)].rstrip() + suffix


def collect_explicit_skill_mentions(
    text: str,
    registry: CapabilityRegistry,
) -> list[str]:
    mentions: list[tuple[int, str]] = [
        (match.start(), match.group(1))
        for match in re.finditer(r"\$([A-Za-z0-9_.:-]+)", text)
    ]

    slash_match = re.match(r"\s*/([A-Za-z0-9_.:-]+)(?=\s|$)", text)
    if slash_match:
        mentions.append((slash_match.start(1) - 1, slash_match.group(1)))

    lowered_text = text.lower()
    for skill in registry.skills():
        if _can_match_skill_name_directly(skill.name):
            start = lowered_text.find(skill.name.lower())
            if start >= 0:
                mentions.append((start, skill.name))
        for alias in skill.aliases:
            start = _find_alias_mention(lowered_text, alias.lower())
            if start >= 0:
                mentions.append((start, skill.name))

    candidates = [name for _, name in sorted(mentions, key=lambda item: item[0])]
    return _dedupe_existing_skills(candidates, registry)


def build_skill_injections(
    skill_names: Iterable[str],
    registry: CapabilityRegistry,
) -> list[SkillInjection]:
    injections: list[SkillInjection] = []
    for name in skill_names:
        skill = registry.get(name)
        if (
            skill is None
            or skill.kind != CapabilityKind.SKILL
            or skill.path is None
        ):
            continue
        path = Path(skill.path)
        injections.append(
            SkillInjection(
                name=skill.name,
                path=path,
                content=read_text_utf8(path),
            )
        )
    return injections


def format_skill_injections(injections: list[SkillInjection]) -> str:
    parts: list[str] = []
    for injection in injections:
        parts.append(
            "\n".join(
                [
                    "<skill>",
                    f"<name>{injection.name}</name>",
                    f"<path>{injection.path}</path>",
                    "<content>",
                    injection.content,
                    "</content>",
                    "</skill>",
                ]
            )
        )
    return "\n\n".join(parts)


def _dedupe_existing_skills(
    candidates: Iterable[str],
    registry: CapabilityRegistry,
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for name in candidates:
        skill = registry.get(name)
        if skill is None or skill.kind != CapabilityKind.SKILL or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _can_match_skill_name_directly(name: str) -> bool:
    return any(separator in name for separator in "-:.") or len(name) >= 10


def _find_alias_mention(text: str, alias: str) -> int:
    if not alias:
        return -1

    start = 0
    while True:
        index = text.find(alias, start)
        if index < 0:
            return -1
        if _has_alias_boundaries(text, index, len(alias)):
            return index
        start = index + len(alias)


def _has_alias_boundaries(text: str, start: int, length: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after_index = start + length
    after = text[after_index] if after_index < len(text) else ""
    return not _is_ascii_word_char(before) and not _is_ascii_word_char(after)


def _is_ascii_word_char(value: str) -> bool:
    return bool(value) and (value.isascii() and (value.isalnum() or value == "_"))
