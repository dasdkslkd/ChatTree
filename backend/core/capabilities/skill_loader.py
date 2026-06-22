from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from backend.core.capabilities.paths import read_text_utf8
from backend.core.capabilities.types import (
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)


def load_skill_roots(
    roots: Iterable[str | Path],
    source: CapabilitySource,
    plugin_id: Optional[str] = None,
    plugin_name: Optional[str] = None,
) -> list[CapabilityDefinition]:
    skills: list[CapabilityDefinition] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue

        for skill_file in sorted(root_path.glob("*/SKILL.md")):
            skill = load_skill_file(
                skill_file,
                source=source,
                plugin_id=plugin_id,
                plugin_name=plugin_name,
            )
            if skill is not None:
                skills.append(skill)
    return skills


def load_skill_file(
    path: str | Path,
    source: CapabilitySource,
    plugin_id: Optional[str] = None,
    plugin_name: Optional[str] = None,
) -> Optional[CapabilityDefinition]:
    skill_path = Path(path)
    markdown = read_text_utf8(skill_path)
    frontmatter = parse_frontmatter(markdown)
    content = strip_frontmatter(markdown)

    description = str(frontmatter.get("description") or "").strip()
    if not description:
        return None

    base_name = str(frontmatter.get("name") or skill_path.parent.name).strip()
    name = f"{plugin_name}:{base_name}" if plugin_name else base_name
    allowed_tools = _normalize_allowed_tools(frontmatter.get("allowed_tools"))
    aliases = _normalize_string_list(
        frontmatter.get("aliases", frontmatter.get("alias"))
    )
    policy = frontmatter.get("policy")
    allow_implicit_invocation = True
    if isinstance(policy, dict) and "allow_implicit_invocation" in policy:
        allow_implicit_invocation = bool(policy["allow_implicit_invocation"])
    elif "allow_implicit_invocation" in frontmatter:
        allow_implicit_invocation = bool(frontmatter["allow_implicit_invocation"])

    return CapabilityDefinition(
        name=name,
        kind=CapabilityKind.SKILL,
        source=source,
        description=description,
        path=skill_path,
        plugin_id=plugin_id,
        plugin_name=plugin_name,
        namespace=plugin_name,
        when_to_use=_optional_str(
            frontmatter.get("when_to_use", frontmatter.get("when-to-use"))
        ),
        allowed_tools=allowed_tools,
        aliases=aliases,
        metadata={
            "base_name": base_name,
            "aliases": aliases,
            "content_length": len(content),
            "allow_implicit_invocation": allow_implicit_invocation,
        },
    )


def parse_frontmatter(markdown: str) -> dict[str, Any]:
    if not markdown.startswith("---"):
        return {}

    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    frontmatter_lines: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            return parse_simple_yaml("\n".join(frontmatter_lines))
        frontmatter_lines.append(line)
    return {}


def strip_frontmatter(markdown: str) -> str:
    if not markdown.startswith("---"):
        return markdown

    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return markdown

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[index + 1 :])
    return markdown


def parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key: Optional[str] = None
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            index += 1
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if indent and current_key:
            current_value = result.get(current_key)
            if line.startswith("- "):
                if not isinstance(current_value, list):
                    current_value = []
                    result[current_key] = current_value
                current_value.append(_parse_scalar(line[2:].strip()))
                index += 1
                continue
            if ":" in line:
                if not isinstance(current_value, dict):
                    current_value = {}
                    result[current_key] = current_value
                nested_key, nested_value = line.split(":", 1)
                current_value[nested_key.strip()] = _parse_scalar(
                    nested_value.strip()
                )
                index += 1
                continue

        if ":" not in line:
            index += 1
            continue

        key, value = line.split(":", 1)
        current_key = key.strip()
        value = value.strip()
        if value in {"|", ">"}:
            block_lines, index = _collect_block_scalar_lines(
                lines,
                start=index + 1,
                parent_indent=indent,
            )
            result[current_key] = (
                _format_literal_block(block_lines)
                if value == "|"
                else _format_folded_block(block_lines)
            )
            continue
        if value:
            result[current_key] = _parse_scalar(value)
        else:
            result[current_key] = None
        index += 1

    return result


def _collect_block_scalar_lines(
    lines: list[str],
    start: int,
    parent_indent: int,
) -> tuple[list[str], int]:
    block_lines: list[str] = []
    block_indent: Optional[int] = None
    index = start

    while index < len(lines):
        raw_line = lines[index]
        if raw_line.strip():
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent <= parent_indent:
                break
            if block_indent is None:
                block_indent = indent
            block_lines.append(raw_line[min(block_indent, indent) :])
        else:
            block_lines.append("")
        index += 1

    return block_lines, index


def _format_literal_block(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n")


def _format_folded_block(lines: list[str]) -> str:
    paragraphs: list[str] = []
    current: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped:
            current.append(stripped)
            continue
        if current:
            paragraphs.append(" ".join(current))
            current = []

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def _parse_scalar(value: str) -> Any:
    if value == "":
        return None

    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None

    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {'"', "'"}
    ):
        return value[1:-1]

    return value


def _normalize_allowed_tools(value: Any) -> list[str]:
    return _normalize_string_list(value)


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
