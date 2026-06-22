from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

from backend.core.capabilities.paths import read_text_utf8
from backend.core.capabilities.skill_loader import parse_frontmatter, strip_frontmatter
from backend.core.capabilities.types import AgentDefinition, CapabilitySource


_KNOWN_FRONTMATTER_FIELDS = {
    "name",
    "description",
    "tools",
    "skills",
    "model",
    "max_turns",
    "maxTurns",
}
_PLUGIN_PRIVILEGED_FIELDS = {
    "permission_mode",
    "permissionMode",
    "hooks",
    "mcp_servers",
    "mcpServers",
}


def load_agent_roots(
    roots: Iterable[str | Path],
    source: CapabilitySource,
    plugin_id: Optional[str] = None,
    plugin_name: Optional[str] = None,
) -> list[AgentDefinition]:
    agents: list[AgentDefinition] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.exists() or not root_path.is_dir():
            continue

        for agent_file in sorted(root_path.glob("*.md")):
            agents.append(
                load_agent_file(
                    agent_file,
                    source=source,
                    plugin_id=plugin_id,
                    plugin_name=plugin_name,
                )
            )
    return agents


def load_agent_file(
    path: str | Path,
    source: CapabilitySource,
    plugin_id: Optional[str] = None,
    plugin_name: Optional[str] = None,
) -> AgentDefinition:
    agent_path = Path(path)
    markdown = read_text_utf8(agent_path)
    frontmatter = parse_frontmatter(markdown)
    content = strip_frontmatter(markdown)

    base_name = str(frontmatter.get("name") or agent_path.stem).strip() or agent_path.stem
    is_plugin_agent = bool(plugin_id or plugin_name)
    name = f"{plugin_name}:{base_name}" if plugin_name else base_name
    metadata = _metadata_from_frontmatter(frontmatter, is_plugin_agent)
    metadata["base_name"] = base_name
    metadata["content_length"] = len(content)

    return AgentDefinition(
        name=name,
        description=str(frontmatter.get("description") or "").strip(),
        system_prompt=content.strip(),
        tools=_normalize_string_list(frontmatter.get("tools")),
        skills=_normalize_string_list(frontmatter.get("skills")),
        model=_optional_str(frontmatter.get("model")),
        max_turns=_parse_positive_int(
            frontmatter.get("max_turns", frontmatter.get("maxTurns"))
        ),
        plugin_id=plugin_id,
        plugin_name=plugin_name,
        path=agent_path,
        source=source,
        metadata=metadata,
    )


def _metadata_from_frontmatter(
    frontmatter: dict[str, Any],
    is_plugin_agent: bool,
) -> dict[str, Any]:
    filtered_fields = set(_KNOWN_FRONTMATTER_FIELDS)
    if is_plugin_agent:
        filtered_fields.update(_PLUGIN_PRIVILEGED_FIELDS)
    return {
        key: value
        for key, value in frontmatter.items()
        if key not in filtered_fields
    }


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    text = str(value).strip()
    return [text] if text else []


def _parse_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
