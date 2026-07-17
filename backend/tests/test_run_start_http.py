from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from backend.api.errors import (
    GENERIC_5XX_MESSAGE,
    ApiError,
    ErrorEnvelope,
    RequestBoundaryMiddleware,
    install_error_handlers,
)
from backend.api.run_start import (
    RunStartResponse,
    require_idempotency_key,
    run_start_api_error,
    run_start_openapi_responses,
    run_start_response,
)
from backend.core.runs import (
    RunIdempotency,
    RunIdempotencyConflictError,
    RunKind,
    RunManager,
    RunRecord,
    RunReferenceConversationMismatchError,
    RunReferenceNotFoundError,
    RunRequestFingerprintError,
    RunStartCoordinator,
    RunStartResult,
    RunStartSpec,
    RunStatus,
)
from backend.core.runs.start_service import (
    RunStartReservationError,
    RunStartSchedulingError,
    RunStartValidationError,
)


def _request_with_raw_headers(values: list[tuple[str, str]]) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/start",
            "raw_path": b"/start",
            "query_string": b"",
            "headers": [
                (name.lower().encode("ascii"), value.encode("latin-1"))
                for name, value in values
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "state": {"request_id": "request-id"},
        }
    )


@pytest.mark.parametrize(
    "value",
    ("", "has space", "é", "x" * 129, "line\nfeed", "has,comma"),
)
def test_invalid_idempotency_key_returns_typed_422(value):
    request = _request_with_raw_headers([("Idempotency-Key", value)])

    with pytest.raises(ApiError) as raised:
        require_idempotency_key(request, value)

    assert raised.value.status_code == 422
    assert raised.value.code == "invalid_idempotency_key"


def test_missing_idempotency_key_returns_typed_428():
    request = _request_with_raw_headers([])

    with pytest.raises(ApiError) as raised:
        require_idempotency_key(request, None)

    assert raised.value.status_code == 428
    assert raised.value.code == "idempotency_key_required"


@pytest.mark.parametrize(
    "values",
    (
        ("op_same", "op_same"),
        ("op_first", "op_second"),
    ),
    ids=("same-values", "different-values"),
)
def test_duplicate_raw_idempotency_headers_are_rejected(values):
    request = _request_with_raw_headers(
        [("Idempotency-Key", value) for value in values]
    )

    with pytest.raises(ApiError) as raised:
        require_idempotency_key(request, values[-1])

    assert request.headers.getlist("Idempotency-Key") == list(values)
    assert raised.value.status_code == 422
    assert raised.value.code == "invalid_idempotency_key"


@pytest.mark.parametrize(
    "value",
    ("a", "op_1234.ab-CD:ef", "x" * 128),
)
def test_one_valid_raw_idempotency_header_is_returned_unchanged(value):
    request = _request_with_raw_headers([("Idempotency-Key", value)])

    assert require_idempotency_key(request, value) == value


def test_folded_header_argument_cannot_override_the_raw_value():
    request = _request_with_raw_headers([("Idempotency-Key", "op_raw")])

    with pytest.raises(ApiError) as raised:
        require_idempotency_key(request, "op_folded")

    assert raised.value.code == "invalid_idempotency_key"


def _key_contract_client() -> tuple[TestClient, list[str]]:
    app = FastAPI()
    install_error_handlers(app)
    calls: list[str] = []

    @app.post("/start")
    async def start(
        idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    ):
        calls.append(idempotency_key)
        return {"key": idempotency_key}

    return TestClient(app, raise_server_exceptions=False), calls


def test_real_http_boundary_accepts_one_key_and_rejects_duplicates():
    client, calls = _key_contract_client()

    accepted = client.post("/start", headers={"Idempotency-Key": "op_http"})
    missing = client.post("/start")
    invalid = client.post("/start", headers={"Idempotency-Key": "has space"})
    duplicate = client.post(
        "/start",
        headers=[
            ("Idempotency-Key", "op_http"),
            ("Idempotency-Key", "op_http"),
        ],
    )

    assert accepted.status_code == 200
    assert accepted.json() == {"key": "op_http"}
    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "idempotency_key_required"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_idempotency_key"
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "invalid_idempotency_key"
    assert calls == ["op_http"]


def test_run_start_response_uses_202_for_winner_and_200_for_loser():
    run = RunRecord(
        run_id="run-1",
        conversation_id="conv-1",
        kind=RunKind.CHAT,
        status=RunStatus.RUNNING,
    )
    winner = run_start_response(RunStartResult(run=run, created=True))
    loser = run_start_response(RunStartResult(run=run, created=False))

    assert winner.status_code == 202
    assert loser.status_code == 200
    assert json.loads(winner.body) == {
        "run_id": "run-1",
        "created": True,
        "status": "running",
    }
    assert json.loads(loser.body)["created"] is False
    assert "attach_url" not in winner.body.decode("utf-8")


def test_run_start_openapi_responses_include_typed_404():
    responses = run_start_openapi_responses()

    assert set(responses) == {200, 202, 404, 409, 422, 428, 500}
    assert responses[200]["model"] is RunStartResponse
    assert responses[202]["model"] is RunStartResponse
    for status_code in (404, 409, 422, 428, 500):
        assert responses[status_code]["model"] is ErrorEnvelope


def test_idempotency_conflict_maps_to_typed_409():
    mapped = run_start_api_error(RunIdempotencyConflictError("run-existing"))

    assert mapped.status_code == 409
    assert mapped.code == "idempotency_key_conflict"
    assert mapped.retryable is False
    assert mapped.details == {"existing_run_id": "run-existing"}


@pytest.mark.parametrize(
    ("exc", "status_code", "code"),
    (
        (
            RunReferenceNotFoundError("anchor_node_id", "node-missing"),
            404,
            "run_reference_not_found",
        ),
        (
            RunReferenceConversationMismatchError("created_by_run_id", "run-other"),
            422,
            "invalid_run_reference",
        ),
    ),
)
def test_reference_errors_map_to_safe_public_details(exc, status_code, code):
    mapped = run_start_api_error(exc)

    assert mapped.status_code == status_code
    assert mapped.code == code
    assert mapped.details == {
        "reference_kind": exc.reference_kind,
        "reference_id": exc.reference_id,
    }


@pytest.mark.parametrize(
    "exc",
    (
        RunRequestFingerprintError("finite JSON required"),
        RunStartValidationError("invalid start"),
    ),
)
def test_start_validation_errors_map_to_typed_422(exc):
    mapped = run_start_api_error(exc)

    assert mapped.status_code == 422
    assert mapped.code == "invalid_request"
    assert mapped.message == str(exc)
    assert mapped.retryable is False


@pytest.mark.parametrize(
    "exc",
    (
        RunStartReservationError("reservation failed"),
        RunStartSchedulingError("run-scheduling"),
    ),
)
def test_internal_start_failures_map_to_already_logged_generic_500(exc):
    mapped = run_start_api_error(exc)

    assert mapped.status_code == 500
    assert mapped.code == "internal_error"
    assert mapped.message == GENERIC_5XX_MESSAGE
    assert mapped.retryable is False
    assert mapped.details is None
    assert mapped.already_logged is True


def test_unknown_start_exception_is_reraised_unchanged():
    exc = RuntimeError("not a shared start error")

    with pytest.raises(RuntimeError) as raised:
        run_start_api_error(exc)

    assert raised.value is exc


@pytest.mark.parametrize(
    ("kind", "expected_text"),
    (
        ("reservation", "reservation source secret"),
        ("scheduling", "scheduling source secret"),
    ),
)
def test_real_http_internal_start_error_is_scrubbed_without_duplicate_boundary_log(
    caplog,
    kind,
    expected_text,
):
    source_logger = logging.getLogger("test.run_start.coordinator")
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/fail/{failure_kind}")
    async def fail(failure_kind: str, request: Request):
        request_id = request.headers.get("X-Request-ID")
        if failure_kind == "reservation":
            source_logger.error(
                "reservation source secret request_id=%s",
                request_id,
            )
            exc = RunStartReservationError("reservation source secret")
        else:
            source_logger.error(
                "scheduling source secret request_id=%s",
                request_id,
            )
            exc = RunStartSchedulingError("run-secret")
        raise run_start_api_error(exc)

    caplog.set_level(logging.ERROR)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        f"/fail/{kind}",
        headers={"X-Request-ID": "run-start-http-error"},
    )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": GENERIC_5XX_MESSAGE,
        "retryable": False,
        "request_id": "run-start-http-error",
    }
    assert response.headers["X-Request-ID"] == "run-start-http-error"
    assert expected_text not in response.text
    relevant = [
        record
        for record in caplog.records
        if record.name in {"test.run_start.coordinator", "backend.api.errors"}
    ]
    assert [record.name for record in relevant] == ["test.run_start.coordinator"]
    assert expected_text in relevant[0].getMessage()
    assert "run-start-http-error" in relevant[0].getMessage()


def _coordinator_http_app(
    coordinator: RunStartCoordinator,
    bootstrap: Any,
) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestBoundaryMiddleware)

    @app.post("/start")
    async def start(
        http_request: Request,
        idempotency_key: Annotated[
            str,
            Depends(require_idempotency_key),
        ],
    ):
        spec = RunStartSpec(
            conversation_id="conversation-http",
            kind=RunKind.CHAT,
            anchor_node_id=None,
            summary="http start",
            idempotency=RunIdempotency(idempotency_key, "a" * 64),
            request_id=http_request.state.request_id,
        )
        try:
            result = await coordinator.start(spec, bootstrap)
        except (RunStartReservationError, RunStartSchedulingError) as exc:
            raise run_start_api_error(exc) from exc
        return run_start_response(result)

    return app


async def _wait_for_barrier_waiters(
    coordinator: RunStartCoordinator,
    key: str,
    expected: int,
) -> None:
    async def wait() -> None:
        while True:
            barrier = coordinator._bootstrap_barriers.get(key)
            callbacks = (
                getattr(barrier.future, "_callbacks", None)
                if barrier is not None
                else None
            )
            if len(callbacks or ()) >= expected:
                return
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1)


def _assert_generic_start_500(response: Any, request_id: str) -> None:
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": GENERIC_5XX_MESSAGE,
        "retryable": False,
        "request_id": request_id,
    }
    assert response.headers["X-Request-ID"] == request_id


def test_real_http_pre_id_retry_exhaustion_logs_only_at_coordinator(
    monkeypatch,
    caplog,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        key = "op_http_pre_id"
        request_count = 6
        reserve_calls = 0
        bootstrap_calls = 0
        reserve_entered = [asyncio.Event(), asyncio.Event()]
        release_reserve = [asyncio.Event(), asyncio.Event()]

        async def persistent_pre_id_failure(**_kwargs):
            nonlocal reserve_calls
            index = reserve_calls
            reserve_calls += 1
            if index < len(reserve_entered):
                reserve_entered[index].set()
                await release_reserve[index].wait()
            raise RuntimeError(f"pre-id source secret {index}")

        async def bootstrap(_run):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            return asyncio.create_task(asyncio.sleep(0))

        monkeypatch.setattr(
            manager,
            "reserve_or_get_run",
            persistent_pre_id_failure,
        )
        app = _coordinator_http_app(coordinator, bootstrap)
        transport = ASGITransport(app=app)
        request_ids = [f"pre-id-http-{index}" for index in range(request_count)]

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            requests = [
                asyncio.create_task(
                    client.post(
                        "/start",
                        headers={
                            "Idempotency-Key": key,
                            "X-Request-ID": request_id,
                        },
                    )
                )
                for request_id in request_ids
            ]
            await asyncio.wait_for(reserve_entered[0].wait(), timeout=1)
            await _wait_for_barrier_waiters(
                coordinator,
                key,
                request_count - 1,
            )
            release_reserve[0].set()

            await asyncio.wait_for(reserve_entered[1].wait(), timeout=1)
            await _wait_for_barrier_waiters(
                coordinator,
                key,
                request_count - 2,
            )
            release_reserve[1].set()
            responses = await asyncio.wait_for(
                asyncio.gather(*requests),
                timeout=1,
            )

        for response, request_id in zip(responses, request_ids):
            _assert_generic_start_500(response, request_id)
        assert reserve_calls == 2
        assert bootstrap_calls == 0
        assert manager.list_runs() == []
        assert coordinator._bootstrap_barriers == {}

        relevant = [
            record
            for record in caplog.records
            if record.name
            in {"backend.core.runs.start_service", "backend.api.errors"}
        ]
        assert relevant
        assert {record.name for record in relevant} == {
            "backend.core.runs.start_service"
        }
        messages = [record.getMessage() for record in relevant]
        assert sum(
            "run reservation failed before canonical ID" in message
            for message in messages
        ) == 2
        assert sum(
            "run reservation retry budget exhausted" in message
            for message in messages
        ) == request_count - 2

        assert (await coordinator.close(timeout=1)).exhausted is False
        assert (await manager.close(timeout=1)).exhausted_run_ids == ()

    caplog.set_level(logging.ERROR)
    asyncio.run(run())


def test_real_http_post_id_scheduling_failure_logs_once_without_boundary_log(
    caplog,
):
    async def run() -> None:
        manager = RunManager()
        coordinator = RunStartCoordinator(manager)
        key = "op_http_post_id"
        request_count = 6
        bootstrap_calls = 0
        bootstrap_entered = asyncio.Event()
        release_bootstrap = asyncio.Event()

        async def bootstrap(_run):
            nonlocal bootstrap_calls
            bootstrap_calls += 1
            bootstrap_entered.set()
            await release_bootstrap.wait()
            raise RuntimeError("post-id scheduling source secret")

        app = _coordinator_http_app(coordinator, bootstrap)
        transport = ASGITransport(app=app)
        request_ids = [f"post-id-http-{index}" for index in range(request_count)]

        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            requests = [
                asyncio.create_task(
                    client.post(
                        "/start",
                        headers={
                            "Idempotency-Key": key,
                            "X-Request-ID": request_id,
                        },
                    )
                )
                for request_id in request_ids
            ]
            await asyncio.wait_for(bootstrap_entered.wait(), timeout=1)
            await _wait_for_barrier_waiters(
                coordinator,
                key,
                request_count - 1,
            )
            release_bootstrap.set()
            responses = await asyncio.wait_for(
                asyncio.gather(*requests),
                timeout=1,
            )

        for response, request_id in zip(responses, request_ids):
            _assert_generic_start_500(response, request_id)
        assert bootstrap_calls == 1
        runs = manager.list_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == RunStatus.INTERRUPTED.value
        assert coordinator._bootstrap_barriers == {}

        relevant = [
            record
            for record in caplog.records
            if record.name
            in {"backend.core.runs.start_service", "backend.api.errors"}
        ]
        assert [record.name for record in relevant] == [
            "backend.core.runs.start_service"
        ]
        assert "run producer scheduling failed" in relevant[0].getMessage()

        assert (await coordinator.close(timeout=1)).exhausted is False
        assert (await manager.close(timeout=1)).exhausted_run_ids == ()

    caplog.set_level(logging.ERROR)
    asyncio.run(run())
