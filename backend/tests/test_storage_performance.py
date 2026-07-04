import time
from pathlib import Path

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.transcript import TranscriptProjection


def test_transcript_query_does_not_load_large_tool_outputs(tmp_path, monkeypatch):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    projection = TranscriptProjection(persistence)
    conv_id = repo.create_conversation(title="Perf")
    parent = None
    for index in range(200):
        node_id = repo.create_node(conv_id, parent_id=parent, child_order=0)
        message_id = repo.add_message(
            conv_id,
            node_id,
            role="assistant",
            content=f"large message {index}\n" + ("x" * 20000),
        )
        projection.upsert_message_item(
            conv_id,
            node_id,
            message_id,
            "assistant_answer",
            local_order=10,
        )
        parent = node_id

    original_read_bytes = Path.read_bytes

    def fail_on_blob_read(path):
        if path.suffix == ".gz" and persistence.blobs_dir in path.parents:
            raise AssertionError(f"transcript query loaded blob content: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_on_blob_read)

    started = time.perf_counter()
    items = projection.list_for_branch(conv_id, parent)
    elapsed = time.perf_counter() - started

    assert len(items) == 200
    assert elapsed < 0.5
    assert all(len(item["preview"]) <= 4096 for item in items)
    assert all(item["preview"].startswith("large message ") for item in items)
