from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import common
from .common import CodeWorkspace


def _matches_excluded_glob(
    path: Path,
    root: Path,
    exclude_globs: List[str],
    *,
    workspace: Optional[CodeWorkspace] = None,
) -> bool:
    if not exclude_globs:
        return False
    return any(common._matches_glob(path, pattern, workspace=workspace, root=root) for pattern in exclude_globs)


def _iter_grep_files(root: Path, glob: str, *, workspace: Optional[CodeWorkspace] = None) -> Iterable[Path]:
    glob = common._normalize_glob_pattern(glob)
    if root.is_file():
        if common._matches_glob(root, glob, workspace=workspace):
            yield root
        return
    search_glob = common._glob_for_search_root(workspace, root, glob) if workspace is not None else glob
    if common._is_match_all_glob(search_glob):
        yield from root.rglob("*")
    elif common._glob_has_path_separator(search_glob):
        yield from root.glob(search_glob)
    else:
        yield from root.rglob(search_glob)


def _iter_glob_candidates(root: Path, patterns: List[str], workspace: CodeWorkspace) -> Iterable[Path]:
    seen: set[Path] = set()
    for pattern in patterns:
        search_pattern = common._glob_for_search_root(workspace, root, pattern)
        if common._is_match_all_glob(search_pattern):
            candidates = root.rglob("*")
        elif common._glob_has_path_separator(search_pattern):
            candidates = root.glob(search_pattern)
        else:
            candidates = root.rglob(search_pattern)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield candidate


def _compile_python_matcher(pattern: str, *, fixed_strings: bool, ignore_case: bool):
    if not fixed_strings:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(pattern, flags)
        return lambda line: compiled.search(line) is not None
    if ignore_case:
        needle = pattern.lower()
        return lambda line: needle in line.lower()
    return lambda line: pattern in line


def _search_payload(
    pattern: str,
    matches: List[Dict[str, Any]],
    searched_files: int,
    skipped_files: List[str],
    truncated: bool,
    engine: str,
    files: Optional[set[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "pattern": pattern,
        "matches": [] if files is not None else matches,
        "searched_files": searched_files,
        "skipped_non_utf8": skipped_files,
        "truncated": truncated,
        "engine": engine,
    }
    if files is not None:
        payload["files"] = sorted(files)
    return payload


def _accept_glob_candidate(
    candidate: Path,
    *,
    workspace: CodeWorkspace,
    root: Path,
    patterns: List[str],
    path_regex: Optional[re.Pattern[str]],
    files_only: bool,
    include_hidden: bool,
    exclude_globs: List[str],
    ignore_matcher: Optional["common._GitIgnoreMatcher"] = None,
    respect_gitignore: bool = True,
) -> Optional[Path]:
    resolved = candidate.resolve()
    if files_only and not resolved.is_file():
        return None
    if not files_only and not (resolved.is_file() or resolved.is_dir()):
        return None
    if not workspace.is_visible(resolved):
        return None
    search_root = root if root.is_dir() else root.parent
    if common._should_skip_python_path(
        resolved,
        search_root,
        hidden=include_hidden,
        no_ignore=respect_gitignore is False,
        ignore_matcher=ignore_matcher,
    ):
        return None
    if _matches_excluded_glob(resolved, search_root, exclude_globs, workspace=workspace):
        return None
    if not any(common._matches_glob(resolved, pattern, workspace=workspace, root=root) for pattern in patterns):
        return None
    relative = workspace.relative(resolved)
    if path_regex and not path_regex.search(relative):
        return None
    return resolved


def _grep_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    pattern: str,
    glob: str,
    max_results: int,
    fixed_strings: bool,
    ignore_case: bool,
    no_ignore: bool,
    hidden: bool,
    before_context: int,
    after_context: int,
    files_with_matches: bool,
    exclude_globs: List[str],
    event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    matches: List[Dict[str, Any]] = []
    matched_files: set[str] = set()
    searched_files = 0
    skipped_files: List[str] = []
    matcher = _compile_python_matcher(pattern, fixed_strings=fixed_strings, ignore_case=ignore_case)
    ignore_matcher = common._GitIgnoreMatcher.for_root(root, workspace)
    last_progress_at = 0.0

    def emit_progress(*, force: bool = False) -> None:
        nonlocal last_progress_at
        now = time.monotonic()
        if not force and now - last_progress_at < 0.5:
            return
        last_progress_at = now
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "scan",
                "engine": "python",
                "root": workspace.relative(root),
                "searched_files": searched_files,
                "matched_files": len(matched_files),
                "matches": len(matches),
            },
        )

    emit_progress(force=True)
    for file_path in _iter_grep_files(root, glob, workspace=workspace):
        resolved = file_path.resolve()
        if (
            not resolved.is_file()
            or not workspace.is_visible(resolved)
            or common._should_skip_python_path(resolved, root, hidden=hidden, no_ignore=no_ignore, ignore_matcher=ignore_matcher)
            or _matches_excluded_glob(resolved, root, exclude_globs, workspace=workspace)
        ):
            continue
        searched_files += 1
        emit_progress()
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_files.append(workspace.relative(resolved))
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not matcher(line):
                continue
            relative_path = workspace.relative(resolved)
            matched_files.add(relative_path)
            if files_with_matches:
                if len(matched_files) >= max_results:
                    emit_progress(force=True)
                    return _search_payload(pattern, [], searched_files, skipped_files, True, "python", matched_files)
                break
            start = max(0, index - before_context)
            stop = min(len(lines), index + after_context + 1)
            for context_index in range(start, stop):
                matches.append({
                    "path": relative_path,
                    "line": context_index + 1,
                    "preview": lines[context_index].strip(),
                    "type": "match" if context_index == index else "context",
                })
                if len(matches) >= max_results:
                    emit_progress(force=True)
                    return _search_payload(pattern, matches, searched_files, skipped_files, True, "python", matched_files)
            if len(matches) >= max_results:
                emit_progress(force=True)
                return {
                    "pattern": pattern,
                    "matches": matches,
                    "searched_files": searched_files,
                    "skipped_non_utf8": skipped_files,
                    "truncated": True,
                    "engine": "python",
                }

    emit_progress(force=True)
    return _search_payload(pattern, matches, searched_files, skipped_files, False, "python", matched_files if files_with_matches else None)


def _glob_files_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    patterns: List[str],
    path_regex: Optional[re.Pattern[str]],
    respect_gitignore: bool,
    include_hidden: bool,
    files_only: bool,
    exclude_globs: List[str],
    sort: str,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    ignore_matcher = common._GitIgnoreMatcher.for_root(root, workspace)
    matches: List[Path] = []
    seen: set[Path] = set()
    scanned_entries = 0
    observed_count = 0
    stopped_for_page = False
    early_page = files_only and sort == "discovery"

    candidates: Iterable[Path]
    if root.is_file():
        candidates = [root]
    else:
        candidates = _iter_glob_candidates(root, patterns, workspace)

    for candidate in candidates:
        scanned_entries += 1
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        accepted = _accept_glob_candidate(
            resolved,
            workspace=workspace,
            root=root,
            patterns=patterns,
            path_regex=path_regex,
            files_only=files_only,
            include_hidden=include_hidden,
            exclude_globs=exclude_globs,
            ignore_matcher=ignore_matcher,
            respect_gitignore=respect_gitignore,
        )
        if accepted is None:
            continue
        if early_page:
            observed_count += 1
            if observed_count <= offset:
                continue
            if len(matches) < limit:
                matches.append(accepted)
                continue
            stopped_for_page = True
            break
        matches.append(accepted)
        observed_count = len(matches)

    if sort == "mtime":
        matches.sort(key=lambda path: (-path.stat().st_mtime, workspace.relative(path)))
    elif sort == "path":
        matches.sort(key=lambda path: workspace.relative(path))

    total_known = not stopped_for_page
    if total_known:
        observed_count = observed_count if early_page else len(matches)
    page = matches if early_page else matches[offset:offset + limit]
    truncated = stopped_for_page or (total_known and offset + limit < observed_count)
    return common._glob_payload(
        workspace=workspace,
        root=root,
        page=[workspace.relative(path) for path in page],
        engine="python",
        sort=sort,
        scanned_entries=scanned_entries,
        observed_count=observed_count,
        total_known=total_known,
        truncated=truncated,
        offset=offset,
    )


def _grep_files_python(
    *,
    workspace: CodeWorkspace,
    root: Path,
    pattern: str,
    glob: str,
    limit: int,
    offset: int,
    fixed_strings: bool,
    ignore_case: bool,
    multiline: bool,
    no_ignore: bool,
    hidden: bool,
    before_context: int,
    after_context: int,
    output: str,
    exclude_globs: List[str],
    event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    if not fixed_strings:
        try:
            flags = re.IGNORECASE | (re.DOTALL if multiline else 0)
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return {"error": {"type": "invalid_query", "message": f"invalid regex: {exc}"}}
    else:
        compiled = None
    ignore_matcher = common._GitIgnoreMatcher.for_root(root, workspace)
    matches: List[Dict[str, Any]] = []
    files: List[str] = []
    counts: List[Dict[str, Any]] = []
    skipped_files: List[str] = []
    searched_files = 0
    last_progress_at = 0.0

    def emit_progress(*, force: bool = False) -> None:
        nonlocal last_progress_at
        now = time.monotonic()
        if not force and now - last_progress_at < 0.5:
            return
        last_progress_at = now
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "scan",
                "engine": "python",
                "root": workspace.relative(root),
                "searched_files": searched_files,
                "matched_files": len(files) or len(counts),
                "matches": len(matches),
            },
        )

    emit_progress(force=True)
    for file_path in _iter_grep_files(root, glob, workspace=workspace):
        resolved = file_path.resolve()
        if (
            not resolved.is_file()
            or not workspace.is_visible(resolved)
            or common._should_skip_python_path(resolved, root, hidden=hidden, no_ignore=no_ignore, ignore_matcher=ignore_matcher)
            or _matches_excluded_glob(resolved, root, exclude_globs, workspace=workspace)
        ):
            continue
        relative = workspace.relative(resolved)
        searched_files += 1
        emit_progress()
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_files.append(relative)
            continue
        file_match_count = 0
        if multiline:
            found = list(compiled.finditer(text)) if compiled else []
            file_match_count = len(found)
            if found and output == "files":
                files.append(relative)
            elif output == "content":
                lines = text.splitlines()
                for match in found:
                    line_no = text.count("\n", 0, match.start()) + 1
                    line_text = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""
                    matches.append({"path": relative, "line": line_no, "text": line_text, "type": "match"})
        else:
            lines = text.splitlines()
            for index, line in enumerate(lines):
                ok = (compiled.search(line) is not None) if compiled else (
                    pattern.lower() in line.lower() if ignore_case else pattern in line
                )
                if not ok:
                    continue
                file_match_count += 1
                if output == "content":
                    start = max(0, index - before_context)
                    stop = min(len(lines), index + after_context + 1)
                    for context_index in range(start, stop):
                        matches.append({
                            "path": relative,
                            "line": context_index + 1,
                            "text": lines[context_index],
                            "type": "match" if context_index == index else "context",
                        })
            if file_match_count and output == "files":
                files.append(relative)
        if output == "count" and file_match_count:
            counts.append({"path": relative, "count": file_match_count})

    emit_progress(force=True)
    if output == "files":
        page = files[offset:offset + limit]
        return {"pattern": pattern, "output": output, "files": page, "count": len(page), "searched_files": searched_files, "skipped_non_utf8": skipped_files, "truncated": offset + limit < len(files), "next_offset": offset + len(page) if offset + limit < len(files) else None, "engine": "python"}
    if output == "count":
        page = counts[offset:offset + limit]
        return {"pattern": pattern, "output": output, "counts": page, "count": len(page), "searched_files": searched_files, "skipped_non_utf8": skipped_files, "truncated": offset + limit < len(counts), "next_offset": offset + len(page) if offset + limit < len(counts) else None, "engine": "python"}
    page = matches[offset:offset + limit]
    return {"pattern": pattern, "output": output, "matches": page, "count": len(page), "searched_files": searched_files, "skipped_non_utf8": skipped_files, "truncated": offset + limit < len(matches), "next_offset": offset + len(page) if offset + limit < len(matches) else None, "engine": "python"}
