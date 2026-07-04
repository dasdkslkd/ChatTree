from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.transcript import TranscriptProjection


def test_transcript_returns_backend_sorted_items_for_current_branch(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    projection = TranscriptProjection(persistence)
    conv_id = repo.create_conversation(title="Transcript")
    node_id = repo.create_node(conv_id, parent_id=None, child_order=0)
    user_id = repo.add_message(conv_id, node_id, role="user", content="Plan first")
    assistant_id = repo.add_message(
        conv_id, node_id, role="assistant", content="Done"
    )

    projection.upsert_message_item(
        conv_id, node_id, user_id, "user_message", local_order=10
    )
    projection.upsert_message_item(
        conv_id, node_id, assistant_id, "assistant_answer", local_order=30
    )
    plan_item = projection.upsert_plan_card(
        conv_id,
        node_id,
        plan_id="plan-1",
        status="awaiting_approval",
        preview="Plan",
        local_order=40,
    )

    items = projection.list_for_branch(conv_id, tip_node_id=node_id)

    assert [item["item_type"] for item in items] == [
        "user_message",
        "assistant_answer",
        "plan_card",
    ]
    assert items[-1]["id"] == plan_item


def test_transcript_branch_excludes_sibling_items(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    projection = TranscriptProjection(persistence)
    conv_id = repo.create_conversation(title="Branches")
    root = repo.create_node(conv_id, parent_id=None, child_order=0)
    left = repo.create_node(conv_id, parent_id=root, child_order=0)
    right = repo.create_node(conv_id, parent_id=root, child_order=1)

    projection.upsert_plan_card(
        conv_id,
        left,
        plan_id="left-plan",
        status="approved",
        preview="left",
        local_order=10,
    )
    projection.upsert_plan_card(
        conv_id,
        right,
        plan_id="right-plan",
        status="approved",
        preview="right",
        local_order=10,
    )

    items = projection.list_for_branch(conv_id, tip_node_id=left)

    assert [item["plan_id"] for item in items] == ["left-plan"]
