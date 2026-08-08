# backend/api/routes/usage.py - 模型 Token 用量统计
from __future__ import annotations

import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from backend.core.persistence.database import SQLitePersistence
from backend.api.dependencies import get_persistence


router = APIRouter()

# 时间范围（秒）：1d / 7d / 30d / 1y / total
_PERIOD_SECONDS = {
    "1d": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "1y": 365 * 86400,
    "total": None,
}


@router.get("/usage/stats", response_model=Dict[str, Any])
async def usage_stats(
    persistence: SQLitePersistence = Depends(get_persistence),
    period: str = Query("1d", pattern="^(1d|7d|30d|1y|total)$"),
) -> Dict[str, Any]:
    """按模型汇总 Token 总消耗与缓存命中率（读取持久化 usage_stats 表）。

    累计量独立于会话/消息，删除会话不影响历史统计；period 控制时间范围。
    """
    seconds = _PERIOD_SECONDS.get(period)
    cutoff_day = None
    if seconds is not None:
        cutoff_day = time.strftime(
            "%Y-%m-%d", time.gmtime(int(time.time()) - seconds)
        )

    where = ""
    params: List[Any] = []
    if cutoff_day is not None:
        where = "WHERE day >= ?"
        params.append(cutoff_day)

    with persistence.connect() as conn:
        records = conn.execute(
            f"""
            SELECT
              model_id,
              SUM(calls) AS calls,
              SUM(input_tokens) AS input_tokens,
              SUM(output_tokens) AS output_tokens,
              SUM(total_tokens) AS total_tokens,
              SUM(cache_hit_tokens) AS cache_hit_tokens,
              SUM(cache_context_tokens) AS cache_context_tokens
            FROM usage_stats
            {where}
            GROUP BY model_id
            """,
            params,
        ).fetchall()

    models = []
    for row in records:
        context = int(row["cache_context_tokens"])
        hit = int(row["cache_hit_tokens"])
        models.append(
            {
                "model": row["model_id"],
                "calls": int(row["calls"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "cache_hit_tokens": hit,
                "cache_hit_rate": (hit / context) if context > 0 else None,
            }
        )
    models.sort(key=lambda m: m["total_tokens"], reverse=True)

    totals = {
        "calls": sum(m["calls"] for m in models),
        "input_tokens": sum(m["input_tokens"] for m in models),
        "output_tokens": sum(m["output_tokens"] for m in models),
        "total_tokens": sum(m["total_tokens"] for m in models),
        "cache_hit_tokens": sum(m["cache_hit_tokens"] for m in models),
        "cache_context_tokens": sum(int(r["cache_context_tokens"]) for r in records),
    }
    totals["cache_hit_rate"] = (
        totals["cache_hit_tokens"] / totals["cache_context_tokens"]
        if totals["cache_context_tokens"] > 0
        else None
    )
    return {"models": models, "totals": totals}