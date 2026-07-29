import json
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.routes.conversations import router
from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.conversation import Conversation
from backend.core.config.types import SCHEMA_VERSION
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.workspace import WorkspaceContext, build_default_workspace, normalize_workspace


class DummyModelManager:
    model_list = {}


def make_chat_manager(tmp_path: Path) -> ChatManager:
    return ChatManager(
        DummyModelManager(),
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts.json")),
    )


def test_normalize_workspace_uses_cwd_as_default_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    workspace = normalize_workspace({"cwd": str(project)})

    assert workspace["cwd"] == str(project.resolve())
    assert workspace["workspace_roots"] == [str(project.resolve())]
    assert ".git" in workspace["protected_paths"]
    assert workspace["label"] == "project"


def test_chat_manager_persists_workspace_and_lists_summary(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    manager = make_chat_manager(tmp_path)

    conversation = manager.create_conversation(
        "工作区对话",
        workspace={"cwd": str(project), "label": "Project A"},
    )

    loaded = manager.get_conversation(conversation.metadata["id"])
    assert loaded is not None
    assert loaded.metadata["workspace"]["cwd"] == str(project.resolve())
    assert loaded.metadata["workspace"]["label"] == "Project A"

    listed = manager.list_conversations()
    assert len(listed) == 1
    assert listed[0]["id"] == conversation.metadata["id"]
    assert listed[0]["title"] == "工作区对话"
    assert listed[0]["updated_at"] == conversation.metadata["updated_at"]
    assert listed[0]["node_count"] == "1"
    assert listed[0]["model_id"] == ""
    assert listed[0]["provider_id"] == ""
    assert listed[0]["workspace"] == loaded.metadata["workspace"]
    assert listed[0]["current_node_id"] == conversation.current_node_id


def test_old_conversation_without_workspace_gets_runtime_default(tmp_path):
    default_project = tmp_path / "default"
    default_project.mkdir()
    metadata = {
        "id": "conv-old",
        "title": "旧对话",
        "created_at": 1,
        "updated_at": 1,
        "total_tokens": {},
        "schema_version": SCHEMA_VERSION,
    }
    conversation = Conversation.from_dict({
        "metadata": metadata,
        "nodes": [{
            "id": "root",
            "parent_id": "None",
            "children_ids": [],
            "timestamp": 1,
            "model_id": None,
            "tool_permission_mode": None,
            "task_context_mode": "attached",
            "total_tokens": 0,
            "branch_usage_info": {"total_tokens": 0},
            "usage": {
                "turn_usage": {"total_tokens": 0},
                "branch_usage": {"total_tokens": 0},
                "active_context_usage": {"total_tokens": 0},
            },
        }],
        "root_node_id": "root",
        "current_node_id": "root",
    })

    workspace = build_default_workspace({"tools": {"code": {"workspace_roots": [str(default_project)]}}})
    assert "workspace" not in conversation.metadata
    assert workspace["cwd"] == str(default_project.resolve())


def test_create_conversation_api_accepts_workspace_and_returns_conversation(tmp_path):
    project = tmp_path / "api-project"
    project.mkdir()
    manager = make_chat_manager(tmp_path)

    from backend.api.dependencies import get_chat_manager

    async def override_manager():
        return manager

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = override_manager
    client = TestClient(app)

    response = client.post(
        "/conversations",
        json={"title": "API", "workspace": {"cwd": str(project), "label": "API Project"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "API"
    assert body["workspace"]["cwd"] == str(project.resolve())
    assert body["workspace"]["label"] == "API Project"
    assert body["current_node_id"]


def test_project_api_creates_new_folder_and_returns_workspace(tmp_path):
    manager = make_chat_manager(tmp_path)

    from backend.api.dependencies import get_chat_manager

    async def override_manager():
        return manager

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = override_manager
    client = TestClient(app)

    target = tmp_path / "projects" / "new-project"
    target.parent.mkdir()
    response = client.post(
        "/projects/folders",
        json={"path": str(target), "label": "New Project"},
    )

    assert response.status_code == 200
    assert target.is_dir()
    body = response.json()
    assert body["cwd"] == str(target.resolve())
    assert body["workspace_roots"] == [str(target.resolve())]
    assert body["label"] == "New Project"


def test_project_api_resolves_existing_folder_without_creating(tmp_path):
    manager = make_chat_manager(tmp_path)

    from backend.api.dependencies import get_chat_manager

    async def override_manager():
        return manager

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = override_manager
    client = TestClient(app)

    existing = tmp_path / "existing"
    existing.mkdir()
    response = client.post(
        "/projects/folders/resolve",
        json={"path": str(existing)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cwd"] == str(existing.resolve())
    assert body["label"] == "existing"


def test_project_api_rejects_missing_existing_folder(tmp_path):
    manager = make_chat_manager(tmp_path)

    from backend.api.dependencies import get_chat_manager

    async def override_manager():
        return manager

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = override_manager
    client = TestClient(app)

    response = client.post(
        "/projects/folders/resolve",
        json={"path": str(tmp_path / "missing")},
    )

    assert response.status_code == 400
