from pathlib import Path

from backend.core.persistence import home


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
