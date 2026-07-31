from pathlib import Path

from backend.core import home


def test_resolve_chattree_home_prefers_explicit_env(monkeypatch, tmp_path):
    configured = tmp_path / "configured-home"
    monkeypatch.setenv("CHATTREE_HOME", str(configured))

    assert home.resolve_chattree_home() == configured


def test_resolve_chattree_home_uses_userprofile_on_windows(monkeypatch, tmp_path):
    user = tmp_path / "User"
    monkeypatch.delenv("CHATTREE_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setattr(home, "_is_windows", lambda: True)

    assert home.resolve_chattree_home() == user / ".chattree"


def test_resolve_chattree_home_uses_home_elsewhere(monkeypatch, tmp_path):
    user = tmp_path / "home"
    monkeypatch.delenv("CHATTREE_HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setattr(home, "_is_windows", lambda: False)

    assert home.resolve_chattree_home() == user / ".chattree"


def test_main_wires_legacy_chat_files_and_canonical_tool_results():
    main_source = Path("main.py").read_text(encoding="utf-8")

    assert 'ChatStorage(str(persistence.home / "conversations"))' in main_source
    assert 'PromptStorage(str(persistence.home / "prompts"))' in main_source
    assert "ToolResultStorage" not in main_source
    assert "ToolManager(runtime_config, chat_repository=chat_repository)" in main_source
    assert "run_repository.mark_unfinished_as_interrupted()" in main_source


def test_default_config_uses_chattree_home(monkeypatch, tmp_path):
    from backend.core.config.config import Config

    configured_home = tmp_path / "home"
    monkeypatch.setenv("CHATTREE_HOME", str(configured_home))

    config = Config()

    assert Path(config.config_path) == configured_home / "config.json"
