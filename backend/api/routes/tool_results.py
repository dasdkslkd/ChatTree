from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.dependencies import get_persistence
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
