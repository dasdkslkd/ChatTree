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
