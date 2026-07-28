"""GET /conversations/{id}/tree 端点集成测试。

回归守护：确保 ChatManager.canonical_messages_by_node 公共方法正确工作，
防止模块级函数被误当实例方法调用导致 500。
"""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, ".")
sys.path.insert(0, "test")

from backend.api.routes.conversations import router as conversations_router
from backend.core.chat.chat_manager import ChatManager
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from test_chat_manager_prompt_slash import collect_chunks


def _make_app(tmp_path: Path) -> tuple[FastAPI, ChatManager]:
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    manager = ChatManager(
        _DummyModelManager(),
        ChatStorage(str(tmp_path / "conversations")),
        PromptStorage(str(tmp_path / "prompts")),
        chat_repository=repository,
    )
    app = FastAPI()
    app.state.chat_manager = manager
    app.state.config_manager = None
    app.state.run_manager = None
    app.state.transcript_assembler = None
    app.include_router(conversations_router, prefix="/api/v1")
    return app, manager


class _DummyModelManager:
    def __init__(self):
        from test_chat_manager_prompt_slash import CapturingModelManager
        self._inner = CapturingModelManager()
        self.model_list = self._inner.model_list
        self.provider = self._inner.provider

    def get_model(self, *args, **kwargs):
        return self._inner.get_model(*args, **kwargs)

    def get_model_metadata(self, *args, **kwargs):
        return self._inner.get_model_metadata(*args, **kwargs)


def test_tree_endpoint_returns_200_with_usage(tmp_path: Path):
    """tree 端点必须返回 200 且节点包含 usage 字段。"""
    app, manager = _make_app(tmp_path)
    conversation = manager.create_conversation("tree test")

    asyncio.run(collect_chunks(manager.send_message_stream(
        conversation.metadata["id"],
        "hello tree",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    )))

    client = TestClient(app)
    response = client.get(f"/api/v1/conversations/{conversation.metadata['id']}/tree")

    assert response.status_code == 200
    body = response.json()
    assert "root_node_id" in body
    assert "current_node_id" in body
    assert "nodes" in body
    assert isinstance(body["nodes"], list)
    assert len(body["nodes"]) >= 2

    for node in body["nodes"]:
        assert "id" in node
        assert "parent_id" in node
        assert "children_ids" in node
        assert "usage" in node
        usage = node["usage"]
        assert usage is not None
        assert "turn_usage" in usage
        assert "branch_usage" in usage
        assert "active_context_usage" in usage
        assert "model_context_window" in usage


def test_tree_endpoint_404_for_missing_conversation(tmp_path: Path):
    """不存在的会话 ID 应返回 404 而非 500。"""
    app, _manager = _make_app(tmp_path)
    client = TestClient(app)
    response = client.get("/api/v1/conversations/nonexistent-id/tree")
    assert response.status_code == 404
