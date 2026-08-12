import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import config as config_route
from backend.api.routes import memory as memory_route
from backend.core.config.config import Config
from backend.core.memory import MemoryStore


def _app(tmp_path, project_id: str):
    config = Config(str(tmp_path / "config.json"), load_from_disk=False)
    config.data["projects"] = {
        str(tmp_path / "project"): {
            "id": project_id,
            "roots": [str(tmp_path / "project")],
            "label": "Project",
        },
    }
    config.save()
    app = FastAPI()
    app.state.config_manager = config
    app.state.memory_store = MemoryStore(tmp_path)
    app.include_router(memory_route.router)
    app.include_router(config_route.router)
    return app, config


def test_memory_route_returns_global_and_selected_project(tmp_path):
    project_id = str(uuid.uuid4())
    root = tmp_path / "memories"
    (root / "projects").mkdir(parents=True)
    (root / "USER.md").write_text("# User Memory\n\n- global fact\n", encoding="utf-8")
    (root / "projects" / f"{project_id}.md").write_text(
        "# Project Memory\n\n- project fact\n",
        encoding="utf-8",
    )
    app, _ = _app(tmp_path, project_id)

    response = TestClient(app).get("/memory", params={"project_id": project_id})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "- global fact" in response.json()["global"]["user"]["content"]
    assert "- project fact" in response.json()["project"]["content"]
    assert "entries" not in response.json()["project"]


def test_memory_route_rejects_unknown_project(tmp_path):
    app, _ = _app(tmp_path, str(uuid.uuid4()))

    response = TestClient(app).get("/memory", params={"project_id": str(uuid.uuid4())})

    assert response.status_code == 404


def test_memory_config_updates_without_rebuilding_runtime_managers_and_rejects_extra_fields(tmp_path, monkeypatch):
    project_id = str(uuid.uuid4())
    app, config = _app(tmp_path, project_id)
    tool_manager = SimpleNamespace(_config=config.data)
    app.state.tool_manager = tool_manager
    monkeypatch.setattr(config_route, "cfg", SimpleNamespace())
    client = TestClient(app)

    response = client.put("/config", json={"memory": {"enabled": False}})

    assert response.status_code == 200
    assert config.data["memory"] == {"enabled": False}
    assert tool_manager._config["memory"] == {"enabled": False}
    assert client.get("/memory").json()["enabled"] is False
    assert client.put(
        "/config",
        json={"memory": {"enabled": True, "unknown": True}},
    ).status_code == 422
