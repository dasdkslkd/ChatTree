from __future__ import annotations

from pathlib import Path
from typing import Union


PathLike = Union[str, Path]


class CapabilityPathError(ValueError):
    """Raised when a capability path escapes its configured root."""


def read_text_utf8(path: PathLike) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text_utf8(path: PathLike, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def resolve_inside_root(root: PathLike, candidate: PathLike) -> Path:
    resolved_root = Path(root).resolve()
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        candidate_path = resolved_root / candidate_path

    resolved_candidate = candidate_path.resolve()
    if not _is_relative_to(resolved_candidate, resolved_root):
        raise CapabilityPathError(
            f"Capability path escapes root: {resolved_candidate} is not inside {resolved_root}"
        )

    return resolved_candidate


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False
