from pathlib import Path
import json
import sys

sys.path.insert(0, ".")

from backend.core.capabilities import CapabilityRegistry, load_skill_roots
from backend.core.capabilities.skill_loader import parse_simple_yaml
from backend.core.capabilities.types import (
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)


def write_skill(root: Path, name: str = "review") -> Path:
    skill_path = root / name / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: review
description: Review code changes
when_to_use: Use before merging code
allowed_tools:
  - read_file
  - search
policy:
  allow_implicit_invocation: false
---

# Review

Check changes carefully.
""",
        encoding="utf-8",
    )
    return skill_path


def test_load_skill_roots_discovers_project_skill(tmp_path):
    root = tmp_path / "skills"
    skill_path = write_skill(root)

    skills = load_skill_roots([root], source=CapabilitySource.PROJECT)

    assert len(skills) == 1
    skill = skills[0]
    assert skill.name == "review"
    assert skill.kind == CapabilityKind.SKILL
    assert skill.source == CapabilitySource.PROJECT
    assert skill.description == "Review code changes"
    assert skill.when_to_use == "Use before merging code"
    assert skill.allowed_tools == ["read_file", "search"]
    assert skill.path == skill_path
    assert skill.metadata["base_name"] == "review"
    assert skill.metadata["content_length"] > 0
    assert skill.metadata["allow_implicit_invocation"] is False


def test_load_skill_roots_namespaces_plugin_skills(tmp_path):
    root = tmp_path / "skills"
    write_skill(root)

    skills = load_skill_roots(
        [root],
        source=CapabilitySource.PLUGIN,
        plugin_id="plugin-123",
        plugin_name="github-helper",
    )

    assert len(skills) == 1
    skill = skills[0]
    assert skill.name == "github-helper:review"
    assert skill.plugin_id == "plugin-123"
    assert skill.plugin_name == "github-helper"
    assert skill.namespace == "github-helper"
    assert skill.metadata["base_name"] == "review"


def test_load_skill_roots_supports_comma_separated_allowed_tools_and_when_to_use_alias(tmp_path):
    root = tmp_path / "skills"
    skill_path = root / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: review
description: Review code changes
when-to-use: Use before merging code
allowed_tools: read_file, search
---

# Review
""",
        encoding="utf-8",
    )

    skills = load_skill_roots([root], source=CapabilitySource.PROJECT)

    assert len(skills) == 1
    assert skills[0].when_to_use == "Use before merging code"
    assert skills[0].allowed_tools == ["read_file", "search"]


def test_load_skill_roots_supports_skill_aliases(tmp_path):
    root = tmp_path / "skills"
    skill_path = root / "kimi-webbridge" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: kimi-webbridge
description: Control the user's real browser
aliases:
  - webbridge
  - browser
---

# Kimi WebBridge
""",
        encoding="utf-8",
    )

    skills = load_skill_roots([root], source=CapabilitySource.PROJECT)

    assert len(skills) == 1
    assert skills[0].aliases == ["webbridge", "browser"]
    assert skills[0].metadata["aliases"] == ["webbridge", "browser"]


def test_load_skill_roots_supports_literal_block_description(tmp_path):
    root = tmp_path / "skills"
    skill_path = root / "kimi-webbridge" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: kimi-webbridge
description: |
  Kimi WebBridge lets AI control the user's real browser.
  Use when the user asks to interact with websites.
---

# Kimi WebBridge
""",
        encoding="utf-8",
    )

    skills = load_skill_roots([root], source=CapabilitySource.PROJECT)

    assert len(skills) == 1
    assert skills[0].description != "|"
    assert "Kimi WebBridge lets AI control" in skills[0].description
    assert "Use when the user asks" in skills[0].description
    assert "\n" in skills[0].description


def test_parse_simple_yaml_supports_folded_block_scalar():
    parsed = parse_simple_yaml(
        """description: >
  Kimi WebBridge lets AI control the user's real browser.
  Use when the user asks to interact with websites.
name: kimi-webbridge
"""
    )

    assert parsed["description"] == (
        "Kimi WebBridge lets AI control the user's real browser. "
        "Use when the user asks to interact with websites."
    )
    assert parsed["name"] == "kimi-webbridge"


def test_load_skill_roots_skips_skill_without_description(tmp_path):
    root = tmp_path / "skills"
    skill_path = root / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: review
description:
---

# Review
""",
        encoding="utf-8",
    )

    assert load_skill_roots([root], source=CapabilitySource.PROJECT) == []


def test_capability_registry_deduplicates_and_serializes_inventory(tmp_path):
    first = CapabilityDefinition(
        name="review",
        kind=CapabilityKind.SKILL,
        source=CapabilitySource.PROJECT,
        description="First",
        path=tmp_path / "first" / "SKILL.md",
    )
    duplicate = CapabilityDefinition(
        name="review",
        kind=CapabilityKind.SKILL,
        source=CapabilitySource.USER,
        description="Duplicate",
        path=tmp_path / "duplicate" / "SKILL.md",
    )
    agent_like = CapabilityDefinition(
        name="helper",
        kind=CapabilityKind.AGENT,
        source=CapabilitySource.PROJECT,
        description="Agent-like capability",
    )

    registry = CapabilityRegistry()
    registry.add_capabilities([first, duplicate, agent_like])

    assert registry.get("review") is first
    assert registry.skills() == [first]

    inventory = registry.inventory()
    json.dumps(inventory)
    assert inventory["skills"][0]["name"] == "review"
    assert inventory["skills"][0]["source"] == "project"
    assert inventory["skills"][0]["kind"] == "skill"
    assert inventory["skills"][0]["path"] == str(first.path)
    assert all(skill["name"] != "helper" for skill in inventory["skills"])
