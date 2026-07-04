from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import conversations as conversations_route
from backend.api.routes import tool_results as tool_results_route
from backend.core.persistence.blob_store import BlobStore
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.transcript import TranscriptProjection


def client_for(projection: TranscriptProjection) -> TestClient:
    app = FastAPI()
    app.include_router(conversations_route.router)
    app.state.transcript_projection = projection
    return TestClient(app)


def test_conversation_transcript_route_returns_branch_items_in_backend_order(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    projection = TranscriptProjection(persistence)
    conversation_id = repository.create_conversation(title="Transcript route")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    message_id = repository.add_message(
        conversation_id,
        node_id,
        role="user",
        content="hello from sqlite",
    )
    projection.upsert_message_item(
        conversation_id,
        node_id,
        message_id,
        "user_message",
        local_order=10,
        status="complete",
        props={"source": "route-test"},
    )
    client = client_for(projection)

    response = client.get(
        f"/conversations/{conversation_id}/transcript",
        params={"node_id": node_id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["type"] == "user_message"
    assert payload["items"][0]["item_type"] == "user_message"
    assert payload["items"][0]["message_id"] == message_id
    assert payload["items"][0]["node_id"] == node_id
    assert payload["items"][0]["preview"] == "hello from sqlite"
    assert payload["items"][0]["status"] == "complete"
    assert payload["items"][0]["local_order"] == 10
    assert payload["items"][0]["props"] == {"source": "route-test"}


def test_conversation_transcript_route_returns_404_for_unknown_conversation(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    client = client_for(TranscriptProjection(persistence))

    response = client.get("/conversations/missing/transcript")

    assert response.status_code == 404


def test_tool_result_route_falls_back_to_sqlite_blob_result(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation(title="Tool result route")
    node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    blob = BlobStore(persistence).put_text("0123456789")
    with persistence.connect() as conn:
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
            ("call-1", conversation_id, node_id, 0, "shell", "complete"),
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
                "tool-result-1",
                conversation_id,
                node_id,
                "call-1",
                "complete",
                "0123",
                blob.blob_id,
                10,
            ),
        )
    app = FastAPI()
    app.include_router(tool_results_route.router)
    app.state.persistence = persistence
    client = TestClient(app)

    response = client.get("/tool-results/tool-result-1", params={"offset": 3, "limit": 4})

    assert response.status_code == 200
    assert response.json() == {
        "tool_result_id": "tool-result-1",
        "tool_name": "shell",
        "offset": 3,
        "limit": 4,
        "next_offset": 7,
        "total_chars": 10,
        "has_more": True,
        "content": "3456",
    }
