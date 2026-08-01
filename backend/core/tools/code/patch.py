from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import CodeWorkspace


@dataclass(frozen=True)
class _PatchHunk:
    old_index: int
    remove: List[str]
    add: List[str]
    header: str


@dataclass(frozen=True)
class _FilePatch:
    path: str
    hunks: List[_PatchHunk]


def _patch_path(raw: str) -> str:
    path = raw.strip()
    if path == "/dev/null":
        raise ValueError("file deletion patches are not supported")
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    parsed = Path(path)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("patch path must stay inside the workspace")
    return path


def _read_targets(kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
    targets = kwargs.get("targets")
    if isinstance(targets, list):
        normalized: List[Dict[str, Any]] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            path = str(target.get("path") or "")
            if path:
                normalized.append(dict(target, path=path))
        if normalized:
            return normalized
    path = kwargs.get("path")
    return [{"path": str(path)}] if path else []


def _read_payload(
    *,
    workspace: CodeWorkspace,
    target: Path,
    start_line: int,
    line_count: Optional[int],
    max_chars: int,
    output_format: str,
) -> Dict[str, Any]:
    if not target.exists() or not target.is_file():
        return {"path": workspace.relative(target), "error": {"type": "not_found", "message": "file not found"}}
    try:
        with target.open("r", encoding="utf-8") as handle:
            selected: List[str] = []
            current_line = 0
            chars = 0
            truncated = False
            for raw_line in handle:
                current_line += 1
                in_range = current_line >= start_line and (line_count is None or len(selected) < line_count)
                if not in_range:
                    if current_line < start_line:
                        continue
                    if line_count is not None and len(selected) >= line_count:
                        truncated = True
                    continue
                remaining = max_chars - chars
                if remaining <= 0:
                    truncated = True
                    continue
                if len(raw_line) > remaining:
                    selected.append(raw_line[:remaining])
                    chars += remaining
                    truncated = True
                    continue
                selected.append(raw_line)
                chars += len(raw_line)
    except UnicodeDecodeError:
        return {"path": workspace.relative(target), "error": {"type": "not_utf8", "message": "file is not valid UTF-8 text"}}
    selected_text = "".join(selected)
    payload: Dict[str, Any] = {
        "path": workspace.relative(target),
        "start_line": start_line,
        "line_count": len(selected),
        "total_lines": current_line,
        "truncated": truncated,
    }
    if output_format == "json":
        payload["lines"] = [
            {"line": start_line + index, "text": _strip_line_ending(line)}
            for index, line in enumerate(selected)
        ]
    elif output_format == "raw":
        payload["content"] = selected_text
    else:
        payload["content"] = _number_lines(selected, start_line)
    return payload


def _file_version(target: Path) -> str:
    with target.open("rb") as handle:
        return f"sha256:{hashlib.file_digest(handle, 'sha256').hexdigest()}"


def _number_lines(lines: List[str], start_line: int) -> str:
    return "\n".join(f"{start_line + index}\t{_strip_line_ending(line)}" for index, line in enumerate(lines))


def _strip_line_ending(line: str) -> str:
    return line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line


def _looks_like_numbered_read_line(text: str) -> bool:
    return any(re.match(r"^\s*\d+(?:\t|\u2192)", line) for line in text.splitlines())


def _apply_simple_unified_patch(workspace: CodeWorkspace, file_patches: List[_FilePatch], targets: List[Path]) -> List[str]:
    changed: List[str] = []
    for file_patch, target in zip(file_patches, targets):
        if not target.exists():
            raise ValueError(f"target file does not exist: {file_patch.path}")
        _apply_file_patch_streaming(target, file_patch)
        changed.append(workspace.relative(target))
    if not changed:
        raise ValueError("no file changes found in patch")
    return changed


def _parse_unified_patch(patch: str) -> List[_FilePatch]:
    lines = patch.splitlines()
    file_patches: List[_FilePatch] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue
        old_path = _patch_path(lines[i][4:].split("\t", 1)[0].strip())
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise ValueError("invalid unified diff header")
        new_path = _patch_path(lines[i][4:].split("\t", 1)[0].strip())
        if old_path != new_path:
            raise ValueError("renames are not supported")
        hunks: List[_PatchHunk] = []
        i += 1
        while i < len(lines) and lines[i].startswith("@@"):
            hunk_header = lines[i]
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", hunk_header)
            if not match:
                raise ValueError("invalid hunk header")
            old_index = int(match.group(1)) - 1
            i += 1
            remove: List[str] = []
            add: List[str] = []
            while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("--- "):
                if lines[i] == r"\ No newline at end of file":
                    i += 1
                    continue
                marker = lines[i][:1]
                value = lines[i][1:]
                if marker == " ":
                    remove.append(value)
                    add.append(value)
                elif marker == "-":
                    remove.append(value)
                elif marker == "+":
                    add.append(value)
                else:
                    raise ValueError("invalid hunk line")
                i += 1
            hunks.append(_PatchHunk(old_index=old_index, remove=remove, add=add, header=hunk_header))
        file_patches.append(_FilePatch(path=new_path, hunks=hunks))
    if not file_patches:
        raise ValueError("no file changes found in patch")
    return file_patches


def _apply_file_patch_streaming(target: Path, file_patch: _FilePatch) -> None:
    resolved_hunks = _resolve_patch_hunk_offsets(target, file_patch)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=str(target.parent),
            delete=False,
        ) as output:
            temp_name = output.name
            _rewrite_patch_stream(target, output, resolved_hunks, file_patch.path)
        os.replace(temp_name, target)
    except Exception:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _resolve_patch_hunk_offsets(target: Path, file_patch: _FilePatch) -> List[_PatchHunk]:
    resolved: List[_PatchHunk] = []
    for hunk in file_patch.hunks:
        old_index = hunk.old_index
        if hunk.remove and not _stream_lines_match_at(target, old_index, hunk.remove):
            old_index = _find_unique_hunk_offset_streaming(target, hunk.remove, file_patch.path, hunk.header)
        resolved.append(_PatchHunk(old_index=old_index, remove=hunk.remove, add=hunk.add, header=hunk.header))

    resolved.sort(key=lambda item: item.old_index)
    previous_end = 0
    for hunk in resolved:
        if hunk.old_index < previous_end:
            raise ValueError(f"overlapping hunks are not supported: {file_patch.path}; {hunk.header}")
        previous_end = hunk.old_index + len(hunk.remove)
    return resolved


def _stream_lines_match_at(target: Path, old_index: int, expected: List[str]) -> bool:
    if not expected:
        return True
    with target.open("r", encoding="utf-8") as handle:
        for current_index, raw_line in enumerate(handle):
            if current_index < old_index:
                continue
            expected_index = current_index - old_index
            if expected_index >= len(expected):
                return True
            if _strip_line_ending(raw_line) != expected[expected_index]:
                return False
        return False


def _find_unique_hunk_offset_streaming(target: Path, expected: List[str], path: str, hunk_header: str) -> int:
    if not expected:
        raise ValueError(
            f"hunk does not match target file: {path}; {hunk_header}; empty context cannot be relocated"
        )

    matches: List[int] = []
    window: List[str] = []
    with target.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle):
            window.append(_strip_line_ending(raw_line))
            if len(window) < len(expected):
                continue
            if len(window) > len(expected):
                window.pop(0)
            if window == expected:
                matches.append(line_number - len(expected) + 1)
                if len(matches) > 5:
                    break
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        lines = ", ".join(str(index + 1) for index in matches[:5])
        suffix = "..." if len(matches) > 5 else ""
        raise ValueError(
            f"hunk does not match target file: {path}; {hunk_header}; "
            f"multiple matching locations found at lines {lines}{suffix}"
        )
    raise ValueError(
        f"hunk does not match target file: {path}; {hunk_header}; no matching context found"
    )


def _rewrite_patch_stream(target: Path, output, hunks: List[_PatchHunk], path: str) -> None:
    hunk_index = 0
    current_index = 0
    with target.open("r", encoding="utf-8") as source:
        while True:
            next_hunk = hunks[hunk_index] if hunk_index < len(hunks) else None
            if next_hunk is not None and current_index == next_hunk.old_index:
                _consume_expected_lines(source, next_hunk.remove, path, next_hunk.header)
                for line in next_hunk.add:
                    output.write(line + "\n")
                current_index += len(next_hunk.remove)
                hunk_index += 1
                continue

            raw_line = source.readline()
            if raw_line == "":
                break
            output.write(_strip_line_ending(raw_line) + "\n")
            current_index += 1

        while hunk_index < len(hunks):
            hunk = hunks[hunk_index]
            if hunk.old_index > current_index or hunk.remove:
                raise ValueError(f"hunk does not match target file: {path}; {hunk.header}")
            for line in hunk.add:
                output.write(line + "\n")
            hunk_index += 1


def _consume_expected_lines(source, expected: List[str], path: str, hunk_header: str) -> None:
    for expected_line in expected:
        raw_line = source.readline()
        if raw_line == "" or _strip_line_ending(raw_line) != expected_line:
            raise ValueError(f"hunk does not match target file: {path}; {hunk_header}")
