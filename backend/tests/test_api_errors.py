from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Annotated
from uuid import UUID

import pytest
from fastapi import Body, FastAPI, Request, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError
from starlette.exceptions import HTTPException

from backend.api.errors import (
    GENERIC_5XX_MESSAGE,
    REQUEST_ID_RE,
    ApiError,
    ErrorEnvelope,
    RequestBoundaryMiddleware,
    canonical_request_id,
    error_response,
    install_error_handlers,
)


class _SensitiveValidationBody(BaseModel):
    value: str

    @field_validator("value")
    @classmethod
    def reject_sensitive_value(cls, value: str) -> str:
        raise PydanticCustomError(
            "sensitive_custom_error",
            "ctx contained {sensitive}",
            {"sensitive": value},
        )


class _DictionaryValidationBody(BaseModel):
    values: dict[str, int]


@pytest.fixture
def contract_client():
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(
        RequestBoundaryMiddleware,
        allowed_origins=("http://localhost:5173",),
        allowed_origin_pattern=r"http://(localhost|127\.0\.0\.1):\d+",
    )

    @app.get("/status/{status_code}")
    async def status_error(status_code: int):
        raise HTTPException(status_code, "legacy status secret")

    @app.get("/legacy-string")
    async def legacy_string():
        raise HTTPException(409, "blocked")

    @app.get("/legacy-dict")
    async def legacy_dict():
        raise HTTPException(409, {"message": "blocked", "secret": "drop"})

    @app.get("/legacy-list")
    async def legacy_list():
        raise HTTPException(409, ["blocked", {"secret": "drop"}])

    @app.get("/typed-client-error")
    async def typed_client_error():
        raise ApiError(
            409,
            "resource_conflict",
            "blocked",
            False,
            {"field": "name"},
        )

    @app.get("/typed-server-error")
    async def typed_server_error():
        raise ApiError(
            503,
            "upstream_failed",
            "typed server secret",
            True,
            {"secret": "drop"},
        )

    @app.get("/typed-server-error-already-logged")
    async def typed_server_error_already_logged():
        raise ApiError(
            500,
            "internal_error",
            "already logged server secret",
            False,
            {"secret": "drop"},
            already_logged=True,
        )

    @app.get("/http-server-error")
    async def http_server_error():
        raise HTTPException(
            500,
            {"message": "legacy server secret", "secret": "drop"},
        )

    @app.get("/unknown-server-error")
    async def unknown_server_error():
        raise RuntimeError("unknown server secret")

    @app.get("/empty-details/{kind}")
    async def empty_details(kind: str):
        details = {} if kind == "empty" else None
        raise ApiError(400, "invalid_request", "invalid", False, details)

    @app.get("/headers")
    async def headers_error():
        raise HTTPException(
            401,
            "authenticate",
            headers={
                "Allow": "GET, HEAD",
                "Retry-After": "17",
                "WWW-Authenticate": 'Bearer realm="api"',
                "ETag": '"revision-1"',
                "Content-Type": "text/plain",
                "Content-Length": "9999",
                "X-Request-ID": "spoofed",
                "X-Unsafe": "drop",
            },
        )

    @app.get("/ok")
    async def ok():
        return Response("ok", headers={"X-Request-ID": "spoofed"})

    @app.get("/mutated-request-id")
    async def mutated_request_id(request: Request):
        request.state.request_id = "not valid!"
        raise ApiError(400, "invalid_request", "invalid", False)

    @app.get("/non-string-request-id/{kind}")
    async def non_string_request_id(request: Request, kind: str):
        values = {
            "uuid": UUID("3d4108fc-d044-448f-8520-8d2fb826eaf8"),
            "integer": 17,
            "object": object(),
        }
        request.state.request_id = values[kind]
        raise ApiError(400, "invalid_request", "invalid", False)

    @app.post("/validated")
    async def validated(value: Annotated[int, Body()]):
        return {"value": value}

    @app.post("/validated-ctx")
    async def validated_ctx(payload: _SensitiveValidationBody):
        return payload

    @app.post("/validated-dictionary")
    async def validated_dictionary(payload: _DictionaryValidationBody):
        return payload

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    (
        (400, "invalid_request", False),
        (401, "unauthorized", False),
        (403, "forbidden", False),
        (404, "not_found", False),
        (405, "method_not_allowed", False),
        (409, "conflict", False),
        (410, "gone", False),
        (412, "precondition_failed", False),
        (413, "payload_too_large", False),
        (415, "unsupported_media_type", False),
        (418, "http_error", False),
        (422, "invalid_request", False),
        (428, "precondition_required", False),
        (429, "rate_limited", True),
        (451, "http_error", False),
        (500, "internal_error", False),
        (502, "service_unavailable", True),
        (503, "service_unavailable", True),
        (504, "service_unavailable", True),
        (507, "internal_error", False),
    ),
)
def test_http_statuses_map_to_stable_codes(contract_client, status_code, code, retryable):
    response = contract_client.get(f"/status/{status_code}")

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable
    if status_code >= 500:
        assert response.json()["error"]["message"] == GENERIC_5XX_MESSAGE
        assert "legacy status secret" not in response.text


@pytest.mark.parametrize(
    "value",
    (
        "request-123",
        "a.b_c:d-e",
        "x" * 128,
    ),
)
def test_canonical_request_id_preserves_valid_values(value):
    assert canonical_request_id(value) == value


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        "contains space",
        "contains/slash",
        "请求",
        "x" * 129,
    ),
)
def test_canonical_request_id_replaces_missing_or_invalid_values(value):
    request_id = canonical_request_id(value)

    assert REQUEST_ID_RE.fullmatch(request_id)
    UUID(request_id)
    assert request_id != value


@pytest.mark.parametrize(
    "value",
    (
        UUID("3d4108fc-d044-448f-8520-8d2fb826eaf8"),
        17,
        object(),
    ),
    ids=("uuid", "integer", "object"),
)
def test_canonical_request_id_replaces_non_string_values(value):
    request_id = canonical_request_id(value)

    assert REQUEST_ID_RE.fullmatch(request_id)
    UUID(request_id)


@pytest.mark.parametrize(
    ("incoming", "preserved"),
    (
        ("client-request:1", True),
        ("not valid!", False),
        (None, False),
    ),
)
def test_response_header_and_body_share_canonical_request_id(
    contract_client,
    incoming,
    preserved,
):
    headers = {"X-Request-ID": incoming} if incoming is not None else {}

    response = contract_client.get("/status/418", headers=headers)

    request_id = response.json()["error"]["request_id"]
    assert response.headers["X-Request-ID"] == request_id
    assert REQUEST_ID_RE.fullmatch(request_id)
    assert (request_id == incoming) is preserved


def test_middleware_replaces_downstream_request_id_header(contract_client):
    response = contract_client.get("/ok", headers={"X-Request-ID": "client-id"})

    assert response.headers["X-Request-ID"] == "client-id"
    assert response.headers.get_list("X-Request-ID") == ["client-id"]


def test_mutated_request_state_still_produces_one_canonical_response_id(
    contract_client,
):
    response = contract_client.get(
        "/mutated-request-id",
        headers={"X-Request-ID": "initial-id"},
    )

    body_request_id = response.json()["error"]["request_id"]
    assert REQUEST_ID_RE.fullmatch(body_request_id)
    assert response.headers["X-Request-ID"] == body_request_id


@pytest.mark.parametrize("kind", ("uuid", "integer", "object"))
def test_non_string_request_state_produces_one_canonical_response_id(
    contract_client,
    kind,
):
    response = contract_client.get(
        f"/non-string-request-id/{kind}",
        headers={"X-Request-ID": "initial-id"},
    )

    assert response.status_code == 400
    body_request_id = response.json()["error"]["request_id"]
    assert REQUEST_ID_RE.fullmatch(body_request_id)
    assert response.headers["X-Request-ID"] == body_request_id


def test_legacy_string_detail_becomes_the_message(contract_client):
    response = contract_client.get("/legacy-string")

    assert response.json()["error"]["message"] == "blocked"
    assert "details" not in response.json()["error"]


def test_legacy_dict_uses_only_its_string_message(contract_client):
    response = contract_client.get("/legacy-dict")

    assert response.json()["error"]["message"] == "blocked"
    assert "secret" not in response.text
    assert "details" not in response.json()["error"]


def test_legacy_list_is_not_echoed(contract_client):
    response = contract_client.get("/legacy-list")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "blocked" not in response.text
    assert "secret" not in response.text


def test_typed_client_error_preserves_its_contract(contract_client):
    response = contract_client.get("/typed-client-error")

    assert response.status_code == 409
    assert response.json()["error"] | {"request_id": "ignored"} == {
        "code": "resource_conflict",
        "message": "blocked",
        "retryable": False,
        "request_id": "ignored",
        "details": {"field": "name"},
    }


@pytest.mark.parametrize(
    ("path", "request_id", "secret"),
    (
        ("/typed-server-error", "typed-5xx", "typed server secret"),
        ("/http-server-error", "http-5xx", "legacy server secret"),
        ("/unknown-server-error", "unknown-5xx", "unknown server secret"),
    ),
)
def test_server_errors_hide_original_text_and_log_request_id(
    contract_client,
    caplog,
    path,
    request_id,
    secret,
):
    caplog.set_level("ERROR", logger="backend.api.errors")

    response = contract_client.get(path, headers={"X-Request-ID": request_id})

    assert response.status_code >= 500
    assert response.json()["error"]["message"] == GENERIC_5XX_MESSAGE
    assert secret not in response.text
    assert "drop" not in response.text
    assert "details" not in response.json()["error"]
    assert request_id in caplog.text
    assert secret in caplog.text
    matching_records = [
        record
        for record in caplog.records
        if record.name == "backend.api.errors"
        and request_id in record.getMessage()
    ]
    assert len(matching_records) == 1
    assert matching_records[0].exc_info is not None
    assert matching_records[0].exc_info[2] is not None


def test_already_logged_500_is_scrubbed_without_a_second_boundary_log(
    contract_client,
    caplog,
):
    caplog.set_level("ERROR", logger="backend.api.errors")

    response = contract_client.get(
        "/typed-server-error-already-logged",
        headers={"X-Request-ID": "already-logged-5xx"},
    )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": GENERIC_5XX_MESSAGE,
        "retryable": False,
        "request_id": "already-logged-5xx",
    }
    assert "already logged server secret" not in response.text
    assert "drop" not in response.text
    assert [
        record
        for record in caplog.records
        if record.name == "backend.api.errors"
    ] == []


def test_api_error_already_logged_is_internal_only():
    exc = ApiError(
        500,
        "internal_error",
        "secret",
        False,
        already_logged=True,
    )

    assert exc.already_logged is True
    error_body_properties = ErrorEnvelope.model_json_schema()["$defs"]["ErrorBody"][
        "properties"
    ]
    assert "already_logged" not in error_body_properties


def test_direct_5xx_error_response_redacts_and_logs_reason(caplog):
    caplog.set_level("ERROR", logger="backend.api.errors")

    response = error_response(
        _request(request_id="direct-5xx-id"),
        status_code=500,
        code="internal_error",
        message="direct server secret",
        retryable=False,
        details={"secret": "direct details secret"},
    )

    payload = json.loads(response.body)["error"]
    assert payload["message"] == GENERIC_5XX_MESSAGE
    assert "details" not in payload
    assert "direct server secret" not in response.body.decode("utf-8")
    assert "direct details secret" not in response.body.decode("utf-8")
    assert "direct-5xx-id" in caplog.text
    assert "direct server secret" in caplog.text


def test_validation_issues_do_not_echo_input_or_ctx(contract_client):
    response = contract_client.post("/validated", json="secret")

    issue = response.json()["error"]["details"]["issues"][0]
    assert set(issue) == {"path", "code", "message"}
    assert "secret" not in response.text


def test_validation_issue_message_does_not_echo_custom_validator_ctx(contract_client):
    response = contract_client.post(
        "/validated-ctx",
        json={"value": "sensitive-context-value"},
    )

    assert response.status_code == 422
    assert "sensitive-context-value" not in response.text

    issue = response.json()["error"]["details"]["issues"][0]
    assert issue["code"] == "validation_error"
    assert "sensitive_custom_error" not in response.text


def test_validation_issue_path_redacts_untrusted_string_segments(contract_client):
    response = contract_client.post(
        "/validated-dictionary",
        json={"values": {"malicious-dictionary-key": "not-an-integer"}},
    )

    issue = response.json()["error"]["details"]["issues"][0]
    assert issue["path"] == "body.*.*"
    assert "malicious-dictionary-key" not in response.text


@pytest.mark.parametrize("kind", ("none", "empty"))
def test_none_and_empty_details_are_omitted(contract_client, kind):
    response = contract_client.get(f"/empty-details/{kind}")

    assert "details" not in response.json()["error"]


def test_only_safe_exception_headers_survive(contract_client):
    response = contract_client.get(
        "/headers",
        headers={"X-Request-ID": "canonical-id"},
    )

    assert response.headers["Allow"] == "GET, HEAD"
    assert response.headers["Retry-After"] == "17"
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="api"'
    assert response.headers["ETag"] == '"revision-1"'
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Content-Length"] != "9999"
    assert response.headers["X-Request-ID"] == "canonical-id"
    assert "X-Unsafe" not in response.headers


def _request(method: str = "GET", request_id: str = "request-id") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "state": {"request_id": request_id},
        }
    )


def test_head_error_route_sends_no_body_through_handlers_and_middleware():
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestBoundaryMiddleware)

    @app.head("/head-error")
    async def head_error():
        raise ApiError(400, "invalid_request", "invalid", False)

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = _request("HEAD").scope
    scope["path"] = "/head-error"
    scope["raw_path"] = b"/head-error"
    scope["headers"] = [(b"x-request-id", b"head-route-id")]
    asyncio.run(app(scope, receive, send))

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    headers = {key.lower(): value for key, value in start["headers"]}
    assert start["status"] == 400
    assert body == b""
    assert headers[b"x-request-id"] == b"head-route-id"


class _OpaqueValue:
    pass


@pytest.mark.parametrize(
    ("name", "details"),
    (
        ("path", {"outer": [{"value": Path("secret")}]}),
        ("exception", {"outer": [{"value": RuntimeError("secret")}]}),
        ("instance", {"outer": [{"value": _OpaqueValue()}]}),
        ("bytes", {"outer": [{"value": b"secret"}]}),
        ("set", {"outer": [{"value": {"secret"}}]}),
        ("non-string-key", {"outer": [{1: "secret"}]}),
        ("nan", {"outer": [{"value": math.nan}]}),
        ("infinity", {"outer": [{"value": math.inf}]}),
        ("negative-infinity", {"outer": [{"value": -math.inf}]}),
    ),
)
def test_details_reject_non_json_values_recursively(name, details):
    with pytest.raises(ValueError, match="details"):
        ApiError(400, "invalid", "invalid", False, details)


def test_details_accept_nested_json_values():
    details = {
        "none": None,
        "bool": True,
        "number": 1,
        "float": 1.5,
        "text": "value",
        "array": [{"nested": False}],
    }

    assert ApiError(400, "invalid", "invalid", False, details).details == details


@pytest.mark.parametrize("kind", ("dict", "list"))
def test_details_reject_circular_references_with_stable_value_error(kind):
    if kind == "dict":
        details = {}
        details["self"] = details
    else:
        circular_list = []
        circular_list.append(circular_list)
        details = {"self": circular_list}

    with pytest.raises(ValueError, match="details.*circular"):
        ApiError(400, "invalid", "invalid", False, details)


def test_invalid_origin_is_rejected_before_the_downstream_app():
    downstream_called = False

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True

    middleware = RequestBoundaryMiddleware(
        downstream,
        allowed_origins=("http://localhost:5173",),
        allowed_origin_pattern=r"http://127\.0\.0\.1:\d+",
    )
    sent = []

    async def send(message):
        sent.append(message)

    scope = _request().scope
    scope["headers"] = [
        (b"origin", b"https://evil.example"),
        (b"x-request-id", b"origin-request"),
    ]
    asyncio.run(middleware(scope, lambda: None, send))

    assert downstream_called is False
    assert sent[0]["status"] == 403
    headers = {key.lower(): value for key, value in sent[0]["headers"]}
    assert headers[b"x-request-id"] == b"origin-request"
    payload = json.loads(sent[1]["body"])
    assert payload["error"]["request_id"] == "origin-request"


def test_middleware_forwards_response_body_messages_unchanged():
    first_body = {"type": "http.response.body", "body": b"one", "more_body": True}
    last_body = {"type": "http.response.body", "body": b"two"}

    async def downstream(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-request-id", b"spoofed")],
            }
        )
        await send(first_body)
        await send(last_body)

    middleware = RequestBoundaryMiddleware(downstream)
    sent = []

    async def send(message):
        sent.append(message)

    scope = _request(request_id="ignored").scope
    scope["headers"] = [(b"x-request-id", b"stream-request")]
    asyncio.run(middleware(scope, lambda: None, send))

    headers = {key.lower(): value for key, value in sent[0]["headers"]}
    assert headers[b"x-request-id"] == b"stream-request"
    assert sent[1] is first_body
    assert sent[2] is last_body
