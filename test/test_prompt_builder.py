from pathlib import Path
import sys

sys.path.insert(0, ".")

from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import (
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySource,
)
from backend.core.prompts.builder import PromptBuilder


def test_prompt_builder_inserts_capabilities_after_existing_system(tmp_path: Path):
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Review\n\n检查代码变更。", encoding="utf-8")
    registry = CapabilityRegistry()
    registry.add_capabilities(
        [
            CapabilityDefinition(
                name="review",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description="Review skill",
                path=skill_path,
            )
        ]
    )
    messages = [
        {"role": "system", "content": "base system"},
        {"role": "user", "content": "use $review"},
    ]

    built = PromptBuilder(registry=registry).build_messages(
        messages,
        active_skill_names=["review"],
    )

    assert [message["role"] for message in built[:4]] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert built[0]["content"] == "base system"
    assert "## Available Capabilities" in built[1]["content"]
    assert "<name>review</name>" in built[2]["content"]
    assert "检查代码变更" in built[2]["content"]


def test_prompt_builder_omits_empty_skill_injection_section(tmp_path: Path):
    registry = CapabilityRegistry()
    registry.add_capabilities(
        [
            CapabilityDefinition(
                name="review",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description="Review skill",
            )
        ]
    )

    built = PromptBuilder(registry=registry).build_messages(
        [{"role": "user", "content": "hello"}],
        active_skill_names=[],
    )

    assert len(built) == 2
    assert built[0]["role"] == "system"
    assert "## Available Capabilities" in built[0]["content"]
    assert built[1]["content"] == "hello"


def test_prompt_builder_can_skip_available_capability_summary(tmp_path: Path):
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Review\n\n检查代码。", encoding="utf-8")
    registry = CapabilityRegistry()
    registry.add_capabilities(
        [
            CapabilityDefinition(
                name="review",
                kind=CapabilityKind.SKILL,
                source=CapabilitySource.PROJECT,
                description="Review skill",
                path=skill_path,
            )
        ]
    )

    built = PromptBuilder(registry=registry).build_messages(
        [{"role": "system", "content": "base"}, {"role": "user", "content": "go"}],
        active_skill_names=["review"],
        include_available_capabilities=False,
    )

    assert len(built) == 3
    assert built[0]["content"] == "base"
    assert "## Available Capabilities" not in built[1]["content"]
    assert "<name>review</name>" in built[1]["content"]
