import json
import threading

import pytest

from scripts import perf_trial


def test_perf_trial_defaults_to_v1_api_base():
    args = perf_trial._build_parser().parse_args([])

    assert args.base_url == "http://127.0.0.1:8001/api/v1"


def test_worker_runs_once_when_duration_is_zero(monkeypatch, tmp_path):
    calls = []

    def fake_run_once(**kwargs):
        calls.append(kwargs["parent_node_id"])
        return "node-2"

    monkeypatch.setattr(perf_trial, "_run_once", fake_run_once)

    parent_node_id = perf_trial._worker(
        base_url="http://server.test/api/v1",
        duration_seconds=0,
        conversation_id="conversation-1",
        initial_parent_node_id="node-1",
        prompt="hello",
        output_path=tmp_path / "events.jsonl",
        lock=threading.Lock(),
        provider_id=None,
        model_id=None,
        reasoning_effort=None,
        tool_permission_mode="auto_approve",
        worker_id=0,
    )

    assert calls == ["node-1"]
    assert parent_node_id == "node-2"


def test_session_file_resumes_the_same_conversation(monkeypatch, tmp_path):
    parents = iter(["node-1", "node-2"])
    worker_parents = []
    create_calls = []

    def fake_create_conversation(base_url, workspace):
        create_calls.append((base_url, workspace))
        return {"id": "conversation-1", "current_node_id": "node-0"}

    def fake_worker(**kwargs):
        worker_parents.append(kwargs["initial_parent_node_id"])
        return next(parents)

    monkeypatch.setattr(perf_trial, "_create_conversation", fake_create_conversation)
    monkeypatch.setattr(perf_trial, "_worker", fake_worker)
    monkeypatch.setattr(perf_trial, "summarize_events", lambda paths: {})
    monkeypatch.setattr(perf_trial, "write_reports", lambda summary, output_dir: None)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello", encoding="utf-8")
    session_file = tmp_path / "session.json"
    argv = [
        "--duration-seconds", "0",
        "--prompt-file", str(prompt),
        "--workspace", str(workspace),
        "--session-file", str(session_file),
        "--output-dir", str(tmp_path / "perf"),
    ]

    assert perf_trial.main(argv) == 0
    assert perf_trial.main(argv) == 0

    assert create_calls == [(perf_trial.DEFAULT_API_BASE_URL, str(workspace))]
    assert worker_parents == ["node-0", "node-1"]
    assert json.loads(session_file.read_text(encoding="utf-8")) == {
        "conversation_id": "conversation-1",
        "parent_node_id": "node-2",
    }


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        (
            "http://127.0.0.1:8001/api/v1",
            "/conversations",
            "http://127.0.0.1:8001/api/v1/conversations",
        ),
        (
            "http://127.0.0.1:8001/api/v1/",
            "/perf/config",
            "http://127.0.0.1:8001/api/v1/perf/config",
        ),
    ],
)
def test_perf_trial_joins_paths_under_api_base(base_url, path, expected):
    assert perf_trial._api_url(base_url, path) == expected


def test_json_and_run_requests_share_api_url_builder(monkeypatch, tmp_path):
    calls = []

    def fake_api_url(base_url, path):
        calls.append((base_url, path))
        return f"http://example.test{path}"

    class FakeResponse:
        def __init__(self, body=b"{}", lines=None):
            self.body = body
            self.lines = list(lines or [])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return self.body

        def readline(self):
            return self.lines.pop(0) if self.lines else b""

    monkeypatch.setattr(perf_trial, "_api_url", fake_api_url)
    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/messages/runs"):
            assert request.get_header("Idempotency-key")
            return FakeResponse(b'{"run_id":"run-1"}')
        if request.full_url.endswith("/runs/run-1/events"):
            return FakeResponse(lines=[
                b'data: {"type":"transcript_patch","node_id":"node-2","revision":3,"operations":[{"op":"remove","id":"old"}]}\n',
                b"\n",
                b"data: [DONE]\n",
                b"\n",
            ])
        if request.full_url.endswith("/runs/run-1"):
            return FakeResponse(b'{"status":"completed","target_node_id":"node-2"}')
        return FakeResponse()

    monkeypatch.setattr(perf_trial.urllib.request, "urlopen", fake_urlopen)

    perf_trial._json_request("http://server.test/api/v1/", "/health")
    perf_trial._run_once(
        base_url="http://server.test/api/v1/",
        conversation_id="conversation-1",
        parent_node_id="node-1",
        prompt="hello",
        output_path=tmp_path / "events.jsonl",
        lock=threading.Lock(),
        provider_id=None,
        model_id=None,
        reasoning_effort=None,
        tool_permission_mode="auto_approve",
        worker_id=0,
        request_index=0,
    )

    assert calls == [
        ("http://server.test/api/v1/", "/health"),
        (
            "http://server.test/api/v1/",
            "/conversations/conversation-1/messages/runs",
        ),
        ("http://server.test/api/v1/", "/runs/run-1/events"),
        ("http://server.test/api/v1/", "/runs/run-1"),
    ]
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    sse = next(event for event in events if event["name"] == "trial.sse_event")
    assert sse["attrs"] == {
        "patch_type": "transcript_patch",
        "revision": 3,
        "operation_count": 1,
    }
