from pathlib import Path
import json
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

sys.path.insert(0, ".")

from backend.api.routes import capabilities as capabilities_route
from backend.api.routes.capabilities import router
from backend.core.capabilities.bootstrap import build_capability_registry
from backend.core.capabilities.types import CapabilitySource
from backend.core.config.config import Config


BUILTIN_AGENT_NAMES = [
    "explorer",
    "implementer",
    "planner",
    "reviewer",
    "verifier",
    "workflow-worker",
]


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
    capability_home = tmp_path / "home" / ".chattree"
    default_skill = write_skill(
        capability_home / "skills",
        "project-skill",
        "project-skill",
        "Project skill",
    )
    default_agent = write_agent(
        capability_home / "agents",
        "project-agent",
        "project-agent",
        "Project agent",
    )
    extra_skill = write_skill(
        capability_home / "extra" / "skills",
        "extra-skill",
        "extra-skill",
        "Extra skill",
    )
    extra_agent = write_agent(
        capability_home / "extra" / "agents",
        "extra-agent",
        "extra-agent",
        "Extra agent",
    )

    plugin_root = capability_home / "plugins" / "demo-plugin"
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
    disabled_root = capability_home / "plugins" / "disabled-plugin"
    write_skill(disabled_root / "skills", "ignored", "ignored", "Ignored skill")
    write_manifest(disabled_root, {"enabled": False, "paths": {"skills": "skills"}})

    registry = build_capability_registry(
        project_root,
        {
            "capabilities": {
                "skill_roots": "extra/skills",
                "agent_roots": [capability_home / "extra" / "agents"],
            },
        },
        capability_home=capability_home,
    )

    inventory = registry.inventory()
    skill_names = [skill["name"] for skill in inventory["skills"]]
    agent_names = [agent["name"] for agent in inventory["agents"]]
    plugin_ids = [plugin["plugin_id"] for plugin in inventory["plugins"]]

    assert skill_names == ["project-skill", "extra-skill", "demo-plugin:review"]
    assert agent_names == [
        "project-agent",
        "extra-agent",
        *BUILTIN_AGENT_NAMES,
        "demo-plugin:helper",
    ]
    assert plugin_ids == ["demo-plugin@local"]
    assert registry.get("project-skill").path == default_skill
    assert registry.get("extra-skill").path == extra_skill
    assert registry.get("demo-plugin:review").path == plugin_skill
    assert registry.get_agent("project-agent").path == default_agent
    assert registry.get_agent("extra-agent").path == extra_agent
    assert registry.get_agent("demo-plugin:helper").path == plugin_agent
    assert registry.get_agent("project-agent").source == CapabilitySource.USER
    assert registry.get_agent("extra-agent").source == CapabilitySource.PROJECT
    assert registry.get_agent("explorer").source == CapabilitySource.SYSTEM
    assert registry.get_agent("demo-plugin:helper").source == CapabilitySource.PLUGIN
    assert registry.get("demo-plugin:review").plugin_id == "demo-plugin@local"
    assert registry.get("demo-plugin:review").namespace == "demo-plugin"
    assert registry.get("ignored") is None


def test_empty_home_loads_complete_packaged_agent_layer(tmp_path):
    capability_home = tmp_path / "empty-home"

    registry = build_capability_registry(
        tmp_path / "project",
        capability_home=capability_home,
    )

    assert [agent.name for agent in registry.agents()] == BUILTIN_AGENT_NAMES
    assert registry.get_agent("general") is None
    for name in BUILTIN_AGENT_NAMES:
        agent = registry.get_agent(name)
        assert agent is not None
        assert agent.source == CapabilitySource.SYSTEM
        assert agent.path.parts[-3:] == ("templates", "agents", f"{name}.md")
        assert agent.tools == ["*"]
        assert agent.max_turns == 500
        assert agent.timeout_seconds == 86400
        assert agent.system_prompt.startswith("# ChatTree")
        assert not agent.system_prompt.startswith("---")
    assert registry.get_agent("workflow-worker").metadata["runtime"] == "workflow"


def test_user_agent_overrides_system_and_deletion_restores_system(tmp_path):
    capability_home = tmp_path / "home"
    override = write_agent(
        capability_home / "agents",
        "implementer",
        "implementer",
        "User implementation agent",
    )
    write_agent(
        capability_home / "agents",
        "custom",
        "custom",
        "Custom user agent",
    )

    overridden = build_capability_registry(
        tmp_path / "project",
        capability_home=capability_home,
    )

    assert overridden.get_agent("implementer").source == CapabilitySource.USER
    assert overridden.get_agent("implementer").description == "User implementation agent"
    assert overridden.get_agent("implementer").tools is None
    assert overridden.get_agent("implementer").max_turns is None
    assert overridden.get_agent("custom").source == CapabilitySource.USER
    assert len(overridden.agents()) == len(BUILTIN_AGENT_NAMES) + 1

    override.unlink()
    restored = build_capability_registry(
        tmp_path / "project",
        capability_home=capability_home,
    )

    assert restored.get_agent("implementer").source == CapabilitySource.SYSTEM
    assert restored.get_agent("implementer").description == "ChatTree implementation agent."


def test_agent_layer_priority_is_user_project_system_then_plugin(tmp_path):
    capability_home = tmp_path / "home"
    write_agent(
        capability_home / "agents",
        "implementer",
        "implementer",
        "User implementation agent",
    )
    configured_root = capability_home / "configured-agents"
    write_agent(
        configured_root,
        "implementer",
        "implementer",
        "Project implementation agent",
    )
    write_agent(
        configured_root,
        "planner",
        "planner",
        "Project planning agent",
    )

    registry = build_capability_registry(
        tmp_path / "project",
        {"capabilities": {"agent_roots": "configured-agents"}},
        capability_home=capability_home,
    )

    assert registry.get_agent("implementer").source == CapabilitySource.USER
    assert registry.get_agent("implementer").description == "User implementation agent"
    assert registry.get_agent("planner").source == CapabilitySource.PROJECT
    assert registry.get_agent("planner").description == "Project planning agent"
    assert registry.get_agent("explorer").source == CapabilitySource.SYSTEM


def test_capabilities_api_returns_inventory_and_summary(tmp_path):
    project_root = tmp_path / "project"
    capability_home = tmp_path / "home" / ".chattree"
    write_skill(
        capability_home / "skills",
        "project-skill",
        "project-skill",
        "Project skill",
    )
    registry = build_capability_registry(project_root, capability_home=capability_home)
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


def test_capabilities_reload_rebuilds_registry_from_disk(tmp_path, monkeypatch):
    class FakeToolManager:
        def __init__(self, config):
            self.config = config
            self.initialized = False
            self.closed = False

        async def init(self):
            self.initialized = True

        async def close(self):
            self.closed = True

        async def describe_inventory_async(self):
            return {"mcp_servers": []}

    class FakeModelManager:
        pass

    project_root = tmp_path / "project"
    capability_home = tmp_path / "home" / ".chattree"
    monkeypatch.setenv("CHATTREE_HOME", str(capability_home))
    write_skill(
        capability_home / "skills",
        "initial-skill",
        "initial-skill",
        "Initial skill",
    )
    config_manager = Config(str(tmp_path / "config.json"))
    config_manager.data = {
        **config_manager.data,
        "provider": {},
        "default_provider": "",
        "tools": {},
    }
    config_manager.save()
    registry = build_capability_registry(
        project_root,
        config_manager.data,
        capability_home=capability_home,
    )
    old_tool_manager = FakeToolManager(config_manager.data)
    chat_manager = SimpleNamespace(
        model_manager=object(),
        tool_manager=old_tool_manager,
        tool_orchestrator=None,
        capability_registry=registry,
    )

    app = FastAPI()
    app.state.project_root = project_root
    app.state.config_manager = config_manager
    app.state.capability_registry = registry
    app.state.tool_manager = old_tool_manager
    app.state.chat_manager = chat_manager
    app.include_router(router)
    client = TestClient(app)

    write_skill(
        capability_home / "skills",
        "new-skill",
        "new-skill",
        "New skill",
    )
    monkeypatch.setattr(capabilities_route, "ToolManager", FakeToolManager, raising=False)
    monkeypatch.setattr(capabilities_route, "ModelManager", FakeModelManager, raising=False)

    response = client.post("/capabilities/reload")

    assert response.status_code == 200
    skill_names = [skill["name"] for skill in response.json()["skills"]]
    assert skill_names == ["initial-skill", "new-skill"]
    assert app.state.capability_registry is chat_manager.capability_registry
    assert app.state.capability_registry.get("new-skill") is not None
    assert old_tool_manager.closed is True
    assert app.state.tool_manager is chat_manager.tool_manager
    assert app.state.tool_manager.initialized is True
