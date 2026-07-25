import time
from pathlib import Path

from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.transcript import TranscriptAssembler


def test_transcript_snapshot_does_not_load_large_tool_output_blobs(tmp_path, monkeypatch):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repo = ChatRepository(persistence)
    conv_id = repo.create_conversation(title="Perf")
    parent = None
    for index in range(200):
        node_id = repo.create_node(conv_id, parent_id=parent, child_order=0)
        call_id = repo.add_tool_call(
            conv_id,
            node_id,
            tool_call_id=f"call-{index}",
            name="large_tool",
            arguments={"index": index},
        )
        repo.add_tool_result(
            conv_id,
            node_id,
            tool_result_id=f"result-{index}",
            tool_call_id=call_id,
            output=f"large result {index}\n" + ("x" * 20000),
        )
        parent = node_id

    original_read_bytes = Path.read_bytes

    def fail_on_blob_read(path):
        if path.suffix == ".gz" and persistence.blobs_dir in path.parents:
            raise AssertionError(f"transcript snapshot loaded blob content: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_on_blob_read)

    started = time.perf_counter()
    items = TranscriptAssembler(persistence).snapshot(conv_id, parent)["items"]
    elapsed = time.perf_counter() - started

    process_items = [item for item in items if item["type"] == "assistant_process"]
    assert len(process_items) == 200
    assert elapsed < 0.5
    for item in process_items:
        assert len(item["blocks"][0]["result_preview"]) <= 4096
