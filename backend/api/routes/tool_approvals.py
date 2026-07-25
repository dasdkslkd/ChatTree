from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..errors import ApiError
from ...core.tools.security.approval import ApprovalManager
from ..dependencies import get_approval_manager

router = APIRouter()


class ToolApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]
    conversation_id: str
    node_id: str
    scope: Literal["once", "session"] = "once"
    remember_rule: bool = False


@router.post("/tool-approvals/tool-calls/{tool_call_id}/decide")
async def decide_tool_approval_by_tool_call(
    tool_call_id: str,
    request: ToolApprovalDecisionRequest,
    approval_manager: ApprovalManager = Depends(get_approval_manager),
):
    approval = next(
        (
            item
            for item in approval_manager.list_pending()
            if item.get("tool_call_id") == tool_call_id
            and item.get("conversation_id") == request.conversation_id
            and item.get("node_id") == request.node_id
        ),
        None,
    )
    if approval is None:
        raise ApiError(
            410,
            "approval_expired",
            "审批请求已失效",
            False,
            details={"tool_call_id": tool_call_id},
        )

    approval_id = str(approval["id"])
    try:
        decision = approval_manager.decide(
            approval_id,
            decision=request.decision,
            scope=request.scope,
        )
    except KeyError:
        raise ApiError(
            410,
            "approval_expired",
            "审批请求已失效",
            False,
            details={"tool_call_id": tool_call_id},
        )

    return {
        "tool_call_id": tool_call_id,
        "status": decision.status,
        "scope": decision.scope,
    }
