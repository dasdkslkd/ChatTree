from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
import zipfile
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
    for spec_name in ("chattree-server.spec", "chattree-launcher.spec"):
        spec_text = (REPO_ROOT / "packaging" / spec_name).read_text(encoding="utf-8")

        assert "collect_data_files(\"backend.core.model\")" in spec_text
        assert "collect_data_files(\"backend.core.prompts\")" in spec_text
        assert "collect_data_files(\"backend.workers\")" in spec_text
        assert "CHATTREE_BUNDLED_RIPGREP" in spec_text
        assert '"tools/ripgrep"' in spec_text
        assert "CHATTREE_BUNDLED_SEARXNG" in spec_text
        assert '"tools/searxng"' in spec_text
        assert "binaries=binaries" in spec_text
        assert "\"main\"" in spec_text
    server_spec = (REPO_ROOT / "packaging" / "chattree-server.spec").read_text(
        encoding="utf-8"
    )
    assert "\"backend.server_cli\"" in server_spec
    assert "\"backend.core.model.providers.openai_compatible\"" in server_spec
    assert "\"backend.core.model.providers.anthropic_provider\"" in server_spec
    assert "\"backend.core.model.providers.gemini_provider\"" in server_spec
    assert "\"chattree_protocol.http_errors\"" in server_spec


def test_build_script_constructs_pyinstaller_command(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    python = tmp_path / "venv" / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    ripgrep_binary = tmp_path / ("rg.exe" if os.name == "nt" else "rg")
    ripgrep_binary.write_text("", encoding="utf-8")

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs)))

    monkeypatch.setattr(build_server_binary.subprocess, "run", fake_run)

    build_server_binary.run_pyinstaller(
        python,
        dist_dir=tmp_path / "dist",
        work_dir=tmp_path / "work",
        clean=True,
        one_dir=False,
        ripgrep_binary=ripgrep_binary,
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
    assert env["CHATTREE_BUNDLED_RIPGREP"] == str(ripgrep_binary)


def test_build_script_downloads_verified_ripgrep_archive(tmp_path, monkeypatch):
    executable_name = "rg.exe" if sys.platform == "win32" else "rg"
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr(f"ripgrep-test/{executable_name}", b"bundled-ripgrep")
    archive_bytes = archive_buffer.getvalue()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    arch = "arm64" if build_server_binary.platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    requests = []
    monkeypatch.setattr(build_server_binary, "RIPGREP_VERSION", "test-version")
    monkeypatch.setattr(
        build_server_binary,
        "RIPGREP_ASSETS",
        {(sys.platform, arch): ("test-target.zip", digest)},
    )

    def fake_urlopen(request, **_kwargs):
        requests.append(request.full_url)
        return io.BytesIO(archive_bytes)

    monkeypatch.setattr(
        build_server_binary.urllib.request,
        "urlopen",
        fake_urlopen,
    )

    binary = build_server_binary.prepare_bundled_ripgrep(tmp_path)

    assert binary.read_bytes() == b"bundled-ripgrep"
    assert requests == [
        "https://github.com/BurntSushi/ripgrep/releases/download/test-version/"
        "ripgrep-test-version-test-target.zip"
    ]


def test_build_script_downloads_verified_searxng_binary(tmp_path, monkeypatch):
    payload = b"bundled-searxng"
    digest = hashlib.sha256(payload).hexdigest()
    arch = "arm64" if build_server_binary.platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    requests = []
    monkeypatch.setattr(build_server_binary, "SEARXNG_VERSION", "test-version")
    monkeypatch.setattr(
        build_server_binary,
        "SEARXNG_ASSETS",
        {(sys.platform, arch): ("test-asset.exe", digest)},
    )

    def fake_urlopen(request, **_kwargs):
        requests.append(request.full_url)
        return io.BytesIO(payload)

    monkeypatch.setattr(build_server_binary.urllib.request, "urlopen", fake_urlopen)

    binary = build_server_binary.prepare_bundled_searxng(tmp_path)

    assert binary.read_bytes() == payload
    executable_name = "searxng-server.exe" if sys.platform == "win32" else "searxng-server"
    assert binary.name == executable_name
    assert requests == [
        "https://github.com/dasdkslkd/searxng/releases/download/test-version/test-asset.exe"
    ]


def test_build_script_rejects_unsupported_searxng_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(build_server_binary, "SEARXNG_ASSETS", {})

    with pytest.raises(SystemExit, match="unsupported searxng"):
        build_server_binary.prepare_bundled_searxng(tmp_path)


def test_run_pyinstaller_bundles_searxng_when_provided(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    ripgrep_binary = tmp_path / ("rg.exe" if os.name == "nt" else "rg")
    searxng_binary = tmp_path / ("searxng-server.exe" if os.name == "nt" else "searxng-server")

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs)))

    monkeypatch.setattr(build_server_binary.subprocess, "run", fake_run)

    build_server_binary.run_pyinstaller(
        python,
        dist_dir=tmp_path / "dist",
        work_dir=tmp_path / "work",
        clean=False,
        one_dir=True,
        ripgrep_binary=ripgrep_binary,
        searxng_binary=searxng_binary,
    )

    env = calls[0][1]["env"]
    assert env["CHATTREE_PYINSTALLER_ONE_DIR"] == "1"
    assert env["CHATTREE_BUNDLED_RIPGREP"] == str(ripgrep_binary)
    assert env["CHATTREE_BUNDLED_SEARXNG"] == str(searxng_binary)


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
