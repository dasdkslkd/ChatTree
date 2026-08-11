"""dev_environment 配置归一化、解析、提示词注入与命令 PATH 注入测试。"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.conversations as conversations_route
from backend.api.errors import install_error_handlers
from backend.api.routes import config as config_route
from backend.core import projects as projects_mod
from backend.core.command_runtime import _command_env
from backend.core.config.config import cfg
from backend.core.projects import (
    detect_tool_path,
    normalize_dev_environment,
    normalize_projects_config,
    resolve_dev_environment,
)
from backend.core.prompts.runtime_context import build_dev_environment_section


def _make_interpreter(tmp_path, name):
    interpreter = tmp_path / name / ("python.exe" if os.name == "nt" else "python")
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_bytes(b"")
    return interpreter


def test_normalize_dev_environment_expands_and_drops_empty():
    normalized = normalize_dev_environment({
        "tools": {" Python ": "  ", "git": "~/tools/git"},
        "environments": {"web": "~/envs/web/python"},
        "default_environment": " web ",
    })
    assert set(normalized["tools"]) == {"git"}  # 空路径被丢弃，键小写化
    assert set(normalized["environments"]) == {"web"}
    assert normalized["default_environment"] == "web"
    assert "~" not in normalized["environments"]["web"]


def test_normalize_project_config_carries_dev_environment():
    project = normalize_projects_config({
        "/tmp/proj": {"dev_environment": {"tools": {"node": "~/node"}}},
    })
    entry = project[list(project)[0]]
    assert set(entry["dev_environment"]["tools"]) == {"node"}


def test_resolve_merges_project_over_global(tmp_path, monkeypatch):
    global_python = _make_interpreter(tmp_path, "base")
    project_python = _make_interpreter(tmp_path, "projenv")
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: None)
    config = {
        "dev_environment": {"tools": {"python": str(global_python), "git": str(global_python)}},
        "projects": {
            str(tmp_path): {
                "dev_environment": {"tools": {"python": str(project_python)}},
            },
        },
    }
    resolved = resolve_dev_environment(config, str(tmp_path))
    assert resolved["default_python"] == str(project_python)
    directories = {str(p) for p in (global_python.parent, project_python.parent)}
    assert set(resolved["path_dirs"]) == directories


def test_resolve_default_environment_fallback_and_missing_paths(tmp_path, monkeypatch):
    existing = _make_interpreter(tmp_path, "web")
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: None)
    config = {
        "dev_environment": {
            "environments": {
                "web": str(existing),
                "ghost": str(tmp_path / "ghost" / "python.exe"),
            },
            "default_environment": "ghost",
        },
    }
    resolved = resolve_dev_environment(config, str(tmp_path))
    # ghost 不存在 → 回退第一个存在的环境；并列环境过滤掉不存在项
    assert resolved["default_environment"] == "web"
    assert resolved["default_python"] == str(existing)
    assert resolved["parallel_environments"] == {}


def test_resolve_skips_injection_when_matches_path_detection(tmp_path, monkeypatch):
    detected = _make_interpreter(tmp_path, "detected")
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: str(detected))
    config = {"dev_environment": {"tools": {"python": str(detected)}}}
    resolved = resolve_dev_environment(config, str(tmp_path))
    assert resolved["path_dirs"] == []


def test_build_section_requires_parallel_environments(tmp_path, monkeypatch):
    default = _make_interpreter(tmp_path, "web")
    parallel = _make_interpreter(tmp_path, "worker")
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: None)
    config = {
        "dev_environment": {
            "environments": {"web": str(default), "worker": str(parallel)},
            "default_environment": "web",
        },
    }
    section = build_dev_environment_section({"cwd": str(tmp_path)}, config)
    assert section is not None
    assert "Default environment `web`" in section.content
    assert f"worker: {parallel}" in section.content
    assert "Do NOT probe PATH" in section.content

    single = {"dev_environment": {"tools": {"python": str(default)}}}
    assert build_dev_environment_section({"cwd": str(tmp_path)}, single) is None
    assert build_dev_environment_section(None, config) is None


def test_command_env_prepends_path_dirs(monkeypatch, tmp_path):
    import backend.core.command_runtime as command_runtime

    injected = str(tmp_path / "inject")
    monkeypatch.setattr(cfg, "data", {"dev_environment": {}})
    monkeypatch.setattr(
        command_runtime,
        "resolve_dev_environment",
        lambda config_data, cwd: {"path_dirs": [injected]},
    )
    env = _command_env(str(tmp_path))
    path_key = next(key for key in env if key.upper() == "PATH")
    assert env[path_key].startswith(injected)

    monkeypatch.setattr(
        command_runtime,
        "resolve_dev_environment",
        lambda config_data, cwd: {"path_dirs": []},
    )
    baseline = _command_env(str(tmp_path))
    assert baseline[path_key] == os.environ.get(path_key, "")


def test_detect_tool_path_lowercases_uppercase_pathext(monkeypatch):
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: f"C:/tools/{name}.EXE")
    assert detect_tool_path("python") == str(Path("C:/tools/python.exe"))
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: None)
    assert detect_tool_path("python") == ""


def test_detected_endpoint_returns_path_lookups(monkeypatch):
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: f"/usr/bin/{name}.EXE")
    result = asyncio.run(config_route.get_dev_environment_detected())
    assert result["python"] == str(Path("/usr/bin/python.exe"))
    assert set(result) == set(config_route.DEV_ENVIRONMENT_DETECT_TOOLS)


def test_delete_project_removes_config_and_history(monkeypatch, tmp_path):
    project_a = str(tmp_path / "proj_a")
    project_b = str(tmp_path / "proj_b")
    config_data = {
        "provider": {},
        "default_provider": "",
        "default_model": "",
        "projects": {project_a: {"label": "A"}, project_b: {"label": "B"}},
    }
    app = FastAPI()
    app.state.config_manager = SimpleNamespace(data=config_data, save=lambda: None)
    deleted: list[str] = []
    app.state.chat_manager = SimpleNamespace(
        list_conversations=lambda: [{
            "id": "c1",
            "workspace": {"cwd": project_a, "workspace_roots": [project_a]},
        }],
        delete_conversation=lambda cid: deleted.append(cid),
    )
    app.state.run_manager = SimpleNamespace(
        list_active=lambda cid: [],
        request_stop=lambda rid: None,
    )
    app.state.tool_manager = SimpleNamespace(_config=config_data)
    # 避免写入全局 cfg 单例
    monkeypatch.setattr(conversations_route, "cfg", SimpleNamespace())
    install_error_handlers(app)
    app.include_router(conversations_route.router, prefix="/api/v1")
    client = TestClient(app)

    resp = client.delete(f"/api/v1/projects/{project_a}")
    assert resp.status_code == 200
    assert resp.json()["deleted_ids"] == ["c1"]
    # 该项目配置被删除，其它项目保留
    assert project_a not in config_data["projects"]
    assert project_b in config_data["projects"]
