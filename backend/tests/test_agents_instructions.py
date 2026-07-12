from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.core.agents.subagent_executor import SubagentExecutor
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import AgentDefinition
from backend.core.chat.chat_manager import ChatManager
from backend.core.instructions import load_agents_instructions


class FakeStorage:
    def __init__(self):
        self.saved = None

    def save(self, data):
        self.saved = data

    def load(self, conversation_id):
        if self.saved and self.saved["metadata"]["id"] == conversation_id:
            return self.saved
        return None


class FakePromptStorage:
    def load(self, prompt_id):
        return None


def test_load_agents_instructions_merges_global_and_project_root_to_cwd(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("global base", encoding="utf-8")
    (home / "AGENTS.override.md").write_text("global override", encoding="utf-8")

    repo = tmp_path / "repo"
    cwd = repo / "packages" / "app"
    cwd.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("root doc", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("cwd base", encoding="utf-8")
    (cwd / "AGENTS.override.md").write_text("cwd override", encoding="utf-8")

    loaded = load_agents_instructions(cwd=cwd, chattree_home=home)

    assert loaded.text == "global override\n\n--- project-doc ---\n\nroot doc\n\ncwd override"
    assert loaded.sources == [
        str(home / "AGENTS.override.md"),
        str(repo / "AGENTS.md"),
        str(cwd / "AGENTS.override.md"),
    ]
    assert "# AGENTS.md instructions for" in loaded.render()
    assert "<INSTRUCTIONS>" in loaded.render()


def test_load_agents_instructions_without_root_marker_only_reads_cwd(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    parent = tmp_path / "parent"
    cwd = parent / "child"
    cwd.mkdir(parents=True)
    (parent / "AGENTS.md").write_text("parent doc", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("cwd doc", encoding="utf-8")

    loaded = load_agents_instructions(cwd=cwd, chattree_home=home)

    assert loaded.text == "cwd doc"
    assert loaded.sources == [str(cwd / "AGENTS.md")]


def test_project_doc_byte_limit_truncates_later_content(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("abcdef", encoding="utf-8")

    loaded = load_agents_instructions(
        cwd=repo,
        chattree_home=home,
        config_data={"instructions": {"project_doc_max_bytes": 3}},
    )

    assert loaded.text == "abc"
    assert loaded.warnings


def test_project_root_markers_are_configurable(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "root"
    cwd = root / "nested"
    cwd.mkdir(parents=True)
    (root / ".chattree-root").write_text("", encoding="utf-8")
    (root / "AGENTS.md").write_text("custom root", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("nested", encoding="utf-8")

    loaded = load_agents_instructions(
        cwd=cwd,
        chattree_home=home,
        config_data={"instructions": {"project_root_markers": [".chattree-root"]}},
    )

    assert loaded.text == "custom root\n\nnested"


def test_agents_loading_can_be_disabled(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (home / "AGENTS.md").write_text("global", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("project", encoding="utf-8")

    loaded = load_agents_instructions(
        cwd=cwd,
        chattree_home=home,
        config_data={"instructions": {"include_agents_md": False}},
    )

    assert loaded.is_empty()
    assert loaded.sources == []


def test_agents_files_use_utf8_lossy_decoding(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "AGENTS.md").write_bytes(b"valid\xfftext")

    loaded = load_agents_instructions(cwd=cwd, chattree_home=home)

    assert loaded.text == "valid\ufffdtext"


def test_chat_manager_injects_agents_instructions_between_core_and_runtime(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CHATTREE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("project rule", encoding="utf-8")

    manager = ChatManager(
        model_manager=None,
        storage=FakeStorage(),
        prompts=FakePromptStorage(),
    )
    conversation = manager.create_conversation(
        "title",
        workspace={"cwd": str(repo), "workspace_roots": [str(repo)]},
    )

    messages = manager._build_prompt_messages(conversation, [])

    assert "# ChatTree Core Prompt" in messages[0]["content"]
    assert "# AGENTS.md instructions" in messages[1]["content"]
    assert "project rule" in messages[1]["content"]
    assert messages[1]["metadata"]["instruction_context"] == "agents_md"
    assert "Runtime mode: main chat" in messages[2]["content"]


def test_subagent_prompt_injects_agents_instructions(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CHATTREE_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("subagent project rule", encoding="utf-8")

    registry = CapabilityRegistry()
    registry.add_agents([AgentDefinition(name="reviewer", system_prompt="Reviewer worker body")])
    executor = SubagentExecutor(
        chat_manager=SimpleNamespace(tool_manager=None),
        run_manager=SimpleNamespace(),
        capability_registry=registry,
    )
    conversation = SimpleNamespace(metadata={"workspace": {"cwd": str(repo), "workspace_roots": [str(repo)]}})

    messages = executor._build_messages(
        "reviewer",
        "检查",
        "node-1",
        conversation=conversation,
    )

    assert "Reviewer worker body" in messages[0]["content"]
    assert "# AGENTS.md instructions" in messages[1]["content"]
    assert "subagent project rule" in messages[1]["content"]
    assert "Runtime mode: subagent" in messages[2]["content"]
