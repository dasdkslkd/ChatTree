import threading

import pytest

from scripts import perf_trial


def test_perf_trial_defaults_to_v1_api_base():
    args = perf_trial._build_parser().parse_args([])

    assert args.base_url == "http://127.0.0.1:8001/api/v1"


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


def test_json_and_stream_requests_share_api_url_builder(monkeypatch, tmp_path):
    calls = []

    def fake_api_url(base_url, path):
        calls.append((base_url, path))
        return f"http://example.test{path}"

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b"{}"

        def readline(self):
            return b""

    monkeypatch.setattr(perf_trial, "_api_url", fake_api_url)
    monkeypatch.setattr(
        perf_trial.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )

    perf_trial._json_request("http://server.test/api/v1/", "/health")
    perf_trial._stream_once(
        base_url="http://server.test/api/v1/",
        conversation_id="conversation-1",
        parent_node_id="node-1",
        prompt="hello",
        output_path=tmp_path / "events.jsonl",
        lock=threading.Lock(),
        provider_id=None,
        model_id=None,
        worker_id=0,
        request_index=0,
    )

    assert calls == [
        ("http://server.test/api/v1/", "/health"),
        (
            "http://server.test/api/v1/",
            "/conversations/conversation-1/messages/stream",
        ),
    ]
