from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from backend.api.dependencies import get_config_manager
from backend.api.errors import ApiError
from backend.core.projects import normalize_projects_config
from backend.core.workspace import normalize_workspace

router = APIRouter()

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_MAX_TEXT_SIZE = 256 * 1024
_HIDDEN_PREFIXES = (".", "__pycache__", "node_modules")
_HIDDEN_NAMES = {".venv", "venv", ".git", "dist", "build", "__pycache__"}


class OpenFileRequest(BaseModel):
    path: str = Field(min_length=1)


class OpenFileResponse(BaseModel):
    path: str


@router.post("/files/open", response_model=OpenFileResponse)
def open_file(body: OpenFileRequest) -> OpenFileResponse:
    """用系统默认软件打开一个本地文件或目录。"""
    raw = body.path.strip()
    if not (
        os.path.isabs(raw)
        or _WINDOWS_ABSOLUTE.match(raw)
        or "/" in raw
        or "\\" in raw
    ):
        raise ApiError(400, "invalid_file_path", "Path must be absolute or contain a separator", False)
    # 相对路径按服务器工作目录解析
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise ApiError(
            404,
            "file_not_found",
            "File or directory does not exist",
            False,
            {"path": str(path)},
        )
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise ApiError(
            500,
            "open_failed",
            f"Failed to open path: {exc}",
            True,
            {"path": str(path)},
        ) from exc
    return OpenFileResponse(path=str(path))


def _workspace_roots(config_manager) -> list[str] | None:
    """返回当前配置中所有项目 workspace 的根目录列表；无可用项目时返回 None。"""
    projects = normalize_projects_config(getattr(config_manager, "data", None).get("projects"))
    roots: list[str] = []
    for project_path in projects:
        workspace = normalize_workspace({"cwd": project_path, "workspace_roots": [project_path]})
        roots.extend(workspace["workspace_roots"])
    return roots or None


def _resolve_project_path(raw: str, config_manager) -> Path:
    """将请求路径解析为绝对路径，并校验其落在某项目 workspace 根目录内。"""
    text = raw.strip()
    path = Path(text).expanduser().resolve()
    roots = [
        Path(root).expanduser().resolve()
        for root in (_workspace_roots(config_manager) or [str(Path.cwd())])
    ]
    if not any(path == root or root in path.parents for root in roots):
        raise ApiError(403, "file_outside_workspace", "Path is outside the project workspace", False, {"path": str(path)})
    return path


@router.get("/files/list")
async def list_directory(
    path: str = Query(...),
    config_manager=Depends(get_config_manager),
) -> dict:
    """列出目录内容（单层），供文件树懒加载使用。"""
    dir_path = _resolve_project_path(path, config_manager)
    if not dir_path.is_dir():
        raise ApiError(404, "file_not_found", "Directory does not exist", False, {"path": str(dir_path)})
    entries = []
    try:
        for child in dir_path.iterdir():
            name = child.name
            if name.startswith(_HIDDEN_PREFIXES) or name in _HIDDEN_NAMES:
                continue
            entries.append({
                "name": name,
                "type": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else 0,
            })
    except OSError as exc:
        raise ApiError(500, "list_failed", f"Failed to list directory: {exc}", True, {"path": str(dir_path)}) from exc
    entries.sort(key=lambda entry: (entry["type"] != "dir", entry["name"].lower()))
    return {"path": str(dir_path), "entries": entries}


@router.get("/files/content")
async def read_file(
    path: str = Query(...),
    config_manager=Depends(get_config_manager),
) -> dict:
    """读取文本文件内容；二进制或超长文件只返回元信息。"""
    file_path = _resolve_project_path(path, config_manager)
    if not file_path.is_file():
        raise ApiError(404, "file_not_found", "File does not exist", False, {"path": str(file_path)})
    size = file_path.stat().st_size
    try:
        with open(file_path, "rb") as handle:
            head = handle.read(8192)
            if b"\x00" in head:
                return {"path": str(file_path), "size": size, "binary": True, "truncated": False, "content": ""}
    except OSError as exc:
        raise ApiError(500, "read_failed", f"Failed to read file: {exc}", True, {"path": str(file_path)}) from exc
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read(_MAX_TEXT_SIZE)
    except OSError as exc:
        raise ApiError(500, "read_failed", f"Failed to read file: {exc}", True, {"path": str(file_path)}) from exc
    return {
        "path": str(file_path),
        "size": size,
        "binary": False,
        "truncated": len(raw) < size,
        "content": raw.decode("utf-8", errors="replace"),
    }
