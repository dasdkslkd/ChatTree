import asyncio

import pytest

from backend.core.runs import RunKind, RunManager, RunStatus, RunWriterConflictError
from backend.core.runs.journal import RunJournal


def test_run_manager_allows_multiple_runs_and_replays_events(tmp_path):
    async def run():
        manager = RunManager(RunJournal(tmp_path))
        first = await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-a")
        second = await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-b")

        await manager.append_event(first.run_id, {"status": "content", "content": "hello"})
        await manager.append_event(first.run_id, {"status": "content", "content": " world"})

        active = manager.list_active("conv")
        assert {item["run_id"] for item in active} == {first.run_id, second.run_id}

        sub = manager.subscribe(first.run_id, 1)
        event = await asyncio.wait_for(anext(sub), timeout=1)
        assert event["content"] == "hello"
        await sub.aclose()

        await manager.finish_run(first.run_id, RunStatus.COMPLETED)
        assert {item["run_id"] for item in manager.list_active("conv")} == {second.run_id}
        journal_payloads = [
            event["payload"]
            for event in manager.journal.read_events("conv", first.run_id)
        ]
        assert any(payload.get("content") == "hello" for payload in journal_payloads)

    asyncio.run(run())


def test_run_manager_rejects_two_writers_for_same_target(tmp_path):
    async def run():
        manager = RunManager(RunJournal(tmp_path))
        await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-a")
        with pytest.raises(RunWriterConflictError):
            await manager.create_run(conversation_id="conv", kind=RunKind.CHAT, target_node_id="node-a")

    asyncio.run(run())


def test_run_journal_stores_events_under_conversation_runs_dir(tmp_path):
    async def run():
        manager = RunManager(RunJournal(tmp_path))
        record = await manager.create_run(conversation_id="conv", kind=RunKind.WORKFLOW)
        await manager.append_event(record.run_id, {"status": "complete", "event_type": "workflow_result"})

        new_path = tmp_path / "conv" / "runs" / f"{record.run_id}.jsonl"
        old_path = tmp_path / "conv" / f"{record.run_id}.jsonl"
        assert new_path.exists()
        assert not old_path.exists()

        payloads = [event["payload"] for event in manager.journal.read_events("conv", record.run_id)]
        assert payloads[-1]["event_type"] == "workflow_result"

    asyncio.run(run())


def test_run_journal_ignores_legacy_data_runs_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy_root = tmp_path / "data" / "runs"
    path = legacy_root / "conv" / "run_legacy.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"run_id":"run_legacy","event_index":0,"payload":{"type":"run_finished","status":"completed"}}\n',
        encoding="utf-8",
    )

    journal = RunJournal()

    events = journal.read_events("conv", "run_legacy")

    assert events == []
