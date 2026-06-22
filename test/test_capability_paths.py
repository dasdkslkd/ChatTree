from pathlib import Path
import sys

import pytest

sys.path.insert(0, ".")

from backend.core.capabilities.paths import (
    CapabilityPathError,
    read_text_utf8,
    resolve_inside_root,
    write_text_utf8,
)
from backend.core.capabilities.types import (
    CapabilityKind,
    CapabilitySource,
    SkillInjection,
)
from backend.core.capabilities import (
    CapabilityDefinition as ExportedCapabilityDefinition,
    CapabilityKind as ExportedCapabilityKind,
    CapabilitySource as ExportedCapabilitySource,
    SkillInjection as ExportedSkillInjection,
)


def test_resolve_inside_root_accepts_relative_path_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    resolved = resolve_inside_root(root, "./skills")

    assert resolved == (root / "skills").resolve()


def test_resolve_inside_root_rejects_relative_path_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(CapabilityPathError):
        resolve_inside_root(root, "../outside")


def test_resolve_inside_root_rejects_absolute_path_outside_root(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(CapabilityPathError):
        resolve_inside_root(root, outside)


def test_read_text_utf8_reads_chinese(tmp_path):
    file_path = tmp_path / "中文.txt"
    file_path.write_text("你好，能力系统", encoding="utf-8")

    assert read_text_utf8(file_path) == "你好，能力系统"


def test_capability_enums_include_planned_values():
    assert CapabilitySource.PROJECT.value == "project"
    assert CapabilityKind.MCP_SERVER.value == "mcp_server"


def test_skill_injection_has_path_field(tmp_path):
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"

    injection = SkillInjection(
        name="demo",
        path=skill_path,
        content="技能内容",
    )

    assert injection.path == skill_path


def test_capability_package_exports_core_types():
    assert ExportedCapabilitySource is CapabilitySource
    assert ExportedCapabilityKind is CapabilityKind
    assert ExportedCapabilityDefinition.__name__ == "CapabilityDefinition"
    assert ExportedSkillInjection is SkillInjection


def test_write_text_utf8_creates_parent_directories(tmp_path):
    file_path = tmp_path / "nested" / "中文.txt"

    write_text_utf8(file_path, "写入中文")

    assert file_path.read_text(encoding="utf-8") == "写入中文"
