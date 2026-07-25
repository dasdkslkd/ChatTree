from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.api.routes.tool_approvals as tool_approval_routes
from backend.api.errors import install_error_handlers


def _client(manager) -> TestClient:
    app = FastAPI()
    app.state.approval_manager = manager
    install_error_handlers(app)
    app.include_router(tool_approval_routes.router)
    return TestClient(app, raise_server_exceptions=False)


def test_decide_tool_approval_by_tool_call_id_uses_pending_request_id():
    calls: list[tuple[str, str, str]] = []
    manager = SimpleNamespace(
        list_pending=lambda: [
            {
                "id": "approval-1",
                "conversation_id": "conv-1",
                "node_id": "node-1",
                "tool_call_id": "call-shell",
            }
        ],
        decide=lambda approval_id, decision, scope: calls.append((approval_id, decision, scope))
        or SimpleNamespace(status="approved", scope=scope),
    )

    response = _client(manager).post(
        "/tool-approvals/tool-calls/call-shell/decide",
        json={
            "decision": "approve",
            "conversation_id": "conv-1",
            "node_id": "node-1",
            "scope": "once",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "tool_call_id": "call-shell",
        "status": "approved",
        "scope": "once",
    }
    assert calls == [("approval-1", "approve", "once")]


def test_decide_tool_approval_scopes_tool_call_id_to_conversation_and_node():
    calls: list[tuple[str, str, str]] = []
    manager = SimpleNamespace(
        list_pending=lambda: [
            {
                "id": "approval-a",
                "conversation_id": "conv-a",
                "node_id": "node-a",
                "tool_call_id": "call-shell",
            },
            {
                "id": "approval-b",
                "conversation_id": "conv-b",
                "node_id": "node-b",
                "tool_call_id": "call-shell",
            },
        ],
        decide=lambda approval_id, decision, scope: calls.append((approval_id, decision, scope))
        or SimpleNamespace(status="denied", scope=scope),
    )

    response = _client(manager).post(
        "/tool-approvals/tool-calls/call-shell/decide",
        json={
            "decision": "deny",
            "conversation_id": "conv-b",
            "node_id": "node-b",
            "scope": "once",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "tool_call_id": "call-shell",
        "status": "denied",
        "scope": "once",
    }
    assert calls == [("approval-b", "deny", "once")]


def test_decide_tool_approval_by_tool_call_id_returns_expired_for_missing_call():
    manager = SimpleNamespace(
        list_pending=lambda: [{"id": "approval-1", "tool_call_id": "other-call"}],
        decide=lambda approval_id, decision, scope: None,
    )

    response = _client(manager).post(
        "/tool-approvals/tool-calls/call-shell/decide",
        json={
            "decision": "deny",
            "conversation_id": "conv-1",
            "node_id": "node-1",
            "scope": "once",
        },
    )

    assert response.status_code == 410
    payload = response.json()
    assert payload["error"]["code"] == "approval_expired"
    assert payload["error"]["details"] == {"tool_call_id": "call-shell"}


def test_decide_tool_approval_requires_node_id():
    manager = SimpleNamespace(
        list_pending=lambda: [
            {
                "id": "approval-1",
                "conversation_id": "conv-1",
                "node_id": "node-1",
                "tool_call_id": "call-shell",
            }
        ],
        decide=lambda approval_id, decision, scope: None,
    )

    response = _client(manager).post(
        "/tool-approvals/tool-calls/call-shell/decide",
        json={
            "decision": "deny",
            "conversation_id": "conv-1",
            "scope": "once",
        },
    )

    assert response.status_code == 422
