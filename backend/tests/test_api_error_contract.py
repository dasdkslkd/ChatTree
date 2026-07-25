from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.responses import StreamingResponse

import backend.api.routes.server as server_routes
import main
from backend.api.dependencies import get_chat_manager, get_run_manager, get_task_service
from backend.api.errors import GENERIC_5XX_MESSAGE, RequestBoundaryMiddleware
from backend.core.server.identity import ServerIdentity


SERVER_ID = "5fb0d7cc-785e-40c2-875d-218447b15583"
_MISSING = object()


def request_main_app(method: str, path: str, **kwargs):
    async def send_request():
        transport = httpx.ASGITransport(
            app=main.app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send_request())


def test_production_server_hides_unhandled_exception(monkeypatch, caplog):
    def explode(_request):
        raise RuntimeError("provider-secret")

    caplog.set_level("ERROR", logger="backend.api.errors")
    monkeypatch.setattr(server_routes, "_identity", explode)

    response = request_main_app(
        "GET",
        "/api/v1/health",
        headers={"X-Request-ID": "req_prod"},
    )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req_prod"
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": GENERIC_5XX_MESSAGE,
        "retryable": False,
        "request_id": "req_prod",
    }
    assert "provider-secret" not in response.text
    assert "provider-secret" in caplog.text


@pytest.mark.parametrize(
    ("method", "headers"),
    (
        (
            "GET",
            {
                "Origin": "https://evil.example",
                "X-Request-ID": "req_bad_origin",
            },
        ),
        (
            "OPTIONS",
            {
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
                "X-Request-ID": "req_bad_preflight",
            },
        ),
    ),
)
def test_production_server_rejects_invalid_origins(method, headers):
    response = request_main_app(method, "/api/v1/health", headers=headers)

    assert response.status_code == 403
    assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert response.json()["error"]["request_id"] == headers["X-Request-ID"]
    assert "access-control-allow-origin" not in response.headers


def test_production_server_allows_preflight_and_adds_request_id():
    response = request_main_app(
        "OPTIONS",
        "/api/v1/health",
        headers={
            "Origin": "http://127.0.0.1:4317",
            "Access-Control-Request-Method": "GET",
            "X-Request-ID": "req_preflight",
        },
    )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == (
        "http://127.0.0.1:4317"
    )
    assert response.headers["X-Request-ID"] == "req_preflight"


def test_production_success_response_has_canonical_request_id(monkeypatch):
    monkeypatch.setattr(
        server_routes,
        "_identity",
        lambda _request: ServerIdentity(server_instance_id=SERVER_ID),
    )

    response = request_main_app(
        "GET",
        "/api/v1/health",
        headers={"X-Request-ID": "req_success"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_success"


class _StreamingRunManager:
    def get_run(self, run_id: str):
        return {"run_id": run_id}

    def read_events(self, run_id: str, from_event: int):
        return []

    async def subscribe(self, run_id: str, from_event: int):
        yield {"run_id": run_id, "sequence": from_event + 1}


class _FinishedRunManager:
    def get_run(self, run_id: str):
        return {"run_id": run_id, "status": "completed"}

    async def subscribe(self, run_id: str, from_event: int):
        raise AssertionError("finished runs must not open an attach stream")


class _RaceFinishedRunManager:
    def __init__(self):
        self.get_calls = 0

    def get_run(self, run_id: str):
        self.get_calls += 1
        status = "running" if self.get_calls == 1 else "completed"
        return {"run_id": run_id, "status": status}

    def read_events(self, run_id: str, from_event: int):
        if from_event > 0:
            return []
        return [
            {
                "type": "run_finished",
                "run_id": run_id,
                "event_index": 0,
                "status": "completed",
            }
        ]

    async def subscribe(self, run_id: str, from_event: int):
        if False:
            yield {}


class _PatchSession:
    def feed(self, payload, *, emit=True):
        if not emit:
            return None
        return {"type": "transcript_patch", "payload": payload}


class _TranscriptAssembler:
    def patch_session(self, run_id: str):
        return _PatchSession()


def test_production_sse_response_has_request_id_before_body():
    previous = getattr(main.app.state, "transcript_assembler", _MISSING)
    main.app.state.transcript_assembler = _TranscriptAssembler()
    main.app.dependency_overrides[get_run_manager] = _StreamingRunManager
    try:
        response = request_main_app(
            "GET",
            "/api/v1/runs/run-1/events",
            headers={"X-Request-ID": "req_sse"},
        )
    finally:
        main.app.dependency_overrides.pop(get_run_manager, None)
        if previous is _MISSING:
            delattr(main.app.state, "transcript_assembler")
        else:
            main.app.state.transcript_assembler = previous

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_sse"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("data: ")


def test_production_finished_run_events_replays_terminal_body():
    previous = getattr(main.app.state, "transcript_assembler", _MISSING)
    main.app.state.transcript_assembler = _TranscriptAssembler()
    main.app.dependency_overrides[get_run_manager] = _FinishedRunManager
    try:
        response = request_main_app(
            "GET",
            "/api/v1/runs/run-1/events",
            headers={"X-Request-ID": "req_finished_events"},
        )
    finally:
        main.app.dependency_overrides.pop(get_run_manager, None)
        if previous is _MISSING:
            delattr(main.app.state, "transcript_assembler")
        else:
            main.app.state.transcript_assembler = previous

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_finished_events"
    assert response.headers["content-type"].startswith("text/event-stream")


def test_production_attach_race_finished_run_returns_terminal_body(monkeypatch):
    previous = getattr(main.app.state, "transcript_assembler", _MISSING)
    main.app.state.transcript_assembler = _TranscriptAssembler()
    main.app.dependency_overrides[get_run_manager] = _RaceFinishedRunManager
    try:
        response = request_main_app(
            "GET",
            "/api/v1/runs/run-1/events",
            headers={"X-Request-ID": "req_finished_race"},
        )
    finally:
        main.app.dependency_overrides.pop(get_run_manager, None)
        if previous is _MISSING:
            delattr(main.app.state, "transcript_assembler")
        else:
            main.app.state.transcript_assembler = previous

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_finished_race"
    assert '"type": "transcript_patch"' in response.text
    assert response.text.strip().endswith("data: [DONE]")


def test_request_boundary_forwards_first_stream_chunk_without_buffering():
    async def exercise() -> None:
        release = asyncio.Event()
        first_body_sent = asyncio.Event()

        async def generate():
            yield b"first"
            await release.wait()
            yield b"second"

        response = StreamingResponse(generate(), media_type="text/event-stream")

        async def downstream(scope, receive, send):
            await response(scope, receive, send)

        boundary = RequestBoundaryMiddleware(downstream)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)
            if (
                message["type"] == "http.response.body"
                and message.get("body") == b"first"
            ):
                first_body_sent.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "headers": [(b"x-request-id", b"req_timing")],
            "client": ("test", 50000),
            "server": ("test", 80),
            "state": {},
        }
        task = asyncio.create_task(boundary(scope, receive, send))
        await asyncio.wait_for(first_body_sent.wait(), timeout=1)
        assert task.done() is False
        assert release.is_set() is False
        release.set()
        await task

        bodies = [
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        ]
        assert bodies[:2] == [b"first", b"second"]

    asyncio.run(exercise())


class _Conversation:
    def get_descendant_node_ids(self, node_id: str):
        return {node_id, "child-node"}


class _ChatManager:
    def get_conversation(self, _conversation_id: str):
        return _Conversation()


class _ActiveRunManager:
    def active_runs_for_targets(self, **_kwargs):
        return [{"run_id": "run-active", "target_node_id": "child-node"}]


def test_active_run_delete_uses_production_api_error_handler():
    main.app.dependency_overrides[get_chat_manager] = _ChatManager
    main.app.dependency_overrides[get_run_manager] = _ActiveRunManager
    try:
        response = request_main_app(
            "DELETE",
            "/api/v1/conversations/conv-1/nodes/node-1",
            headers={"X-Request-ID": "req_active_run"},
        )
    finally:
        main.app.dependency_overrides.pop(get_chat_manager, None)
        main.app.dependency_overrides.pop(get_run_manager, None)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "active_runs_present",
        "message": "该分支仍有运行中的任务，请先停止后再删除",
        "retryable": True,
        "request_id": "req_active_run",
        "details": {"active_run_ids": ["run-active"]},
    }


def test_production_validation_error_uses_error_envelope():
    main.app.dependency_overrides[get_task_service] = object
    try:
        response = request_main_app(
            "POST",
            "/api/v1/conversations/conv-1/task",
            json={"steps": []},
            headers={"X-Request-ID": "req_validation"},
        )
    finally:
        main.app.dependency_overrides.pop(get_task_service, None)

    assert response.status_code == 422
    payload = response.json()["error"]
    assert payload["code"] == "invalid_request"
    assert payload["request_id"] == "req_validation"
    assert payload["details"]["issues"]
    assert "detail" not in response.json()
