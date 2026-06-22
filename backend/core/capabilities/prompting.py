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

    candidates = [name for _, name in sorted(mentions, key=lambda item: item[0])]
    return _dedupe_existing_skills(candidates, registry)


def collect_skill_injection_names(
    text: str,
    registry: CapabilityRegistry,
    active_skill_names: Iterable[str] = (),
) -> list[str]:
    mentions: list[tuple[int, str]] = [
        (index, name)
        for index, name in enumerate(
            collect_explicit_skill_mentions(text, registry)
        )
    ]

    lowered_text = text.lower()
    if _looks_like_task_request(lowered_text):
        for skill in registry.skills():
            start = _find_current_turn_skill_intent(lowered_text, skill)
            if start >= 0:
                mentions.append((start, skill.name))

    if _looks_like_followup_task(lowered_text):
        offset = len(text) + 1
        for index, name in enumerate(active_skill_names):
            mentions.append((offset + index, name))

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


def _find_current_turn_skill_intent(text: str, skill) -> int:
    if _looks_like_skill_availability_question(text):
        return -1

    terms: list[str] = []
    if _can_match_skill_name_directly(skill.name):
        terms.append(skill.name.lower())
    terms.extend(alias.lower() for alias in skill.aliases)

    for term in terms:
        start = _find_alias_mention(text, term)
        if start >= 0:
            return start

    if _skill_supports_web_tasks(skill) and _contains_any(text, WEB_TASK_TERMS):
        return 0
    return -1


TASK_REQUEST_TERMS = {
    "用",
    "使用",
    "打开",
    "访问",
    "进入",
    "点击",
    "输入",
    "读取",
    "抓取",
    "搜索",
    "截图",
    "保存",
    "导出",
    "分析",
    "检查",
    "修",
    "写",
    "生成",
    "运行",
    "看看",
    "继续",
    "再",
    "然后",
    "接着",
    "use",
    "open",
    "visit",
    "navigate",
    "click",
    "type",
    "read",
    "scrape",
    "search",
    "screenshot",
    "save",
    "export",
    "analyze",
    "inspect",
    "check",
    "fix",
    "write",
    "generate",
    "run",
    "continue",
    "again",
    "then",
    "next",
}

FOLLOWUP_TASK_TERMS = {
    "继续",
    "再",
    "然后",
    "接着",
    "截图",
    "点击",
    "输入",
    "打开",
    "保存",
    "导出",
    "检查",
    "修",
    "continue",
    "again",
    "then",
    "next",
    "screenshot",
    "click",
    "type",
    "open",
    "save",
    "export",
    "check",
    "fix",
}

WEB_TASK_TERMS = {
    "网页",
    "网站",
    "浏览器",
    "页面",
    "bilibili",
    "http://",
    "https://",
    "www.",
    ".com",
    ".cn",
    "url",
    "browser",
    "webpage",
    "website",
    "page",
}


def _looks_like_task_request(text: str) -> bool:
    return _contains_any(text, TASK_REQUEST_TERMS)


def _looks_like_followup_task(text: str) -> bool:
    return _contains_any(text, FOLLOWUP_TASK_TERMS)


def _looks_like_skill_availability_question(text: str) -> bool:
    availability_terms = {
        "你有",
        "有哪些",
        "是否有",
        "有没有",
        "能用哪些",
        "what skills",
        "which skills",
        "do you have",
        "available skills",
    }
    return _contains_any(text, availability_terms)


def _skill_supports_web_tasks(skill) -> bool:
    haystack = " ".join(
        [
            skill.name,
            skill.description or "",
            skill.when_to_use or "",
            " ".join(skill.aliases),
        ]
    ).lower()
    return _contains_any(
        haystack,
        {
            "webbridge",
            "browser",
            "webpage",
            "website",
            "open url",
            "screenshot",
            "real browser",
            "navigate",
        },
    )


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term and term in text for term in terms)


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
