from fastapi import APIRouter, Depends, HTTPException, Query

from ..dependencies import get_tool_manager

router = APIRouter()


@router.get("/api/tool-results/{tool_result_id}", include_in_schema=False)
@router.get("/tool-results/{tool_result_id}")
async def get_tool_result(
    tool_result_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(16000, ge=1),
    tool_manager=Depends(get_tool_manager),
):
    store = getattr(tool_manager, "tool_result_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="工具结果存储未初始化")

    result = store.read_slice(tool_result_id, offset=offset, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="工具结果不存在")
    return result
