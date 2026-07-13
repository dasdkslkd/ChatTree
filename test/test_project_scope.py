from pathlib import Path
from types import SimpleNamespace
import sys
import asyncio

import pytest
sys.path.insert(0, ".")

from backend.core.capabilities.agent_loader import load_agent_roots
from backend.core.capabilities.prompting import (
    build_available_capabilities_prompt,
    collect_skill_injection_names,
)
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.skill_loader import load_skill_roots
from backend.core.capabilities.types import CapabilitySource
from backend.core.agents.subagent_executor import SubagentExecutor
from backend.core.config.config import cfg
from backend.core.projects import filter_capability_registry_for_workspace
from backend.core.tools.exposure import ToolExposureContext
from backend.core.tools.tool_manager import ToolManager


def write_skill(root: Path, name: str, description: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
name: {name}
description: {description}
---

# {name}
""",
        encoding="utf-8",
    )


def write_agent(root: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"""---
name: {name}
description: {name} agent
---

You are {name}.
""",
        encoding="utf-8",
    )


def make_registry(tmp_path: Path) -> CapabilityRegistry:
    skill_root = tmp_path / "skills"
    agent_root = tmp_path / "agents"
    write_skill(skill_root, "visible-skill", "visible browser task")
    write_skill(skill_root, "hidden-skill", "hidden browser task")
    write_agent(agent_root, "visible-agent")
    write_agent(agent_root, "hidden-agent")
    registry = CapabilityRegistry()
    registry.add_capabilities(load_skill_roots([skill_root], source=CapabilitySource.PROJECT))
    registry.add_agents(load_agent_roots([agent_root], source=CapabilitySource.PROJECT))
    return registry


def project_config(tmp_path: Path) -> dict:
    cwd = str(tmp_path / "project")
    return {
        "projects": {
            cwd: {
                "visible": True,
                "enabled_skills": ["visible-skill"],
                "enabled_agents": ["visible-agent"],
                "enabled_mcp_servers": ["visible-server"],
            }
        }
    }


def test_project_scope_filters_available_capabilities_and_skill_mentions(tmp_path):
    registry = make_registry(tmp_path)
    config = project_config(tmp_path)
    workspace = {"cwd": str(tmp_path / "project")}

    scoped = filter_capability_registry_for_workspace(registry, config, workspace)
    prompt = build_available_capabilities_prompt(scoped)

    assert "visible-skill" in prompt
    assert "visible-agent" in prompt
    assert "hidden-skill" not in prompt
    assert "hidden-agent" not in prompt
    assert collect_skill_injection_names("$hidden-skill", scoped) == []
    assert collect_skill_injection_names("$visible-skill", scoped) == ["visible-skill"]


def test_tool_manager_filters_mcp_servers_by_project_scope(tmp_path):
    config = project_config(tmp_path)
    manager = ToolManager({
        **config,
        "tools": {
            "enabled": True,
            "builtin": {"enabled": False},
            "mcp": {"enabled": True, "servers": {}},
        },
    })
    manager._connection_manager = SimpleNamespace(
        list_all_tools=lambda: [
            {
                "server": "visible-server",
                "tool": {"name": "search"},
                "callable_name": "visible_server__search",
                "openai_schema": {"type": "function", "function": {"name": "visible_server__search"}},
            },
            {
                "server": "hidden-server",
                "tool": {"name": "search"},
                "callable_name": "hidden_server__search",
                "openai_schema": {"type": "function", "function": {"name": "hidden_server__search"}},
            },
        ],
    )

    tools = manager.get_openai_tools(
        workspace={"cwd": str(tmp_path / "project")},
        exposure_context=ToolExposureContext(include_mcp=True),
    )

    names = [tool["function"]["name"] for tool in tools]
    assert "visible_server__search" in names
    assert "hidden_server__search" not in names


def test_subagent_start_uses_conversation_workspace_when_request_omits_workspace(tmp_path):
    registry = make_registry(tmp_path)
    config = project_config(tmp_path)
    previous_cfg = cfg.data
    cfg.data = config

    class FakeChatManager:
        tool_manager = None

        def get_conversation(self, conversation_id):
            return SimpleNamespace(metadata={"workspace": {"cwd": str(tmp_path / "project")}})

    try:
        executor = SubagentExecutor(
            chat_manager=FakeChatManager(),
            run_manager=SimpleNamespace(),
            capability_registry=registry,
        )
        with pytest.raises(KeyError):
            asyncio.run(
                executor.start(
                    conversation_id="conv",
                    agent_name="hidden-agent",
                    input_data="task",
                    workspace=None,
                )
            )
    finally:
        cfg.data = previous_cfg
