from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.errors import RequestBoundaryMiddleware, install_error_handlers
from backend.api.routes import tool_approvals
from backend.core.tools.security.approval import ApprovalManager, ApprovalRequest


def _client_for(manager: ApprovalManager) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestBoundaryMiddleware)
    app.state.approval_manager = manager
    app.include_router(tool_approvals.router)
    return TestClient(app)


def _request(
    approval_id: str,
    *,
    conversation_id: str = "conversation-1",
    status: str = "pending",
) -> ApprovalRequest:
    return ApprovalRequest(
        id=approval_id,
        conversation_id=conversation_id,
        node_id="node-1",
        tool_call_id="call-1",
        tool_name="shell",
        arguments_preview='{"command":"echo ok"}',
        risk_level="medium",
        reason="test approval",
        suggested_actions=["allow_once", "allow_session", "deny"],
        status=status,
    )


def test_list_pending_tool_approvals_filters_by_conversation():
    manager = ApprovalManager()
    manager._pending["approval-1"] = _request("approval-1", conversation_id="conversation-1")
    manager._pending["approval-2"] = _request("approval-2", conversation_id="conversation-2")
    manager._pending["approval-3"] = _request(
        "approval-3",
        conversation_id="conversation-1",
        status="cancelled",
    )
    client = _client_for(manager)

    response = client.get("/tool-approvals/pending", params={"conversation_id": "conversation-1"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["approvals"]] == ["approval-1"]
    assert response.json()["approvals"][0]["expires_at"] is None


def test_decide_stale_tool_approval_returns_gone():
    client = _client_for(ApprovalManager())

    response = client.post(
        "/tool-approvals/missing/decide",
        json={"decision": "approve", "scope": "once"},
    )

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "approval_expired"
    assert response.json()["error"]["message"] == "审批请求已失效"
    assert response.json()["error"]["retryable"] is False
    assert response.json()["error"]["details"] == {
        "approval_id": "missing",
    }
    assert response.headers["X-Request-ID"] == response.json()["error"][
        "request_id"
    ]
