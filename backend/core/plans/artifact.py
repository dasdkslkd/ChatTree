from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


class PlanPatchError(ValueError):
    pass


@dataclass(frozen=True)
class PlanArtifactUpdate:
    path: Path
    content: str
    revision: int


class PlanArtifactStore:
    def __init__(self, home: str | Path) -> None:
        self.home = Path(home)

    def path_for(self, *, conversation_id: str, plan_id: str) -> Path:
        return (
            self.home
            / "conversations"
            / _safe_segment(conversation_id)
            / "plans"
            / f"{_safe_segment(plan_id)}.md"
        )

    def update(
        self,
        *,
        conversation_id: str,
        plan_id: str,
        mode: str,
        content: str | None = None,
        patch: str | None = None,
        revision: int = 0,
    ) -> PlanArtifactUpdate:
        path = self.path_for(conversation_id=conversation_id, plan_id=plan_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if mode == "replace":
            next_content = content or ""
        elif mode == "apply_patch":
            next_content = apply_plan_patch(current, patch or "")
        else:
            raise ValueError("mode must be replace or apply_patch")
        path.write_text(next_content, encoding="utf-8", newline="\n")
        next_revision = revision + 1 if revision > 0 else (2 if current else 1)
        return PlanArtifactUpdate(path=path, content=next_content, revision=next_revision)


def apply_plan_patch(current: str, patch: str) -> str:
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise PlanPatchError("patch must use ChatTree apply_patch format")
    body = lines[1:-1]
    if not body or not body[0].startswith("*** Update File: "):
        raise PlanPatchError("patch must update the controlled plan file")
    target = body[0].removeprefix("*** Update File: ").strip()
    if target not in {"plan.md", "plan"}:
        raise PlanPatchError("patch must update the controlled plan file")
    if not any(line.startswith("@@") for line in body):
        raise PlanPatchError("patch must include a hunk")

    old_lines = current.splitlines(keepends=False)
    out: list[str] = []
    cursor = 0
    hunk: list[str] = []
    for line in body[1:]:
        if line.startswith("@@"):
            if hunk:
                cursor = _apply_hunk(old_lines, out, cursor, hunk)
                hunk = []
            continue
        hunk.append(line)
    if hunk:
        cursor = _apply_hunk(old_lines, out, cursor, hunk)
    out.extend(old_lines[cursor:])
    return "\n".join(out) + ("\n" if out else "")


def _apply_hunk(old_lines: list[str], out: list[str], cursor: int, hunk: list[str]) -> int:
    context = [line[1:] for line in hunk if line.startswith((" ", "-"))]
    start = _find_context(old_lines, context, cursor)
    out.extend(old_lines[cursor:start])
    pos = start
    for line in hunk:
        if line.startswith(" "):
            expected = line[1:]
            if pos >= len(old_lines) or old_lines[pos] != expected:
                raise PlanPatchError("patch context does not match")
            out.append(old_lines[pos])
            pos += 1
        elif line.startswith("-"):
            expected = line[1:]
            if pos >= len(old_lines) or old_lines[pos] != expected:
                raise PlanPatchError("patch removal does not match")
            pos += 1
        elif line.startswith("+"):
            out.append(line[1:])
        else:
            raise PlanPatchError("unsupported patch line")
    return pos


def _find_context(old_lines: list[str], context: list[str], cursor: int) -> int:
    if not context:
        return cursor
    for index in range(cursor, len(old_lines) - len(context) + 1):
        if old_lines[index : index + len(context)] == context:
            return index
    raise PlanPatchError("patch context not found")


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).replace("_.._", "___").strip("._")
    return safe or "unknown"
