from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, ".")

from backend.core.capabilities import load_plugins_from_roots
from backend.core.capabilities.paths import CapabilityPathError
from backend.core.capabilities.plugin_loader import load_plugin
from backend.core.capabilities.types import PluginLoadOutcome


def write_manifest(plugin_root: Path, manifest: dict) -> Path:
    manifest_path = plugin_root / ".chattree-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_load_plugins_from_roots_loads_active_plugin_manifest(tmp_path):
    plugins_root = tmp_path / ".chattree" / "plugins"
    plugin_root = plugins_root / "demo"
    skills_root = plugin_root / "skills"
    agents_root = plugin_root / "agents"
    skills_root.mkdir(parents=True)
    agents_root.mkdir(parents=True)
    write_manifest(
        plugin_root,
        {
            "name": "Demo Plugin",
            "version": "1.2.3",
            "description": "Loads demo capabilities",
            "paths": {
                "skills": "skills",
                "agents": ["agents"],
                "mcp_servers": {
                    "demo": {
                        "command": "demo-server",
                    }
                },
            },
            "interface": {
                "label": "Demo",
            },
        },
    )

    outcome = load_plugins_from_roots([plugins_root])

    assert isinstance(outcome, PluginLoadOutcome)
    assert outcome.errors == []
    active_plugins = outcome.active_plugins()
    assert len(active_plugins) == 1
    plugin = active_plugins[0]
    assert plugin.plugin_id == "demo@local"
    assert plugin.name == "demo"
    assert plugin.root == plugin_root.resolve()
    assert plugin.enabled is True
    assert plugin.description == "Loads demo capabilities"
    assert plugin.version == "1.2.3"
    assert plugin.skill_roots == [skills_root.resolve()]
    assert plugin.agent_roots == [agents_root.resolve()]
    assert isinstance(plugin.mcp_servers, dict)
    assert plugin.mcp_servers["demo"]["command"] == "demo-server"
    assert plugin.interface == {"label": "Demo", "display_name": "Demo Plugin"}


def test_load_plugin_parses_manifest_enabled_without_string_truthiness(tmp_path):
    bool_root = tmp_path / ".chattree" / "plugins" / "bool-disabled"
    bool_manifest = write_manifest(
        bool_root,
        {
            "enabled": False,
        },
    )
    string_root = tmp_path / ".chattree" / "plugins" / "string-disabled"
    string_manifest = write_manifest(
        string_root,
        {
            "enabled": "false",
        },
    )

    assert load_plugin(bool_root, bool_manifest).is_active() is False
    assert load_plugin(string_root, string_manifest).is_active() is False


def test_plugin_load_outcome_effective_roots_are_ordered_and_deduplicated(tmp_path):
    plugins_root = tmp_path / ".chattree" / "plugins"
    shared_skills = plugins_root / "alpha" / "skills"
    shared_agents = plugins_root / "alpha" / "agents"
    beta_root = plugins_root / "beta"
    beta_skills = beta_root / "skills"
    beta_agents = beta_root / "agents"
    for path in [shared_skills, shared_agents, beta_skills, beta_agents]:
        path.mkdir(parents=True)
    write_manifest(
        plugins_root / "alpha",
        {
            "name": "Alpha",
            "paths": {
                "skills": ["skills", "skills"],
                "agents": ["agents", "agents"],
            },
        },
    )
    write_manifest(
        beta_root,
        {
            "name": "Beta",
            "paths": {
                "skills": ["skills"],
                "agents": ["agents"],
            },
        },
    )

    outcome = load_plugins_from_roots([plugins_root])

    assert outcome.effective_skill_roots() == [
        shared_skills.resolve(),
        beta_skills.resolve(),
    ]
    assert outcome.effective_agent_roots() == [
        shared_agents.resolve(),
        beta_agents.resolve(),
    ]


def test_load_plugin_rejects_manifest_path_escape(tmp_path):
    plugin_root = tmp_path / ".chattree" / "plugins" / "demo"
    manifest_path = write_manifest(
        plugin_root,
        {
            "name": "Demo",
            "paths": {
                "skills": ["../escaped"],
            },
        },
    )

    with pytest.raises(CapabilityPathError):
        load_plugin(plugin_root, manifest_path)


def test_load_plugin_loads_existing_hooks(tmp_path):
    plugin_root = tmp_path / ".chattree" / "plugins" / "demo"
    hook_path = plugin_root / "hooks" / "hooks.json"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text("{}", encoding="utf-8")
    manifest_path = write_manifest(
        plugin_root,
        {
            "paths": {
                "hooks": "hooks/hooks.json",
            },
        },
    )

    plugin = load_plugin(plugin_root, manifest_path)

    assert plugin.hooks == [hook_path.resolve()]


def test_load_plugin_rejects_manifest_file_outside_plugin_root(tmp_path):
    plugin_root = tmp_path / ".chattree" / "plugins" / "demo"
    manifest_path = write_manifest(
        tmp_path / ".chattree" / "plugins" / "other",
        {
            "name": "Other",
        },
    )

    with pytest.raises(CapabilityPathError):
        load_plugin(plugin_root, manifest_path)


def test_load_plugins_from_roots_returns_empty_outcome_for_missing_root(tmp_path):
    outcome = load_plugins_from_roots([tmp_path / "missing"])

    assert isinstance(outcome, PluginLoadOutcome)
    assert outcome.plugins == []
    assert outcome.errors == []
