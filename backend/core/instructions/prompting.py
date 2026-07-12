from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from backend.core.prompts.types import PromptSection

from .loader import load_agents_instructions


def build_agents_instruction_section(
    workspace: Mapping[str, Any] | None,
    config_data: Mapping[str, Any] | None,
    *,
    chattree_home: str | Path | None = None,
) -> Optional[PromptSection]:
    if not isinstance(workspace, Mapping):
        return None
    cwd = workspace.get("cwd")
    if not cwd:
        return None

    loaded = load_agents_instructions(
        cwd=str(cwd),
        chattree_home=chattree_home,
        config_data=config_data,
    )
    content = loaded.render()
    if not content:
        return None
    metadata: dict[str, Any] = {
        "instruction_context": "agents_md",
        "sources": loaded.sources,
    }
    if loaded.warnings:
        metadata["warnings"] = list(loaded.warnings)
    return PromptSection(
        name="agents_md_instructions",
        role="system",
        content=content,
        priority=12,
        metadata=metadata,
    )
