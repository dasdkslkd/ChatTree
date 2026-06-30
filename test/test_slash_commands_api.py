import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, ".")

from backend.api.routes import slash


def test_list_slash_commands_exposes_registry_without_side():
    app = FastAPI()
    app.include_router(slash.router)

    response = TestClient(app).get("/slash/commands")

    assert response.status_code == 200
    commands = response.json()
    names = [command["name"] for command in commands]
    assert names == ["init", "review", "btw", "fork", "workflow"]
    assert "side" not in names

    btw = next(command for command in commands if command["name"] == "btw")
    assert btw["dispatch_kind"] == "side_question"
    assert btw["run_kind"] == "side_question"
    assert btw["tool_policy"] == "disabled"
    assert btw["persistence_policy"] == "side_run"
    assert btw["stream_target_policy"] == "anchor_only"
    assert btw["blocks_main_thread"] is False

