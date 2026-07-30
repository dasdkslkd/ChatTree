from __future__ import annotations

import os
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
    return any(
        common._matches_glob(
            path,
            pattern,
            workspace=workspace,
            root=root,
            recursive_basename=True,
        )
        for pattern in exclude_globs
    )


def _iter_grep_files(root: Path, glob: str, *, workspace: Optional[CodeWorkspace] = None) -> Iterable[Path]:
    glob = common._normalize_glob_pattern(glob)
    if root.is_file():
        if common._matches_glob(
            root,
            glob,
            workspace=workspace,
            root=root,
            recursive_basename=True,
        ):
            yield root
        return
    search_glob = common._glob_for_search_root(workspace, root, glob) if workspace is not None else glob
    if common._is_match_all_glob(search_glob):
        yield from root.rglob("*")
    elif common._glob_has_path_separator(search_glob):
        yield from root.glob(search_glob)
    else:
        yield from root.rglob(search_glob)


def _iter_glob_candidates(
    root: Path,
    patterns: List[str],
    workspace: CodeWorkspace,
    *,
    exclude_globs: List[str],
    include_hidden: bool,
    respect_gitignore: bool,
    ignore_matcher: "common._GitIgnoreMatcher",
) -> Iterable[Path]:
    patterns = [common._glob_for_search_root(workspace, root, pattern) for pattern in patterns]
    exclude_globs = [
        common._glob_for_search_root(workspace, root, pattern)
        for pattern in exclude_globs
    ]
    max_depth = (
        None
        if any("**" in pattern for pattern in patterns)
        else max(len(pattern.split("/")) for pattern in patterns)
    )
    for current, dirnames, filenames in os.walk(root, topdown=True):
        current_path = Path(current)
        kept_dirs: List[str] = []
        for names, is_dir in ((dirnames, True), (filenames, False)):
            for name in names:
                candidate = current_path / name
                try:
                    resolved = candidate.resolve()
                    depth = len(resolved.relative_to(root).parts)
                    if not workspace.is_visible(resolved):
                        continue
                    if common._should_skip_python_path(
                        resolved,
                        root,
                        hidden=include_hidden,
                        no_ignore=respect_gitignore is False,
                        ignore_matcher=ignore_matcher,
                    ):
                        continue
                    if any(
                        common._matches_glob(
                            resolved,
                            pattern,
                            root=root,
                        )
                        for pattern in exclude_globs
                    ):
                        continue
                except (OSError, ValueError):
                    continue
                if is_dir and (
                    max_depth is None
                    or depth < max_depth
                ):
                    kept_dirs.append(name)
                if any(
                    common._matches_glob(
                        resolved,
                        pattern,
                        root=root,
                    )
                    for pattern in patterns
                ):
                    yield resolved
        dirnames[:] = kept_dirs


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
        search_root = root.parent
        candidates = (
            [root]
            if (
                not common._should_skip_python_path(
                    root,
                    search_root,
                    hidden=include_hidden,
                    no_ignore=respect_gitignore is False,
                    ignore_matcher=ignore_matcher,
                )
                and not any(
                    common._matches_glob(
                        root,
                        pattern,
                        workspace=workspace,
                        root=root,
                    )
                    for pattern in exclude_globs
                )
                and any(
                    common._matches_glob(
                        root,
                        pattern,
                        workspace=workspace,
                        root=root,
                    )
                    for pattern in patterns
                )
            )
            else []
        )
    else:
        candidates = _iter_glob_candidates(
            root,
            patterns,
            workspace,
            exclude_globs=exclude_globs,
            include_hidden=include_hidden,
            respect_gitignore=respect_gitignore,
            ignore_matcher=ignore_matcher,
        )

    for resolved in candidates:
        scanned_entries += 1
        if resolved in seen:
            continue
        seen.add(resolved)
        if files_only and not resolved.is_file():
            continue
        if not files_only and not (resolved.is_file() or resolved.is_dir()):
            continue
        relative = workspace.relative(resolved)
        if path_regex and not path_regex.search(relative):
            continue
        if early_page:
            observed_count += 1
            if observed_count <= offset:
                continue
            if len(matches) < limit:
                matches.append(resolved)
                continue
            stopped_for_page = True
            break
        matches.append(resolved)
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
