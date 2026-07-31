from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.perf import configure_profiler, get_profiler
from backend.core.home import resolve_chattree_home

router = APIRouter()


class PerfEventBatch(BaseModel):
    events: list[dict[str, Any]] = []


class PerfConfigUpdate(BaseModel):
    enabled: bool | None = None
    perf_run_id: str | None = None
    output_dir: str | None = None
    sample_rate: float | None = None


@router.get("/perf/config")
async def get_perf_config() -> dict[str, Any]:
    profiler = get_profiler()
    return profiler.config.public_dict()


@router.post("/perf/config")
async def update_perf_config(update: PerfConfigUpdate) -> dict[str, Any]:
    current = get_profiler().config
    sample_rate = current.sample_rate
    if update.sample_rate is not None:
        sample_rate = min(1.0, max(0.0, float(update.sample_rate)))
    output_dir = current.output_dir
    if update.output_dir:
        output_dir = _safe_output_dir(update.output_dir)
    next_config = replace(
        current,
        enabled=current.enabled if update.enabled is None else update.enabled,
        perf_run_id=current.perf_run_id if not update.perf_run_id else update.perf_run_id,
        output_dir=output_dir,
        sample_rate=sample_rate,
    )
    profiler = configure_profiler(next_config)
    return profiler.config.public_dict()


@router.post("/perf/events")
async def record_frontend_perf_events(batch: PerfEventBatch) -> dict[str, Any]:
    profiler = get_profiler()
    accepted = profiler.record_frontend_events(batch.events)
    return {"accepted": accepted, "enabled": profiler.enabled}


def _safe_output_dir(value: str) -> Path:
    root = (resolve_chattree_home() / "perf").resolve()
    candidate = Path(value).expanduser().resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"performance output_dir must be under {root}",
        ) from exc
    return candidate
