from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from backend.api.dependencies import get_config_manager
from backend.core.config.config import Config
from backend.core.memory import MemoryStore
from backend.core.projects import normalize_projects_config


router = APIRouter()


@router.get("/memory", response_model=dict[str, Any])
async def inspect_memory(
    request: Request,
    response: Response,
    project_id: Optional[str] = None,
    config_manager: Config = Depends(get_config_manager),
):
    store: MemoryStore | None = getattr(request.app.state, "memory_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Memory store is unavailable")
    project = None
    if project_id:
        configured = normalize_projects_config(config_manager.data.get("projects"))
        project = next(
            (item for item in configured.values() if item["id"] == project_id),
            None,
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
    response.headers["Cache-Control"] = "no-store"
    return {
        "enabled": config_manager.data.get("memory", {}).get("enabled", True) is not False,
        "global": {
            "user": _public_view(store.inspect("user")),
            "machine": _public_view(store.inspect("machine")),
        },
        "project": _public_view(store.inspect("project", project_id)) if project else None,
    }


def _public_view(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "entries"}
