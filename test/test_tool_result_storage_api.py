import json
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, ".")

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
