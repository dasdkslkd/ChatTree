from pathlib import Path
import json
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, ".")

from backend.api.routes.capabilities import router
from backend.core.capabilities.bootstrap import build_capability_registry


def write_skill(root: Path, folder: str, name: str, description: str) -> Path:
    skill_path = root / folder / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        f"""---
name: {name}
description: {description}
---

# {name}
""",
        encoding="utf-8",
    )
    return skill_path


def write_agent(root: Path, filename: str, name: str, description: str) -> Path:
    agent_path = root / f"{filename}.md"
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(
        f"""---
name: {name}
description: {description}
---

You are {name}.
""",
        encoding="utf-8",
    )
    return agent_path


def write_manifest(plugin_root: Path, manifest: dict) -> Path:
    manifest_path = plugin_root / ".chattree-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_build_capability_registry_loads_project_configured_and_active_plugin_capabilities(tmp_path):
    project_root = tmp_path / "project"
    default_skill = write_skill(
        project_root / ".chattree" / "skills",
        "project-skill",
        "project-skill",
        "Project skill",
    )
    default_agent = write_agent(
        project_root / ".chattree" / "agents",
        "project-agent",
        "project-agent",
        "Project agent",
    )
    extra_skill = write_skill(
        project_root / "extra" / "skills",
        "extra-skill",
        "extra-skill",
        "Extra skill",
    )
    extra_agent = write_agent(
        project_root / "extra" / "agents",
        "extra-agent",
        "extra-agent",
        "Extra agent",
    )

    plugin_root = project_root / ".chattree" / "plugins" / "demo-plugin"
    plugin_skill = write_skill(
        plugin_root / "skills",
        "review",
        "review",
        "Plugin skill",
    )
    plugin_agent = write_agent(
        plugin_root / "agents",
        "helper",
        "helper",
        "Plugin agent",
    )
    write_manifest(
        plugin_root,
        {
            "description": "Demo plugin",
            "paths": {
                "skills": "skills",
                "agents": "agents",
            },
        },
    )
    disabled_root = project_root / ".chattree" / "plugins" / "disabled-plugin"
    write_skill(disabled_root / "skills", "ignored", "ignored", "Ignored skill")
    write_manifest(disabled_root, {"enabled": False, "paths": {"skills": "skills"}})

    registry = build_capability_registry(
        project_root,
        {
            "capabilities": {
                "skill_roots": "extra/skills",
                "agent_roots": [project_root / "extra" / "agents"],
            },
        },
    )

    inventory = registry.inventory()
    skill_names = [skill["name"] for skill in inventory["skills"]]
    agent_names = [agent["name"] for agent in inventory["agents"]]
    plugin_ids = [plugin["plugin_id"] for plugin in inventory["plugins"]]

    assert skill_names == ["project-skill", "extra-skill", "demo-plugin:review"]
    assert agent_names == ["project-agent", "extra-agent", "demo-plugin:helper"]
    assert plugin_ids == ["demo-plugin@local"]
    assert registry.get("project-skill").path == default_skill
    assert registry.get("extra-skill").path == extra_skill
    assert registry.get("demo-plugin:review").path == plugin_skill
    assert registry.get_agent("project-agent").path == default_agent
    assert registry.get_agent("extra-agent").path == extra_agent
    assert registry.get_agent("demo-plugin:helper").path == plugin_agent
    assert registry.get("demo-plugin:review").plugin_id == "demo-plugin@local"
    assert registry.get("demo-plugin:review").namespace == "demo-plugin"
    assert registry.get("ignored") is None


def test_capabilities_api_returns_inventory_and_summary(tmp_path):
    project_root = tmp_path / "project"
    write_skill(
        project_root / ".chattree" / "skills",
        "project-skill",
        "project-skill",
        "Project skill",
    )
    registry = build_capability_registry(project_root)
    app = FastAPI()
    app.state.capability_registry = registry
    app.include_router(router)
    client = TestClient(app)

    inventory_response = client.get("/capabilities")
    summary_response = client.get("/capabilities/summary")

    assert inventory_response.status_code == 200
    assert inventory_response.json()["skills"][0]["name"] == "project-skill"
    assert summary_response.status_code == 200
    assert "project-skill" in summary_response.json()["summary"]
