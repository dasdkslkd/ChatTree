from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.api.errors import ApiError

router = APIRouter()

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class OpenFileRequest(BaseModel):
    path: str = Field(min_length=1)


class OpenFileResponse(BaseModel):
    path: str


@router.post("/files/open", response_model=OpenFileResponse)
def open_file(body: OpenFileRequest) -> OpenFileResponse:
    """用系统默认软件打开一个本地文件或目录。"""
    raw = body.path.strip()
    if not os.path.isabs(raw) and not _WINDOWS_ABSOLUTE.match(raw):
        raise ApiError(400, "invalid_file_path", "File path must be absolute", False)
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
