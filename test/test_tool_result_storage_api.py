import json
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, ".")

from backend.api.dependencies import get_tool_manager
from backend.api.routes import tool_results as tool_results_route
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.storage.tool_result_storage import ToolResultStorage
from main import app


class FakeToolManager:
    def __init__(self, tool_result_store):
        self.tool_result_store = tool_result_store


def test_tool_result_storage_reads_default_slice_and_preserves_metadata(tmp_path):
    store = ToolResultStorage(str(tmp_path))

    record = store.save_result(
        content="x" * 17005,
        tool_name="shell",
        conversation_id="conv-1",
        node_id="node-1",
        tool_call_id="call-1",
        structured_metadata={"format": "text"},
        raw_metadata={"mime_type": "text/plain"},
    )

    persisted = store.read_result(record["id"])
    assert persisted["structured_metadata"] == {"format": "text"}
    assert persisted["raw_metadata"] == {"mime_type": "text/plain"}

    first_page = store.read_slice(record["id"])
    assert first_page == {
        "tool_result_id": record["id"],
        "tool_name": "shell",
        "offset": 0,
        "limit": 16000,
        "next_offset": 16000,
        "total_chars": 17005,
        "has_more": True,
        "content": "x" * 16000,
    }

    final_page = store.read_slice(record["id"], offset=16000, limit=2000)
    assert final_page["content"] == "x" * 1005
    assert final_page["next_offset"] is None
    assert final_page["has_more"] is False


def test_tool_result_storage_reads_legacy_records_without_new_metadata(tmp_path):
    legacy_id = "legacy-result"
    legacy_path = tmp_path / f"{legacy_id}.json"
    legacy_path.write_text(
        json.dumps(
            {
                "id": legacy_id,
                "tool_name": "legacy_tool",
                "conversation_id": "conv-legacy",
                "node_id": "node-legacy",
                "tool_call_id": None,
                "content": "abcdef",
                "created_at": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                legacy_id: {
                    "id": legacy_id,
                    "path": str(legacy_path),
                    "tool_name": "legacy_tool",
                    "conversation_id": "conv-legacy",
                    "node_id": "node-legacy",
                    "tool_call_id": None,
                    "created_at": 1,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ToolResultStorage(str(tmp_path))
    result = store.read_slice(legacy_id, offset=2, limit=3)

    assert result["tool_result_id"] == legacy_id
    assert result["tool_name"] == "legacy_tool"
    assert result["content"] == "cde"
    assert result["total_chars"] == 6


def test_tool_result_route_reads_paginated_content_and_returns_404(tmp_path):
    store = ToolResultStorage(str(tmp_path))
    record = store.save_result(
        content="0123456789" * 2000,
        tool_name="python",
        conversation_id="conv-2",
        node_id="node-2",
        tool_call_id=None,
    )

    had_tool_manager = hasattr(app.state, "tool_manager")
    previous_tool_manager = getattr(app.state, "tool_manager", None)
    app.state.tool_manager = FakeToolManager(store)
    try:
        client = TestClient(app)

        response = client.get(f"/tool-results/{record['id']}")
        assert response.status_code == 200
        assert response.json() == {
            "tool_result_id": record["id"],
            "tool_name": "python",
            "offset": 0,
            "limit": 16000,
            "next_offset": 16000,
            "total_chars": 20000,
            "has_more": True,
            "content": "0123456789" * 1600,
        }

        api_response = client.get(f"/api/tool-results/{record['id']}?offset=16000&limit=5")
        assert api_response.status_code == 200
        assert api_response.json()["content"] == "01234"
        assert api_response.json()["next_offset"] == 16005

        missing_response = client.get("/tool-results/missing")
        assert missing_response.status_code == 404
    finally:
        if had_tool_manager:
            app.state.tool_manager = previous_tool_manager
        else:
            delattr(app.state, "tool_manager")


def test_tool_result_route_respects_tool_manager_dependency_override(tmp_path):
    override_store = ToolResultStorage(str(tmp_path / "override"))
    record = override_store.save_result(
        content="override content",
        tool_name="shell",
        conversation_id="conv-override",
        node_id="node-override",
        tool_call_id="call-override",
    )
    state_store = ToolResultStorage(str(tmp_path / "state"))

    previous_overrides = dict(app.dependency_overrides)
    had_tool_manager = hasattr(app.state, "tool_manager")
    previous_tool_manager = getattr(app.state, "tool_manager", None)
    had_persistence = hasattr(app.state, "persistence")
    previous_persistence = getattr(app.state, "persistence", None)
    app.state.tool_manager = FakeToolManager(state_store)
    if had_persistence:
        delattr(app.state, "persistence")
    app.dependency_overrides[get_tool_manager] = lambda: FakeToolManager(override_store)
    try:
        client = TestClient(app)

        response = client.get(f"/tool-results/{record['id']}")

        assert response.status_code == 200
        assert response.json()["content"] == "override content"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        if had_tool_manager:
            app.state.tool_manager = previous_tool_manager
        else:
            delattr(app.state, "tool_manager")
        if had_persistence:
            app.state.persistence = previous_persistence


def test_tool_result_route_returns_clear_error_for_missing_sqlite_blob(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Missing blob")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    with persistence.connect() as conn:
        conn.execute(
            """
            INSERT INTO blobs (
              id,
              path,
              mime_type,
              compression,
              byte_size,
              stored_size,
              char_count,
              ref_count,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """,
            (
                "missing-blob-id",
                "blobs/missing/missing-blob-id.gz",
                "text/plain; charset=utf-8",
                "gzip",
                128,
                64,
                128,
                1,
            ),
        )
        conn.execute(
            """
            INSERT INTO tool_calls (
              id,
              conversation_id,
              node_id,
              call_index,
              name,
              status,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            """,
            ("call-missing-blob", conversation_id, node_id, 0, "shell", "complete"),
        )
        conn.execute(
            """
            INSERT INTO tool_results (
              id,
              conversation_id,
              node_id,
              tool_call_id,
              status,
              output_preview,
              output_blob_id,
              output_size,
              created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """,
            (
                "tool-result-missing-blob",
                conversation_id,
                node_id,
                "call-missing-blob",
                "complete",
                "preview",
                "missing-blob-id",
                128,
            ),
        )
    route_app = FastAPI()
    route_app.include_router(tool_results_route.router)
    route_app.state.persistence = persistence
    client = TestClient(route_app, raise_server_exceptions=False)

    response = client.get("/tool-results/tool-result-missing-blob")

    assert response.status_code == 404
    assert response.json()["detail"] == "工具结果 blob 不存在"
