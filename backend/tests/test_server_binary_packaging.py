from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import scripts.build_server_binary as build_server_binary


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_entry_module(name: str = "chattree_server_entry"):
    spec = importlib.util.spec_from_file_location(
        f"{name}_for_test",
        REPO_ROOT / "packaging" / f"{name}.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaging_entrypoint_delegates_to_server_cli(monkeypatch):
    entry = _load_entry_module()
    captured = {}

    def fake_main():
        captured["called"] = True
        return 7

    monkeypatch.setattr(entry, "server_main", fake_main)

    assert entry.main() == 7
    assert captured == {"called": True}


def test_launcher_entrypoint_treats_clean_shutdown_as_success(monkeypatch):
    entry = _load_entry_module("chattree_launcher_entry")
    import client_launcher.__main__ as launcher

    monkeypatch.setattr(sys, "argv", ["chattree-launcher"])
    monkeypatch.setattr(launcher, "main", lambda: None)

    assert entry.main() == 0


def test_pyinstaller_spec_collects_required_utf8_runtime_data():
    spec_text = (REPO_ROOT / "packaging" / "chattree-server.spec").read_text(
        encoding="utf-8"
    )

    assert "collect_data_files(\"backend.core.model\")" in spec_text
    assert "collect_data_files(\"backend.core.prompts\")" in spec_text
    assert "collect_data_files(\"backend.workers\")" in spec_text
    assert "\"main\"" in spec_text
    assert "\"backend.server_cli\"" in spec_text
    assert "\"backend.core.model.providers.openai_compatible\"" in spec_text
    assert "\"backend.core.model.providers.anthropic_provider\"" in spec_text
    assert "\"backend.core.model.providers.gemini_provider\"" in spec_text
    assert "\"chattree_protocol.http_errors\"" in spec_text


def test_build_script_constructs_pyinstaller_command(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    python = tmp_path / "venv" / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs)))

    monkeypatch.setattr(build_server_binary.subprocess, "run", fake_run)

    build_server_binary.run_pyinstaller(
        python,
        dist_dir=tmp_path / "dist",
        work_dir=tmp_path / "work",
        clean=True,
        one_dir=False,
    )

    assert len(calls) == 1
    assert calls[0][0] == [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(tmp_path / "dist"),
        "--workpath",
        str(tmp_path / "work"),
        "--clean",
        str(build_server_binary.SPEC_PATH),
    ]
    env = calls[0][1]["env"]
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["CHATTREE_PYINSTALLER_ONE_DIR"] == "0"


def test_build_script_install_uses_isolated_venv_python(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))

    monkeypatch.setattr(build_server_binary.subprocess, "run", fake_run)

    build_server_binary.install_build_dependencies(tmp_path / "python")

    assert calls[0][:4] == [str(tmp_path / "python"), "-m", "pip", "install"]
    assert any(part.startswith("pyinstaller==") for part in calls[0])
    assert any(part.startswith("pyinstaller-hooks-contrib==") for part in calls[0])
    assert calls[1] == [
        str(tmp_path / "python"),
        "-m",
        "pip",
        "install",
        str(build_server_binary.REPO_ROOT),
    ]


def test_build_script_rejects_clean_path_outside_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(build_server_binary, "REPO_ROOT", tmp_path / "repo")

    with pytest.raises(ValueError, match="outside repo"):
        build_server_binary._remove_path(tmp_path / "elsewhere" / "artifact")
