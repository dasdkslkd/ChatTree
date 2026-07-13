from pathlib import Path
import sys

sys.path.insert(0, ".")

from backend.core.capabilities import load_agent_file, load_agent_roots
from backend.core.capabilities.types import CapabilitySource


def write_agent(root: Path, name: str, content: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_agent_roots_discovers_project_agent_with_frontmatter(tmp_path):
    root = tmp_path / "agents"
    agent_path = write_agent(
        root,
        "reviewer",
        """---
description: Reviews code changes
tools:
  - read
  - search
skills: review, testing
model: gpt-5
maxTurns: 4
custom_note: keep me
---

You are a careful code reviewer.
""",
    )

    agents = load_agent_roots([root], source=CapabilitySource.PROJECT)

    assert len(agents) == 1
    agent = agents[0]
    assert agent.name == "reviewer"
    assert agent.description == "Reviews code changes"
    assert agent.system_prompt == "You are a careful code reviewer."
    assert agent.tools == ["read", "search"]
    assert agent.skills == ["review", "testing"]
    assert agent.model == "gpt-5"
    assert agent.max_turns == 4
    assert agent.path == agent_path
    assert agent.source == CapabilitySource.PROJECT
    assert agent.plugin_id is None
    assert agent.plugin_name is None
    assert agent.metadata["base_name"] == "reviewer"
    assert agent.metadata["content_length"] == len("\nYou are a careful code reviewer.\n")
    assert agent.metadata["custom_note"] == "keep me"


def test_load_agent_roots_scans_direct_markdown_files_in_stable_order(tmp_path):
    root = tmp_path / "agents"
    second_path = write_agent(root, "zeta", "Second prompt.")
    first_path = write_agent(root, "alpha", "First prompt.")
    write_agent(root / "nested", "beta", "Nested prompt.")
    (root / "notes.txt").write_text("Not an agent.", encoding="utf-8")

    agents = load_agent_roots([root], source=CapabilitySource.PROJECT)

    assert [agent.name for agent in agents] == ["alpha", "zeta"]
    assert [agent.path for agent in agents] == [first_path, second_path]
    assert [agent.system_prompt for agent in agents] == [
        "First prompt.",
        "Second prompt.",
    ]


def test_load_agent_file_uses_frontmatter_name_for_project_agent(tmp_path):
    agent_path = write_agent(
        tmp_path,
        "file-name",
        """---
name: helper
description: Helps with code
max_turns: 2
---

Prompt body.
""",
    )

    agent = load_agent_file(agent_path, source=CapabilitySource.PROJECT)

    assert agent.name == "helper"
    assert agent.metadata["base_name"] == "helper"
    assert agent.max_turns == 2
    assert agent.tools is None


def test_load_agent_file_supports_comma_tools_and_yaml_list_skills(tmp_path):
    agent_path = write_agent(
        tmp_path,
        "helper",
        """---
description: Helps with code
tools: read, search
skills:
  - review
  - testing
---

Prompt body.
""",
    )

    agent = load_agent_file(agent_path, source=CapabilitySource.PROJECT)

    assert agent.tools == ["read", "search"]
    assert agent.skills == ["review", "testing"]


def test_load_agent_file_treats_inline_empty_list_as_empty_string_list(tmp_path):
    agent_path = write_agent(
        tmp_path,
        "helper",
        """---
description: Helps with code
tools: []
skills: []
---

Prompt body.
""",
    )

    agent = load_agent_file(agent_path, source=CapabilitySource.PROJECT)

    assert agent.tools == []
    assert agent.skills == []


def test_load_agent_file_supports_explicit_tool_wildcard_and_disallowed_tools(tmp_path):
    agent_path = write_agent(
        tmp_path,
        "helper",
        """---
description: Helps with code
tools: "*"
disallowedTools:
  - shell(git commit*)
  - agent
---

Prompt body.
""",
    )

    agent = load_agent_file(agent_path, source=CapabilitySource.PROJECT)

    assert agent.tools == ["*"]
    assert agent.disallowed_tools == ["shell(git commit*)", "agent"]


def test_load_agent_roots_namespaces_plugin_agents(tmp_path):
    root = tmp_path / "agents"
    write_agent(
        root,
        "reviewer",
        """---
name: review
description: Reviews code changes
---

Plugin prompt.
""",
    )

    agents = load_agent_roots(
        [root],
        source=CapabilitySource.PLUGIN,
        plugin_id="plugin-123",
        plugin_name="github-helper",
    )

    assert len(agents) == 1
    agent = agents[0]
    assert agent.name == "github-helper:review"
    assert agent.plugin_id == "plugin-123"
    assert agent.plugin_name == "github-helper"
    assert agent.metadata["base_name"] == "review"


def test_plugin_agent_filters_privileged_frontmatter_from_metadata(tmp_path):
    agent_path = write_agent(
        tmp_path,
        "privileged",
        """---
description: Tries to request powers
permission_mode: bypass
permissionMode: bypass
hooks:
  - run-this
mcp_servers:
  local:
mcpServers:
  remote:
ordinary: retained
---

Prompt mentions hooks and mcp_servers but body is not parsed for powers.
""",
    )

    agent = load_agent_file(
        agent_path,
        source=CapabilitySource.PLUGIN,
        plugin_id="plugin-123",
        plugin_name="plugin-name",
    )

    assert agent.permission_mode is None
    assert not hasattr(agent, "hooks")
    assert not hasattr(agent, "mcp_servers")
    assert "permission_mode" not in agent.metadata
    assert "permissionMode" not in agent.metadata
    assert "hooks" not in agent.metadata
    assert "mcp_servers" not in agent.metadata
    assert "mcpServers" not in agent.metadata
    assert agent.metadata["ordinary"] == "retained"


def test_load_agent_file_rejects_non_positive_or_invalid_max_turns(tmp_path):
    for value in ["0", "-1", "not-a-number"]:
        agent_path = write_agent(
            tmp_path / value.replace("-", "_"),
            "agent",
            f"""---
description: Bad max turns
max_turns: {value}
---

Prompt.
""",
        )

        agent = load_agent_file(agent_path, source=CapabilitySource.PROJECT)

        assert agent.max_turns is None


def test_load_agent_roots_returns_empty_for_missing_root(tmp_path):
    assert load_agent_roots([tmp_path / "missing"], source=CapabilitySource.PROJECT) == []
