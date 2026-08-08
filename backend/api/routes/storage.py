# backend/api/routes/storage.py - 存储监控与手动压缩
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status

from backend.api.errors import ApiError
from backend.core.persistence.database import SQLitePersistence
from backend.core.server.admission import (
    MutationAdmission,
    MutationAdmissionClosed,
    ServerBusyError,
    ServerBusyState,
)
from backend.api.dependencies import get_persistence, get_run_manager


router = APIRouter()


def _dir_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


@router.get("/storage/stats", response_model=Dict[str, Any])
async def storage_stats(
    persistence: SQLitePersistence = Depends(get_persistence),
    run_manager=Depends(get_run_manager),
) -> Dict[str, Any]:
    """存储占用与碎片统计。"""
    stats = persistence.stats()
    conversations_dir = persistence.home / "conversations"
    runs_count = 0
    if conversations_dir.is_dir():
        for run_dir in conversations_dir.glob("*/runs"):
            runs_count += len(list(run_dir.glob("*.jsonl")))
    active_runs = len(run_manager.list_active())
    return {
        **stats,
        "home": str(persistence.home),
        "conversations_dir_bytes": _dir_bytes(conversations_dir),
        "run_journals_count": runs_count,
        "active_runs": active_runs,
        "reclaimable_bytes": stats["freelist_bytes"],
        "recommended": stats["freelist_bytes"] > 5 * 1024 * 1024,
    }


@router.post(
    "/storage/compact",
    response_model=Dict[str, Any],
    status_code=status.HTTP_202_ACCEPTED,
)
async def storage_compact(
    request: Request,
    background_tasks: BackgroundTasks,
    persistence: SQLitePersistence = Depends(get_persistence),
    run_manager=Depends(get_run_manager),
) -> Dict[str, Any]:
    """无运行中任务时压缩并退出服务（VACUUM 需要独占锁，结束后自动退出）。"""
    admission = getattr(request.app.state, "mutation_admission", None)
    request_shutdown = getattr(request.app.state, "request_shutdown", None)
    if not isinstance(admission, MutationAdmission) or not callable(request_shutdown):
        raise ApiError(
            503, "compact_unavailable", "Storage compact is not available", True
        )

    def inspect_busy():
        active = run_manager.list_active()
        return ServerBusyState(
            active_run_ids=tuple(
                str(run["run_id"]) for run in active if run.get("run_id") is not None
            )
        )

    try:
        await admission.close_if_idle(inspect_busy)
    except MutationAdmissionClosed as exc:
        raise ApiError(
            503, "server_shutting_down", "Server is already shutting down", True
        ) from exc
    except ServerBusyError as exc:
        raise ApiError(
            409,
            "server_busy",
            "Server has active runs",
            True,
            {"active_run_ids": list(exc.state.active_run_ids)},
        ) from exc

    # 准入已关闭（之后无法启动新 run），此时无并发写，安全执行全量 VACUUM。
    # VACUUM 占用且独占数据库，放线程池执行避免阻塞事件循环中的读请求。
    try:
        reclaimed = await asyncio.to_thread(
            persistence.reclaim_blobs, compact=True
        )
        db_bytes = persistence.stats()["db_file_bytes"]
    except Exception as exc:
        # 失败时恢复准入，服务保持可用而非卡死在半关闭状态（可重试）。
        admission._open = True
        raise ApiError(
            503,
            "compact_failed",
            "Storage compact failed",
            True,
        ) from exc
    # 响应发给代理（launcher/proxy）之后再触发退出，避免连接被提前中断返回 502。
    background_tasks.add_task(request_shutdown)
    return {
        "reclaimed_blobs": reclaimed,
        "db_file_bytes_after": db_bytes,
        "status": "compacted_and_stopping",
    }