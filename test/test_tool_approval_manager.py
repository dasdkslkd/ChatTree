import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core.tools.security.approval import ApprovalManager, ApprovalRequest


def make_request(approval_id="approval-1", node_id="node-1"):
    return ApprovalRequest(
        id=approval_id,
        conversation_id="conv-1",
        node_id=node_id,
        tool_call_id="call-1",
        tool_name="mcp__filesystem__write_file",
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

    manager.decide("approval-1", decision="approve", scope="once")
    result = await wait_task

    assert result.status == "approved"
    assert result.scope == "once"


def test_approval_manager_resolves_approved_request():
    asyncio.run(_approved_case())


async def _denied_case():
    manager = ApprovalManager(timeout_seconds=5)
    request = make_request()
    wait_task = asyncio.create_task(manager.request_and_wait(request))
    await asyncio.sleep(0)

    manager.decide("approval-1", decision="deny", scope="once")
    result = await wait_task

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


def test_unknown_approval_id_decide_raises_key_error():
    manager = ApprovalManager(timeout_seconds=5)

    with pytest.raises(KeyError):
        manager.decide("missing-approval", decision="approve", scope="once")
