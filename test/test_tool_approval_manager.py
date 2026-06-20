import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core.tools.security.approval import ApprovalDecision, ApprovalManager, ApprovalRequest


def make_request(
    approval_id="approval-1",
    node_id="node-1",
    conversation_id="conv-1",
    tool_name="mcp__filesystem__write_file",
):
    return ApprovalRequest(
        id=approval_id,
        conversation_id=conversation_id,
        node_id=node_id,
        tool_call_id="call-1",
        tool_name=tool_name,
        arguments_preview='{"path":"src/example.py"}',
        risk_level="high",
        reason="MCP tools require approval",
        suggested_actions=["allow_once", "allow_session", "deny"],
    )


async def _approved_case():
    manager = ApprovalManager(timeout_seconds=5)
    request = make_request()
    wait_task = asyncio.create_task(manager.request_and_wait(request))
    await asyncio.sleep(0)

    decision = manager.decide("approval-1", decision="approve", scope="once")
    result = await wait_task

    assert isinstance(decision, ApprovalDecision)
    assert decision.status == "approved"
    assert decision.scope == "once"
    assert result.status == "approved"
    assert result.scope == "once"
    assert manager.get("approval-1") is None


def test_approval_manager_resolves_approved_request():
    asyncio.run(_approved_case())


async def _begin_request_registers_before_waiting_case():
    manager = ApprovalManager(timeout_seconds=5)
    request = make_request()

    wait_task = manager.begin_request(request)
    decision = manager.decide("approval-1", decision="approve", scope="once")
    result = await wait_task

    assert decision.status == "approved"
    assert result.status == "approved"
    assert result.scope == "once"
    assert manager.get("approval-1") is None


def test_begin_request_registers_before_waiting():
    asyncio.run(_begin_request_registers_before_waiting_case())


async def _denied_case():
    manager = ApprovalManager(timeout_seconds=5)
    request = make_request()
    wait_task = asyncio.create_task(manager.request_and_wait(request))
    await asyncio.sleep(0)

    decision = manager.decide("approval-1", decision="deny", scope="once")
    result = await wait_task

    assert decision.status == "denied"
    assert decision.scope == "once"
    assert result.status == "denied"
    assert result.scope == "once"


def test_approval_manager_resolves_denied_request():
    asyncio.run(_denied_case())


async def _cancel_for_node_case():
    manager = ApprovalManager(timeout_seconds=5)
    matching_request = make_request("approval-1", node_id="node-1")
    other_request = make_request("approval-2", node_id="node-2")
    matching_task = asyncio.create_task(manager.request_and_wait(matching_request))
    other_task = asyncio.create_task(manager.request_and_wait(other_request))
    await asyncio.sleep(0)

    manager.cancel_for_node("node-1")
    matching_result = await matching_task

    assert matching_result.status == "cancelled"
    assert matching_result.scope is None
    assert manager.get("approval-2") == other_request

    manager.cancel_for_node("node-2")
    other_result = await other_task
    assert other_result.status == "cancelled"


def test_cancel_for_node_cancels_pending_request():
    asyncio.run(_cancel_for_node_case())


async def _session_allow_scoped_by_conversation_case():
    manager = ApprovalManager(timeout_seconds=5)
    request = make_request(
        conversation_id="conv-1",
        tool_name="mcp__filesystem__write_file",
    )
    wait_task = asyncio.create_task(manager.request_and_wait(request))
    await asyncio.sleep(0)

    decision = manager.decide("approval-1", decision="approve", scope="session")
    result = await wait_task

    assert decision.status == "approved"
    assert decision.scope == "session"
    assert result.status == "approved"
    assert result.scope == "session"
    assert manager.is_session_allowed("conv-1", "mcp__filesystem__write_file")
    assert not manager.is_session_allowed("conv-2", "mcp__filesystem__write_file")


def test_session_allow_is_scoped_by_conversation():
    asyncio.run(_session_allow_scoped_by_conversation_case())


async def _timeout_case():
    manager = ApprovalManager(timeout_seconds=0.01)
    request = make_request()

    result = await manager.request_and_wait(request)

    assert result.status == "expired"
    assert result.scope is None
    assert request.status == "expired"
    assert manager.get("approval-1") is None


def test_timeout_returns_expired_and_cleans_pending_request():
    asyncio.run(_timeout_case())


def test_unknown_approval_id_decide_raises_key_error():
    manager = ApprovalManager(timeout_seconds=5)

    with pytest.raises(KeyError):
        manager.decide("missing-approval", decision="approve", scope="once")
