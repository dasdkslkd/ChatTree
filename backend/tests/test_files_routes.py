import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.files as files_routes
from backend.api.errors import install_error_handlers


def _client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(files_routes.router, prefix="/api/v1")
    return TestClient(app)


def _make_file(tmp_path, name: str):
    target = tmp_path / name
    target.write_text("hello", encoding="utf-8")
    return target


def test_open_file_uses_startfile_on_windows(monkeypatch, tmp_path):
    target = _make_file(tmp_path, "notes.md")
    opened: list[str] = []

    def fake_startfile(path: str) -> None:
        opened.append(path)

    monkeypatch.setattr(files_routes.sys, "platform", "win32")
    monkeypatch.setattr(files_routes.os, "startfile", fake_startfile)

    response = _client().post("/api/v1/files/open", json={"path": str(target)})

    assert response.status_code == 200
    assert response.json() == {"path": str(target.resolve())}
    assert opened == [str(target.resolve())]


def test_open_file_uses_open_command_on_macos(monkeypatch, tmp_path):
    target = _make_file(tmp_path, "notes.md")
    calls: list[list[str]] = []

    def fake_popen(args):
        calls.append(args)
        return SimpleNamespace()

    monkeypatch.setattr(files_routes.sys, "platform", "darwin")
    monkeypatch.setattr(files_routes.subprocess, "Popen", fake_popen)

    response = _client().post("/api/v1/files/open", json={"path": str(target)})

    assert response.status_code == 200
    assert response.json() == {"path": str(target.resolve())}
    assert calls == [["open", str(target.resolve())]]


def test_open_file_rejects_relative_path():
    response = _client().post("/api/v1/files/open", json={"path": "src/main.py"})

    assert response.status_code == 400


def test_open_file_rejects_empty_path():
    response = _client().post("/api/v1/files/open", json={"path": ""})

    assert response.status_code == 422


def test_open_file_returns_not_found_for_missing_path(tmp_path):
    missing = tmp_path / "missing.py"

    response = _client().post("/api/v1/files/open", json={"path": str(missing)})

    assert response.status_code == 404


def test_open_file_reports_open_failure(monkeypatch, tmp_path):
    target = _make_file(tmp_path, "notes.md")

    def fail_startfile(path: str) -> None:
        raise OSError("no association")

    monkeypatch.setattr(files_routes.sys, "platform", "win32")
    monkeypatch.setattr(files_routes.os, "startfile", fail_startfile)

    response = _client().post("/api/v1/files/open", json={"path": str(target)})

    assert response.status_code == 500
