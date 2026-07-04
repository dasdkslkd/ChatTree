from __future__ import annotations

import inspect
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.dependencies import get_tool_manager
from backend.core.persistence.blob_store import BlobStore

router = APIRouter()


def _sqlite_tool_result_slice(
    persistence: Any,
    tool_result_id: str,
    *,
    offset: int,
    limit: int,
) -> dict[str, Any] | None:
    if persistence is None or not hasattr(persistence, "connect"):
        return None

    with persistence.connect() as conn:
        table_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tool_results)").fetchall()
        }
        if not table_columns:
            return None
        if "output_inline" in table_columns:
            row = conn.execute(
                """
                SELECT
                  tool_results.id,
                  tool_results.tool_call_id,
                  tool_results.output_inline AS output_text,
                  tool_results.output_preview,
                  tool_results.output_blob_id,
                  tool_results.output_size,
                  tool_results.metadata_json,
                  tool_calls.name AS tool_name
                FROM tool_results
                LEFT JOIN tool_calls
                  ON tool_calls.conversation_id = tool_results.conversation_id
                 AND tool_calls.id = tool_results.tool_call_id
                WHERE tool_results.id = ?
                """,
                (tool_result_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT
                  tool_results.id,
                  tool_results.tool_call_id,
                  tool_results.output_preview AS output_text,
                  tool_results.output_preview,
                  tool_results.output_blob_id,
                  tool_results.output_size,
                  tool_results.metadata_json,
                  tool_calls.name AS tool_name
                FROM tool_results
                LEFT JOIN tool_calls
                  ON tool_calls.conversation_id = tool_results.conversation_id
                 AND tool_calls.id = tool_results.tool_call_id
                WHERE tool_results.id = ?
                """,
                (tool_result_id,),
            ).fetchone()
    if row is None:
        return None

    content = row["output_text"] or ""
    if row["output_blob_id"]:
        try:
            content = BlobStore(persistence).get_text(row["output_blob_id"])
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="工具结果 blob 不存在",
            ) from exc
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=500,
                detail="工具结果 blob 无法读取",
            ) from exc
    metadata = _load_metadata(row["metadata_json"])
    tool_name = row["tool_name"] or metadata.get("tool_name")

    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or 16000))
    chunk = content[offset:offset + limit]
    next_offset = offset + len(chunk)
    total_chars = len(content)
    if not total_chars and row["output_size"]:
        total_chars = int(row["output_size"])
    return {
        "tool_result_id": tool_result_id,
        "tool_name": tool_name,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset if next_offset < total_chars else None,
        "total_chars": total_chars,
        "has_more": next_offset < total_chars,
        "content": chunk,
    }


def _load_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


async def _get_optional_tool_manager(request: Request) -> Any | None:
    override = request.app.dependency_overrides.get(get_tool_manager)
    if override is not None:
        try:
            signature = inspect.signature(override)
            value = override(request) if signature.parameters else override()
        except (TypeError, ValueError):
            value = override()
        if inspect.isawaitable(value):
            return await value
        return value

    try:
        return get_tool_manager(request)
    except HTTPException as exc:
        if exc.status_code == 500 and exc.detail == "工具管理器未初始化":
            return None
        raise


@router.get("/api/tool-results/{tool_result_id}", include_in_schema=False)
@router.get("/tool-results/{tool_result_id}")
async def get_tool_result(
    request: Request,
    tool_result_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(16000, ge=1),
    tool_manager: Any | None = Depends(_get_optional_tool_manager),
):
    store = getattr(tool_manager, "tool_result_store", None)
    result = None
    if store is not None:
        result = store.read_slice(tool_result_id, offset=offset, limit=limit)
    if result is None:
        result = _sqlite_tool_result_slice(
            getattr(request.app.state, "persistence", None),
            tool_result_id,
            offset=offset,
            limit=limit,
        )
    if result is None:
        if store is None and not hasattr(request.app.state, "persistence"):
            raise HTTPException(status_code=500, detail="工具结果存储未初始化")
        raise HTTPException(status_code=404, detail="工具结果不存在")
    return result
