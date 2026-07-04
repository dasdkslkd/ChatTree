import json

import pytest

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository


def test_repository_creates_conversation_node_and_messages(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)

    conv_id = repo.create_conversation(title="New chat")
    root_id = repo.create_node(conv_id, parent_id=None, child_order=0)
    user_id = repo.add_message(conv_id, root_id, role="user", content="hello")
    assistant_id = repo.add_message(
        conv_id, root_id, role="assistant", content="world"
    )

    conversation = repo.get_conversation(conv_id)
    messages = repo.list_node_messages(root_id)

    assert conversation["root_node_id"] == root_id
    assert conversation["current_node_id"] == root_id
    assert [m["id"] for m in messages] == [user_id, assistant_id]
    assert messages[0]["preview"] == "hello"


def test_repository_stores_large_message_as_blob(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    conv_id = repo.create_conversation(title="Large")
    node_id = repo.create_node(conv_id, parent_id=None, child_order=0)
    content = "x" * 20000

    message_id = repo.add_message(
        conv_id, node_id, role="assistant", content=content
    )
    message = repo.get_message(message_id)

    assert message["content_inline"] is None
    assert message["content_blob_id"]
    assert message["preview"] == content[:4096]
    assert repo.get_message_content(message_id) == content


def test_repository_rejects_second_root_without_moving_current_node(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    conv_id = repo.create_conversation(title="Single root")
    root_id = repo.create_node(conv_id, parent_id=None, child_order=0)

    with pytest.raises(ValueError):
        repo.create_node(conv_id, parent_id=None, child_order=1)

    conversation = repo.get_conversation(conv_id)
    assert conversation["root_node_id"] == root_id
    assert conversation["current_node_id"] == root_id


def test_add_tool_call_preserves_metadata_when_result_placeholder_conflicts(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    conv_id = repo.create_conversation(title="Tool call upsert")
    node_id = repo.create_node(conv_id, parent_id=None, child_order=0)

    repo.add_tool_call(
        conv_id,
        node_id,
        tool_call_id="call_2",
        name="second_tool",
        arguments='{"b":2}',
        call_index=1,
    )
    repo.add_tool_call(
        conv_id,
        node_id,
        tool_call_id="call_2",
        name="",
        arguments=None,
        call_index=0,
    )

    with persistence.connect() as conn:
        row = conn.execute(
            """
            SELECT call_index, name, args_inline
            FROM tool_calls
            WHERE conversation_id = ? AND id = ?
            """,
            (conv_id, "call_2"),
        ).fetchone()

    assert repo.tool_call_exists(conv_id, "call_2")
    assert row["call_index"] == 1
    assert row["name"] == "second_tool"
    assert json.loads(row["args_inline"]) == {"b": 2}


def test_tool_call_ids_can_repeat_across_conversations_without_metadata_bleed(
    tmp_path,
):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    conv_a = repo.create_conversation(title="Tool call A")
    node_a = repo.create_node(conv_a, parent_id=None, child_order=0)
    conv_b = repo.create_conversation(title="Tool call B")
    node_b = repo.create_node(conv_b, parent_id=None, child_order=0)

    repo.add_tool_call(
        conv_a,
        node_a,
        tool_call_id="call_0",
        name="first_tool",
        arguments={"conversation": "a"},
        call_index=2,
    )
    repo.add_tool_result(
        conv_a,
        node_a,
        tool_result_id="result-a",
        tool_call_id="call_0",
        output="result a",
        metadata={"conversation": "a"},
    )
    repo.add_tool_call(
        conv_b,
        node_b,
        tool_call_id="call_0",
        name="second_tool",
        arguments={"conversation": "b"},
        call_index=5,
    )
    repo.add_tool_result(
        conv_b,
        node_b,
        tool_result_id="result-b",
        tool_call_id="call_0",
        output="result b",
        metadata={"conversation": "b"},
    )

    with persistence.connect() as conn:
        calls = conn.execute(
            """
            SELECT conversation_id, call_index, name, args_inline
            FROM tool_calls
            WHERE id = ?
            ORDER BY conversation_id
            """,
            ("call_0",),
        ).fetchall()
        results = conn.execute(
            """
            SELECT conversation_id, tool_call_id, output_preview, metadata_json
            FROM tool_results
            WHERE id IN (?, ?)
            ORDER BY conversation_id
            """,
            ("result-a", "result-b"),
        ).fetchall()

    calls_by_conversation = {
        row["conversation_id"]: (
            row["call_index"],
            row["name"],
            json.loads(row["args_inline"]),
        )
        for row in calls
    }
    results_by_conversation = {
        row["conversation_id"]: (
            row["tool_call_id"],
            row["output_preview"],
            json.loads(row["metadata_json"]),
        )
        for row in results
    }

    assert calls_by_conversation == {
        conv_a: (2, "first_tool", {"conversation": "a"}),
        conv_b: (5, "second_tool", {"conversation": "b"}),
    }
    assert results_by_conversation == {
        conv_a: ("call_0", "result a", {"conversation": "a"}),
        conv_b: ("call_0", "result b", {"conversation": "b"}),
    }
