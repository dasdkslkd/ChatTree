from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...core.tools.security.approval import ApprovalManager
from ..dependencies import get_approval_manager

router = APIRouter()


class ToolApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]
    scope: Literal["once", "session"] = "once"
    remember_rule: bool = False


@router.get("/tool-approvals/pending")
async def list_pending_tool_approvals(
    conversation_id: str | None = Query(default=None),
    approval_manager: ApprovalManager = Depends(get_approval_manager),
):
    return {
        "approvals": approval_manager.list_pending(conversation_id=conversation_id),
    }


@router.post("/tool-approvals/{approval_id}/decide")
async def decide_tool_approval(
    approval_id: str,
    request: ToolApprovalDecisionRequest,
    approval_manager: ApprovalManager = Depends(get_approval_manager),
):
    try:
        decision = approval_manager.decide(
            approval_id,
            decision=request.decision,
            scope=request.scope,
        )
    except KeyError:
        raise HTTPException(status_code=410, detail="审批请求已失效")

    return {
        "approval_id": approval_id,
        "status": decision.status,
        "scope": decision.scope,
    }
