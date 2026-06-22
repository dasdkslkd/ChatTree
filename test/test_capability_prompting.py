from pathlib import Path
import sys

sys.path.insert(0, ".")

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import (
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)
from backend.core.capabilities.prompting import (
    build_available_capabilities_prompt,
    build_skill_injections,
    collect_explicit_skill_mentions,
    format_skill_injections,
)


def make_registry(skill_path: Path) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.add_capabilities(
        [
            CapabilityDefinition(
                name="review",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description="Skill review",
                path=skill_path,
                when_to_use="Use before merging",
            ),
            CapabilityDefinition(
                name="other",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description="Other skill",
            ),
            CapabilityDefinition(
                name="plugin.skill",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PLUGIN,
                description="Plugin skill",
            ),
            CapabilityDefinition(
                name="kimi-webbridge",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description=(
                    "Kimi WebBridge lets AI control the user's real browser.\n"
                    "Use when the user asks to interact with websites."
                ),
                aliases=["webbridge", "browser"],
            ),
            CapabilityDefinition(
                name="agentlike",
                kind=CapabilityKind.AGENT,
                source=CapabilitySource.PROJECT,
                description="Not a skill",
                path=skill_path,
            ),
        ]
    )
    return registry


def test_summary_includes_title_and_skill_line(tmp_path):
    registry = make_registry(tmp_path / "review" / "SKILL.md")

    prompt = build_available_capabilities_prompt(registry)

    assert "## Available Capabilities" in prompt
    assert "Skills are instruction packs, not callable tool functions." in prompt
    assert (
        "Some skills operate through commands, local daemons, HTTP endpoints, or MCP servers named differently from the skill."
        in prompt
    )
    assert (
        "When a skill is injected, follow its SKILL.md health checks and operation instructions before deciding the capability is unavailable."
        in prompt
    )
    assert "- review: Skill review" in prompt
    assert "Use before merging" in prompt
    assert "Kimi WebBridge lets AI control the user's real browser." in prompt
    assert "Use when the user asks to interact with websites." in prompt
    assert "aliases: webbridge, browser" in prompt


def test_collect_explicit_skill_mentions_supports_dollar_and_slash(tmp_path):
    registry = make_registry(tmp_path / "review" / "SKILL.md")

    assert collect_explicit_skill_mentions("please use $review now", registry) == [
        "review"
    ]
    assert collect_explicit_skill_mentions("/review this change", registry) == [
        "review"
    ]
    assert collect_explicit_skill_mentions("/review then $other", registry) == [
        "review",
        "other",
    ]
    assert collect_explicit_skill_mentions("please use $plugin.skill", registry) == [
        "plugin.skill"
    ]
    assert collect_explicit_skill_mentions("/plugin.skill please", registry) == [
        "plugin.skill"
    ]


def test_collect_explicit_skill_mentions_detects_known_skill_names_in_text(tmp_path):
    registry = make_registry(tmp_path / "review" / "SKILL.md")

    assert collect_explicit_skill_mentions("你有kimi-webbridge技能吗", registry) == [
        "kimi-webbridge"
    ]
    assert collect_explicit_skill_mentions("你有 kimi-webbridge 技能吗", registry) == [
        "kimi-webbridge"
    ]
    assert collect_explicit_skill_mentions(
        "先用 $review，再看看 kimi-webbridge", registry
    ) == ["review", "kimi-webbridge"]
    assert collect_explicit_skill_mentions("用webbridge打开bilibili", registry) == [
        "kimi-webbridge"
    ]
    assert collect_explicit_skill_mentions("open this in browser", registry) == [
        "kimi-webbridge"
    ]
    assert collect_explicit_skill_mentions("other words should not match", registry) == []


def test_collect_explicit_skill_mentions_skips_missing_skills(tmp_path):
    registry = make_registry(tmp_path / "review" / "SKILL.md")

    assert collect_explicit_skill_mentions("$missing /missing", registry) == []
    assert collect_explicit_skill_mentions("$agentlike", registry) == []


def test_summary_with_zero_budget_returns_empty_string(tmp_path):
    registry = make_registry(tmp_path / "review" / "SKILL.md")

    assert build_available_capabilities_prompt(registry, char_budget=0) == ""


def test_build_skill_injections_reads_complete_skill_file(tmp_path):
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text(
        "---\nname: review\ndescription: Skill review\n---\n\n# Review\n\n完整正文\n",
        encoding="utf-8",
    )
    registry = make_registry(skill_path)

    injections = build_skill_injections(["review"], registry)

    assert len(injections) == 1
    assert injections[0].name == "review"
    assert injections[0].path == skill_path
    assert "# Review" in injections[0].content
    assert "完整正文" in injections[0].content


def test_build_skill_injections_skips_non_skill_capabilities(tmp_path):
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("正文", encoding="utf-8")
    registry = make_registry(skill_path)

    assert build_skill_injections(["agentlike"], registry) == []


def test_format_skill_injections_wraps_name_path_and_content(tmp_path):
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("正文", encoding="utf-8")
    registry = make_registry(skill_path)
    injections = build_skill_injections(["review"], registry)

    prompt = format_skill_injections(injections)

    assert "<skill>" in prompt
    assert "<name>review</name>" in prompt
    assert f"<path>{skill_path}</path>" in prompt
    assert "正文" in prompt
