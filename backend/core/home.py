from __future__ import annotations

import os
from pathlib import Path


_NativePath = type(Path.cwd())


def _native_path(value: str | os.PathLike[str]) -> Path:
    return _NativePath(value)


def _is_windows() -> bool:
    return os.name == "nt"


def resolve_chattree_home(value: str | os.PathLike[str] | None = None) -> Path:
    explicit = value or os.environ.get("CHATTREE_HOME")
    if explicit:
        return _native_path(explicit).expanduser().resolve()
    if _is_windows():
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return (_native_path(userprofile) / ".chattree").resolve()
    home = os.environ.get("HOME")
    if home:
        return (_native_path(home) / ".chattree").resolve()
    return (_native_path.home() / ".chattree").resolve()
