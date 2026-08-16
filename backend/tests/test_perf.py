from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import perf as perf_route
from backend.core.perf import NoopProfiler, configure_profiler
from backend.core.perf.aggregate import summarize_events, write_reports
from backend.core.perf.config import PerfConfig
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.transcript import TranscriptAssembler, TranscriptPatchSession


def test_noop_profiler_does_not_write(tmp_path):
    profiler = NoopProfiler(PerfConfig(enabled=False, output_dir=tmp_path))

    with profiler.span("ignored", conversation_id="conv"):
        profiler.mark("ignored.mark")
    assert profiler.record_frontend_events([{"type": "mark", "name": "x"}]) == 0

    assert not list(tmp_path.glob("*.jsonl"))


def test_perf_profiler_writes_sanitized_backend_and_frontend_events(tmp_path):
    profiler = configure_profiler(PerfConfig(enabled=True, perf_run_id="test", output_dir=tmp_path, max_attr_length=8))
    try:
        with profiler.span("unit.span", conversation_id="conv", long_value="x" * 20):
            profiler.mark("unit.mark", run_id="run-1")
        accepted = profiler.record_frontend_events([
            {"type": "mark", "name": "frontend.mark", "attrs": {"long": "y" * 20}},
        ])

        assert accepted == 1
        backend_rows = [
            json.loads(line)
            for line in (tmp_path / "backend-events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        frontend_rows = [
            json.loads(line)
            for line in (tmp_path / "frontend-events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert {row["name"] for row in backend_rows} >= {"unit.span", "unit.mark"}
        assert frontend_rows[0]["name"] == "frontend.mark"
        assert frontend_rows[0]["attrs"]["long"].startswith("yyyyyyyy")
    finally:
        configure_profiler(PerfConfig(enabled=False, output_dir=tmp_path))


def test_perf_routes_respect_enabled_profiler(tmp_path):
    configure_profiler(PerfConfig(enabled=True, perf_run_id="route-test", output_dir=tmp_path))
    try:
        app = FastAPI()
        app.include_router(perf_route.router)
        client = TestClient(app)

        config = client.get("/perf/config")
        assert config.status_code == 200
        assert config.json()["enabled"] is True
        assert config.json()["perf_run_id"] == "route-test"

        updated = client.post("/perf/config", json={"enabled": False, "sample_rate": 0.25})
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert updated.json()["sample_rate"] == 0.25
        rejected = client.post("/perf/config", json={"output_dir": str(tmp_path / "outside")})
        assert rejected.status_code == 400

        client.post("/perf/config", json={"enabled": True, "perf_run_id": "route-test"})

        recorded = client.post("/perf/events", json={"events": [{"type": "mark", "name": "browser"}]})
        assert recorded.status_code == 200
        assert recorded.json() == {"accepted": 1, "enabled": True}
    finally:
        configure_profiler(PerfConfig(enabled=False, output_dir=tmp_path))


def test_run_manager_emits_perf_spans_when_enabled(tmp_path):
    async def run():
        configure_profiler(PerfConfig(enabled=True, perf_run_id="run-test", output_dir=tmp_path))
        try:
            manager = RunManager()
            record = await manager.create_run(conversation_id="conv", kind=RunKind.CHAT)
            await manager.append_event(record.run_id, {"status": "content", "content": "hello"})
            batch = await manager.append_events(record.run_id, [
                {"status": "content", "content": "a"},
                {"status": "content", "content": "b"},
            ])
            assert [item["event_index"] for item in batch] == [2, 3]
            await manager.finish_run(record.run_id, RunStatus.COMPLETED)
        finally:
            configure_profiler(PerfConfig(enabled=False, output_dir=tmp_path))

    asyncio.run(run())

    rows = [
        json.loads(line)
        for line in (tmp_path / "backend-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    names = {row["name"] for row in rows}
    assert {"run.create", "run.append_event", "run.finish"} <= names


def test_perf_aggregate_writes_reports(tmp_path):
    events = tmp_path / "trial-events.jsonl"
    events.write_text(
        "\n".join([
            json.dumps({"type": "span", "name": "a", "duration_ms": 10}),
            json.dumps({"type": "span", "name": "a", "duration_ms": 30}),
            json.dumps({"type": "span", "name": "b", "duration_ms": 5}),
            json.dumps({
                "type": "mark",
                "name": "chat.provider_round.done",
                "attrs": {
                    "first_token_latency_ms": 120,
                    "first_content_latency_ms": 150,
                    "tokens_per_minute_est": 2400,
                },
            }),
        ]),
        encoding="utf-8",
    )

    summary = summarize_events([events])
    write_reports(summary, tmp_path)

    assert summary["hotspots"][0]["name"] == "a"
    metric_names = {item["name"] for item in summary["metrics"]}
    assert "chat.provider_round.done.first_token_latency_ms" in metric_names
    assert "chat.provider_round.done.first_content_latency_ms" in metric_names
    assert "chat.provider_round.done.tokens_per_minute_est" in metric_names
    assert summary["hotspots"][0]["category"] == "other"
    assert "other" in summary["hotspot_groups"]
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "hotspots.md").exists()
    assert (tmp_path / "hotspots.html").exists()


def test_transcript_patch_session_feed_records_perf_span(tmp_path):
    configure_profiler(PerfConfig(enabled=True, perf_run_id="feed-test", output_dir=tmp_path))
    try:
        persistence = SQLitePersistence(tmp_path / "feed-db")
        persistence.initialize()
        repository = ChatRepository(persistence)
        conversation_id = repository.create_conversation(title="feed")
        node_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
        session = TranscriptPatchSession(TranscriptAssembler(persistence), run_id="run-1")
        patch = session.feed({
            "conversation_id": conversation_id,
            "node_id": node_id,
            "type": "text",
            "status": "content",
            "content": "hello",
        })
        assert patch is not None
        assert patch["type"] == "transcript_patch"
    finally:
        configure_profiler(PerfConfig(enabled=False, output_dir=tmp_path))

    rows = [
        json.loads(line)
        for line in (tmp_path / "backend-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    names = {row["name"] for row in rows}
    assert {
        "transcript.patch.feed",
        "transcript.patch.feed.start",
        "transcript.patch.state",
        "transcript.patch.live_items",
        "transcript.patch.snapshot",
        "transcript.patch.revision",
    } <= names
    span = next(row for row in rows if row["name"] == "transcript.patch.feed")
    assert span["attrs"]["run_id"] == "run-1"
    assert span["attrs"]["conversation_id"] == conversation_id
    assert span["attrs"]["node_id"] == node_id
