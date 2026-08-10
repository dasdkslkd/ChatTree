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


def _project_client(project_roots):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(files_routes.router, prefix="/api/v1")
    config_manager = SimpleNamespace(data={"projects": {root: {} for root in project_roots}})

    async def override_get_config_manager():
        return config_manager

    app.dependency_overrides[files_routes.get_config_manager] = override_get_config_manager
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


def test_open_file_resolves_relative_path_against_cwd(monkeypatch, tmp_path):
    target = tmp_path / "sub" / "notes.md"
    target.parent.mkdir()
    target.write_text("hello", encoding="utf-8")
    opened: list[str] = []

    def fake_startfile(path: str) -> None:
        opened.append(path)

    monkeypatch.setattr(files_routes.sys, "platform", "win32")
    monkeypatch.setattr(files_routes.os, "startfile", fake_startfile)
    monkeypatch.chdir(tmp_path)

    response = _client().post("/api/v1/files/open", json={"path": "sub/notes.md"})

    assert response.status_code == 200
    assert response.json() == {"path": str(target.resolve())}
    assert opened == [str(target.resolve())]


def test_open_file_rejects_non_path_text():
    response = _client().post("/api/v1/files/open", json={"path": "plain text"})

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


def test_list_directory_lists_only_project_workspace(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x = 1", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".git").mkdir()
    (root / "readme.md").write_text("hi", encoding="utf-8")

    response = _project_client([str(root)]).get("/api/v1/files/list", params={"path": str(root)})

    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()["entries"]]
    assert names == ["src", "readme.md"]


def test_list_directory_rejects_path_outside_workspace(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()

    response = _project_client([str(root)]).get("/api/v1/files/list", params={"path": str(outside)})

    assert response.status_code == 403


def test_read_file_content_within_workspace(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "app.py"
    target.write_text("print('hi')", encoding="utf-8")

    response = _project_client([str(root)]).get("/api/v1/files/content", params={"path": str(target)})

    assert response.status_code == 200
    body = response.json()
    assert body["binary"] is False
    assert body["content"] == "print('hi')"


def test_read_binary_file_flags_binary(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "data.bin"
    target.write_bytes(b"\x00\x01\x02")

    response = _project_client([str(root)]).get("/api/v1/files/content", params={"path": str(target)})

    assert response.status_code == 200
    assert response.json()["binary"] is True


def test_read_file_rejects_path_outside_workspace(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    response = _project_client([str(root)]).get("/api/v1/files/content", params={"path": str(outside)})

    assert response.status_code == 403


def test_rename_file(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "old.py"
    target.write_text("x = 1", encoding="utf-8")

    response = _project_client([str(root)]).post(
        "/api/v1/files/rename", json={"path": str(target), "new_name": "new.py"}
    )

    assert response.status_code == 200
    assert response.json()["path"].endswith("new.py")
    assert not target.exists()
    assert (root / "new.py").exists()


def test_rename_rejects_name_with_separator(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "old.py"
    target.write_text("x = 1", encoding="utf-8")

    response = _project_client([str(root)]).post(
        "/api/v1/files/rename", json={"path": str(target), "new_name": "a/b"}
    )

    assert response.status_code == 400


def test_rename_rejects_workspace_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()

    response = _project_client([str(root)]).post(
        "/api/v1/files/rename", json={"path": str(root), "new_name": "proj2"}
    )

    assert response.status_code == 403


def test_delete_file(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "drop.py"
    target.write_text("x = 1", encoding="utf-8")

    response = _project_client([str(root)]).post("/api/v1/files/delete", json={"path": str(target)})

    assert response.status_code == 200
    assert not target.exists()


def test_delete_directory_recursive(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "pkg"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "mod.py").write_text("x = 1", encoding="utf-8")

    response = _project_client([str(root)]).post("/api/v1/files/delete", json={"path": str(target)})

    assert response.status_code == 200
    assert not target.exists()


def test_delete_rejects_workspace_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()

    response = _project_client([str(root)]).post("/api/v1/files/delete", json={"path": str(root)})

    assert response.status_code == 403
    assert root.exists()


def test_reveal_file(tmp_path, monkeypatch):
    import subprocess as _subprocess

    calls = []
    monkeypatch.setattr(_subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "view.py"
    target.write_text("x = 1", encoding="utf-8")

    response = _project_client([str(root)]).post("/api/v1/files/reveal", json={"path": str(target)})

    assert response.status_code == 200
    assert calls and str(target) in calls[0]
