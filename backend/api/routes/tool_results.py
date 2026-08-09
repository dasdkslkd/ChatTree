from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.dependencies import get_persistence
from backend.api.errors import ApiError
from backend.core.persistence.repository import ChatRepository

router = APIRouter()


def _tool_result_slice(
    repository: ChatRepository,
    tool_result_id: str,
    *,
    offset: int,
    limit: int,
) -> dict[str, Any] | None:
    try:
        return repository.get_tool_result_slice(tool_result_id, offset=offset, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="工具结果 blob 不存在") from exc
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=500, detail="工具结果 blob 无法读取") from exc


@router.get("/tool-results/{tool_result_id}")
async def get_tool_result(
    request: Request,
    tool_result_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(16000, ge=1),
    persistence: Any = Depends(get_persistence),
):
    repository = getattr(request.app.state, "chat_repository", None)
    if not isinstance(repository, ChatRepository):
        repository = ChatRepository(persistence)
    result = _tool_result_slice(repository, tool_result_id, offset=offset, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="工具结果不存在")
    return result


@router.post("/tool-results/{tool_result_id}/revert")
async def revert_tool_result(
    request: Request,
    tool_result_id: str,
    persistence: Any = Depends(get_persistence),
):
    """回退写文件工具造成的变更：把旧内容快照写回文件（原不存在则删除）。"""
    repository = getattr(request.app.state, "chat_repository", None)
    if not isinstance(repository, ChatRepository):
        repository = ChatRepository(persistence)
    result = _tool_result_slice(repository, tool_result_id, offset=0, limit=1)
    if result is None:
        raise HTTPException(status_code=404, detail="工具结果不存在")
    snapshot = result.get("diff_before")
    if not isinstance(snapshot, dict) or not snapshot:
        raise ApiError(409, "not_revertible", "该工具结果没有可回退的变更记录", False)

    reverted = []
    try:
        for raw_path, entry in snapshot.items():
            path = Path(raw_path)
            before = str((entry or {}).get("before") or "")
            existed = bool((entry or {}).get("existed"))
            if existed:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(before, encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
            reverted.append(str(path))
    except OSError as exc:
        raise ApiError(500, "revert_failed", f"回退失败: {exc}", True, details={"reverted": reverted}) from exc
    return {"tool_result_id": tool_result_id, "reverted": reverted}
