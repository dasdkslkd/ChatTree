import asyncio
import json
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, ".")

from backend.api.routes import tool_results as tool_results_route
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.tools.code import CodeToolConfig, ReadFileTool
from backend.core.tools.tool_manager import ToolManager


def _repository(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Canonical tool result")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    call_id = repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-canonical",
        name="shell",
        arguments={},
    )
    return persistence, repository, conversation_id, node_id, call_id


def _route_client(persistence, repository=None):
    route_app = FastAPI()
    route_app.include_router(tool_results_route.router)
    route_app.state.persistence = persistence
    if repository is not None:
        route_app.state.chat_repository = repository
    return TestClient(route_app, raise_server_exceptions=False)


def test_repository_reads_default_slice_from_canonical_tool_results(tmp_path):
    _persistence, repository, conversation_id, node_id, call_id = _repository(tmp_path)
    result_id = repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-canonical",
        tool_call_id=call_id,
        output="x" * 17005,
        metadata={"tool_name": "shell"},
    )

    first_page = repository.get_tool_result_slice(result_id)
    assert first_page == {
        "tool_result_id": result_id,
        "tool_name": "shell",
        "offset": 0,
        "limit": 16000,
        "next_offset": 16000,
        "total_chars": 17005,
        "has_more": True,
        "content": "x" * 16000,
    }

    final_page = repository.get_tool_result_slice(result_id, offset=16000, limit=2000)
    assert final_page["content"] == "x" * 1005
    assert final_page["next_offset"] is None
    assert final_page["has_more"] is False


def test_tool_result_route_reads_only_canonical_sqlite_columns(tmp_path):
    persistence, repository, conversation_id, node_id, call_id = _repository(tmp_path)
    result_id = repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-canonical",
        tool_call_id=call_id,
        output="canonical output",
    )
    with persistence.connect() as conn:
        conn.execute("ALTER TABLE tool_results ADD COLUMN output_inline TEXT")
        conn.execute(
            "UPDATE tool_results SET output_inline = ? WHERE id = ?",
            ("legacy output", result_id),
        )
    client = _route_client(persistence, repository)

    response = client.get(f"/tool-results/{result_id}")

    assert response.status_code == 200
    assert response.json()["content"] == "canonical output"


def test_tool_result_route_reads_blob_pages_and_returns_404(tmp_path):
    persistence, repository, conversation_id, node_id, call_id = _repository(tmp_path)
    result_id = repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-blob",
        tool_call_id=call_id,
        output="0123456789" * 2000,
        metadata={"tool_name": "python"},
    )
    client = _route_client(persistence)

    response = client.get(f"/tool-results/{result_id}")
    assert response.status_code == 200
    assert response.json() == {
        "tool_result_id": result_id,
        "tool_name": "shell",
        "offset": 0,
        "limit": 16000,
        "next_offset": 16000,
        "total_chars": 20000,
        "has_more": True,
        "content": "0123456789" * 1600,
    }

    api_response = client.get(f"/tool-results/{result_id}?offset=16000&limit=5")
    assert api_response.status_code == 200
    assert api_response.json()["content"] == "01234"
    assert api_response.json()["next_offset"] == 16005

    missing_response = client.get("/tool-results/missing")
    assert missing_response.status_code == 404


def test_read_tool_result_tool_uses_repository_and_errors_without_it(tmp_path):
    _persistence, repository, conversation_id, node_id, call_id = _repository(tmp_path)
    result_id = repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-readable",
        tool_call_id=call_id,
        output="abcdef",
    )
    manager = ToolManager({"tools": {"builtin": {"enabled": False}}}, chat_repository=repository)

    payload = json.loads(asyncio.run(manager.execute_tool("read_tool_result", {
        "tool_result_id": result_id,
        "offset": 2,
        "limit": 3,
    })))
    assert payload == {"content": "cde", "read_more": 'read({"source":"tool_result","tool_result_id":"result-readable","offset":5})'}

    missing_manager = ToolManager({"tools": {"builtin": {"enabled": False}}})
    error = json.loads(asyncio.run(missing_manager.execute_tool("read_tool_result", {
        "tool_result_id": result_id,
    })))
    assert error["error"]["type"] == "tool_result_unavailable"


def test_read_source_tool_result_uses_runtime_repository(tmp_path):
    _persistence, repository, conversation_id, node_id, call_id = _repository(tmp_path)
    result_id = repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-read-source",
        tool_call_id=call_id,
        output="abcdef",
    )
    tool = ReadFileTool(CodeToolConfig.from_dict({"workspace_roots": [str(tmp_path)]}))

    payload = json.loads(asyncio.run(tool.execute(
        source="tool_result",
        tool_result_id=result_id,
        offset=1,
        limit=3,
        _runtime_context={"chat_repository": repository},
    )))
    assert payload["content"] == "bcd"
    assert payload["next_offset"] == 4

    error = json.loads(asyncio.run(tool.execute(source="tool_result", tool_result_id=result_id)))
    assert error["error"]["type"] == "tool_result_unavailable"


def test_tool_result_route_returns_clear_error_for_missing_sqlite_blob(tmp_path):
    persistence, repository, conversation_id, node_id, _call_id = _repository(tmp_path)
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
                "call-canonical",
                "complete",
                "preview",
                "missing-blob-id",
                128,
            ),
        )
    client = _route_client(persistence, repository)

    response = client.get("/tool-results/tool-result-missing-blob")

    assert response.status_code == 404
    assert response.json()["detail"] == "工具结果 blob 不存在"
