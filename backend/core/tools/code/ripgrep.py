from __future__ import annotations

import base64
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import common, python_fallback
from .common import CodeToolConfig, CodeWorkspace
from ...subprocess_utils import subprocess_window_kwargs


def _resolve_ripgrep_executable(config: CodeToolConfig) -> Optional[Path]:
    executable = "rg.exe" if os.name == "nt" else "rg"
    platform_dir = _ripgrep_platform_dir()
    candidates = [
        config.ripgrep_install_dir / config.ripgrep_version / platform_dir / executable,
        config.ripgrep_install_dir / config.ripgrep_version / platform_dir / "rg",
        config.ripgrep_install_dir / platform_dir / executable,
        config.ripgrep_install_dir / platform_dir / "rg",
        config.ripgrep_install_dir / executable,
        config.ripgrep_install_dir / "rg",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if os.name != "nt" and not os.access(candidate, os.X_OK):
            continue
        return candidate
    path_executable = shutil.which(executable) or shutil.which("rg")
    if path_executable:
        return Path(path_executable)
    return None


def _ripgrep_platform_dir() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    if system == "windows":
        return f"win32-{arch}"
    if system == "darwin":
        return f"darwin-{arch}"
    if system == "linux":
        return f"linux-{arch}"
    return f"{system or 'unknown'}-{arch}"


def _rg_json_text(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    if isinstance(text, str):
        return text
    raw_bytes = value.get("bytes")
    if isinstance(raw_bytes, str):
        try:
            return base64.b64decode(raw_bytes).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def _ripgrep_failure_reason(stderr: str) -> str:
    stderr_text = stderr or ""
    if "regex parse error" in stderr_text.lower():
        message = " ".join(line.strip() for line in stderr_text.splitlines() if line.strip())
        return f"ripgrep_invalid_regex:{message[:200]}"
    message = (stderr or "").strip().splitlines()
    if message:
        first_line = message[0][:120].replace("\n", " ")
        return f"ripgrep_failed:{first_line}"
    return "ripgrep_failed"


def _grep_with_rg(
    *,
    rg_path: Path,
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
    count_mode: bool,
    multiline: bool,
    exclude_globs: List[str],
    timeout_seconds: int,
    event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if root.is_file() and not common._matches_glob(root, glob, workspace=workspace):
        return ({
            "pattern": pattern,
            "matches": [],
            "searched_files": 0,
            "skipped_non_utf8": [],
            "truncated": False,
            "engine": "rg",
        }, None)

    single_file = root if root.is_file() else None
    single_file_relative = workspace.relative(root) if single_file is not None else None
    cwd = root if root.is_dir() else root.parent
    target = "." if root.is_dir() else root.name
    argv = [str(rg_path), "--color", "never", "--no-config", "--line-number"]
    if not files_with_matches:
        argv.append("--json")
    if multiline:
        argv.append("--multiline")
    if fixed_strings:
        argv.append("--fixed-strings")
    if ignore_case:
        argv.append("--ignore-case")
    if no_ignore:
        argv.append("--no-ignore")
    if hidden:
        argv.append("--hidden")
    if before_context:
        argv.extend(["--before-context", str(before_context)])
    if after_context:
        argv.extend(["--after-context", str(after_context)])
    if files_with_matches:
        argv.append("--files-with-matches")
    rg_glob = common._glob_for_search_root(workspace, root, glob)
    if rg_glob and not common._is_match_all_glob(rg_glob):
        argv.extend(["--glob", rg_glob])
    for exclude_glob in exclude_globs:
        argv.extend(["--glob", f"!{common._glob_for_search_root(workspace, root, exclude_glob)}"])
    argv.extend(["--", pattern, target])

    matches: List[Dict[str, Any]] = []
    match_index_by_line: dict[tuple[str, int], int] = {}
    counts: dict[str, int] = defaultdict(int)
    matched_files: set[str] = set()
    skipped_files: set[str] = set()
    searched_paths: set[str] = set()
    scanned_lines = 0
    truncated = False
    last_progress_at = 0.0

    def emit_progress(*, phase: str = "scan", force: bool = False) -> None:
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
                "phase": phase,
                "engine": "rg",
                "root": workspace.relative(root),
                "searched_files": len(searched_paths),
                "matched_files": len(matched_files),
                "matches": sum(counts.values()) if count_mode else len(matches),
                "scanned_entries": scanned_lines,
                "truncated": truncated,
            },
        )

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=common._shell_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **subprocess_window_kwargs(),
        )
    except FileNotFoundError:
        return None, "ripgrep_not_installed"
    except OSError as exc:
        return None, f"ripgrep_failed:{type(exc).__name__}"

    line_queue: queue.Queue[object] = queue.Queue(maxsize=256)
    stdout_done = object()
    stop_reader = threading.Event()
    stderr_lines: list[str] = []

    def pump_stdout() -> None:
        try:
            if proc.stdout is not None:
                try:
                    for line in proc.stdout:
                        if stop_reader.is_set():
                            break
                        while not stop_reader.is_set():
                            try:
                                line_queue.put(line, timeout=0.05)
                                break
                            except queue.Full:
                                continue
                except (OSError, ValueError):
                    pass
        finally:
            while not stop_reader.is_set():
                try:
                    line_queue.put(stdout_done, timeout=0.05)
                    break
                except queue.Full:
                    continue

    def pump_stderr() -> None:
        try:
            if proc.stderr is not None:
                for line in proc.stderr:
                    stderr_lines.append(line)
        except (OSError, ValueError):
            pass

    stdout_thread = threading.Thread(target=pump_stdout, name="grep-rg-stdout", daemon=True)
    stderr_thread = threading.Thread(target=pump_stderr, name="grep-rg-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    timed_out = False
    invalid_json = False
    return_code: int | None = None
    emit_progress(phase="scan_start", force=True)

    def stop_process() -> None:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        stdout_thread.join(timeout=0.2)
        stderr_thread.join(timeout=0.2)

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                queued = line_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if queued is stdout_done:
                break
            raw_line = str(queued).rstrip("\r\n")
            if not raw_line:
                continue
            scanned_lines += 1
            emit_progress()
            if files_with_matches:
                if single_file_relative is not None:
                    relative_path = single_file_relative
                else:
                    resolved = (cwd / raw_line.strip()).resolve()
                    if not resolved.is_file() or not workspace.is_visible(resolved):
                        continue
                    relative_path = workspace.relative(resolved)
                searched_paths.add(relative_path)
                matched_files.add(relative_path)
                if len(matched_files) >= max_results:
                    truncated = True
                    break
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                invalid_json = True
                break
            if event.get("type") not in {"match", "context"}:
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            path_text = _rg_json_text(data.get("path"))
            if not path_text:
                continue
            if single_file_relative is not None:
                relative_path = single_file_relative
            else:
                resolved = (cwd / path_text).resolve()
                if not resolved.is_file() or not workspace.is_visible(resolved):
                    continue
                relative_path = workspace.relative(resolved)
            searched_paths.add(relative_path)
            if event.get("type") == "match":
                counts[relative_path] += 1
                matched_files.add(relative_path)
            line_text = _rg_json_text(data.get("lines"))
            if line_text is None:
                skipped_files.add(relative_path)
                continue
            if count_mode:
                continue
            line_number = int(data.get("line_number") or 0)
            key = (relative_path, line_number)
            existing_index = match_index_by_line.get(key)
            entry = {
                "path": relative_path,
                "line": line_number,
                "preview": line_text.strip(),
                "type": event.get("type"),
            }
            if existing_index is not None:
                if entry["type"] == "match" and matches[existing_index].get("type") != "match":
                    matches[existing_index] = entry
                continue
            match_index_by_line[key] = len(matches)
            matches.append(entry)
            if len(matches) >= max_results:
                truncated = True
                break
    finally:
        if timed_out or truncated or invalid_json:
            stop_reader.set()
            stop_process()

    if timed_out:
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "timeout",
                "engine": "rg",
                "searched_files": len(searched_paths),
                "matches": sum(counts.values()) if count_mode else len(matches),
            },
        )
        return None, "ripgrep_timeout"
    if invalid_json:
        return None, "ripgrep_invalid_json"
    if not truncated:
        remaining = max(0.001, deadline - time.monotonic())
        try:
            return_code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            stop_reader.set()
            stop_process()
            return None, "ripgrep_timeout"
        stdout_thread.join(timeout=0.2)
        stderr_thread.join(timeout=0.2)
        if return_code not in {0, 1}:
            return None, _ripgrep_failure_reason("".join(stderr_lines))

    payload: Dict[str, Any] = {
        "pattern": pattern,
        "matches": matches,
        "searched_files": len(searched_paths),
        "skipped_non_utf8": sorted(skipped_files),
        "truncated": truncated,
        "engine": "rg",
    }
    if files_with_matches:
        payload["files"] = sorted(matched_files)
        payload["matches"] = []
    if count_mode:
        payload["counts"] = [
            {"path": path, "count": count}
            for path, count in sorted(counts.items())
        ]
        payload["matches"] = []
    emit_progress(phase="complete", force=True)
    return (payload, None)


def _glob_files_with_rg(
    *,
    rg_path: Path,
    workspace: CodeWorkspace,
    root: Path,
    patterns: List[str],
    path_regex: Optional[re.Pattern[str]],
    files_only: bool,
    respect_gitignore: bool,
    include_hidden: bool,
    exclude_globs: List[str],
    sort: str,
    limit: int,
    offset: int,
    timeout_seconds: int,
    event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if root.is_file():
        if not any(common._matches_glob(root, pattern, workspace=workspace) for pattern in patterns):
            return (common._glob_payload(
                workspace=workspace,
                root=root,
                page=[],
                engine="rg",
                sort=sort,
                scanned_entries=1,
                observed_count=0,
                total_known=True,
                truncated=False,
                offset=offset,
            ), None)
        relative = workspace.relative(root)
        if path_regex and not path_regex.search(relative):
            files: List[str] = []
        else:
            files = [relative]
        page = files[offset:offset + limit]
        return (common._glob_payload(
            workspace=workspace,
            root=root,
            page=page,
            engine="rg",
            sort=sort,
            scanned_entries=1,
            observed_count=len(files),
            total_known=True,
            truncated=offset + limit < len(files),
            offset=offset,
        ), None)

    argv = [str(rg_path), "--files", "--color", "never", "--no-config"]
    if sort == "path" and files_only:
        argv.extend(["--sort", "path"])
    if not respect_gitignore:
        argv.append("--no-ignore")
    if include_hidden:
        argv.append("--hidden")
    rg_patterns = [common._glob_for_search_root(workspace, root, pattern) for pattern in patterns]
    for pattern in rg_patterns:
        if pattern and not common._is_match_all_glob(pattern):
            argv.extend(["--glob", pattern])
    for exclude_glob in exclude_globs:
        argv.extend(["--glob", f"!{common._glob_for_search_root(workspace, root, exclude_glob)}"])
    argv.extend(["--", "."])

    matches: List[Path] = []
    seen: set[Path] = set()
    scanned_entries = 0
    stopped_early = False
    stopped_for_page = False
    timed_out = False
    observed_count = 0
    early_page = files_only and sort in {"discovery", "path"}
    last_progress_at = 0.0

    def emit_progress(*, phase: str = "scan", force: bool = False) -> None:
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
                "phase": phase,
                "engine": "rg",
                "root": workspace.relative(root),
                "scanned_entries": scanned_entries,
                "matched_entries": observed_count if early_page else len(matches),
                "truncated": stopped_early,
            },
        )

    def add_candidate(candidate: Path) -> bool:
        nonlocal observed_count, stopped_early, stopped_for_page
        resolved_candidate = candidate.resolve()
        if resolved_candidate in seen:
            return False
        seen.add(resolved_candidate)
        accepted = python_fallback._accept_glob_candidate(
            resolved_candidate,
            workspace=workspace,
            root=root,
            patterns=patterns,
            path_regex=path_regex,
            files_only=files_only,
            include_hidden=include_hidden,
            exclude_globs=exclude_globs,
        )
        if accepted is None:
            return False
        if early_page:
            observed_count += 1
            if observed_count <= offset:
                return False
            if len(matches) < limit:
                matches.append(accepted)
                return False
            stopped_early = True
            stopped_for_page = True
            return True
        matches.append(accepted)
        observed_count = len(matches)
        return False

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(root),
            env=common._shell_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **subprocess_window_kwargs(),
        )
    except FileNotFoundError:
        return None, "ripgrep_not_installed"
    except OSError as exc:
        return None, f"ripgrep_failed:{type(exc).__name__}"

    line_queue: queue.Queue[object] = queue.Queue(maxsize=256)
    stdout_done = object()
    stop_reader = threading.Event()
    stderr_lines: list[str] = []

    def pump_stdout() -> None:
        try:
            if proc.stdout is not None:
                try:
                    for line in proc.stdout:
                        if stop_reader.is_set():
                            break
                        while not stop_reader.is_set():
                            try:
                                line_queue.put(line, timeout=0.05)
                                break
                            except queue.Full:
                                continue
                except (OSError, ValueError):
                    pass
        finally:
            while not stop_reader.is_set():
                try:
                    line_queue.put(stdout_done, timeout=0.05)
                    break
                except queue.Full:
                    continue

    def pump_stderr() -> None:
        try:
            if proc.stderr is not None:
                for line in proc.stderr:
                    stderr_lines.append(line)
        except (OSError, ValueError):
            pass

    stdout_thread = threading.Thread(target=pump_stdout, name="glob-rg-stdout", daemon=True)
    stderr_thread = threading.Thread(target=pump_stderr, name="glob-rg-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    return_code: int | None = None
    emit_progress(phase="scan_start", force=True)

    def stop_process() -> None:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        stdout_thread.join(timeout=0.2)
        stderr_thread.join(timeout=0.2)

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                queued = line_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if queued is stdout_done:
                break
            raw_line = str(queued)
            relative_text = raw_line.strip()
            if not relative_text:
                continue
            scanned_entries += 1
            emit_progress()
            resolved = (root / relative_text).resolve()
            if not resolved.is_file() or not workspace.is_visible(resolved):
                continue
            if not include_hidden and common._is_hidden_under(resolved, root):
                continue
            if files_only:
                if add_candidate(resolved):
                    break
                continue
            ancestors: List[Path] = []
            parent = resolved.parent
            while parent != root and root in parent.parents:
                ancestors.append(parent)
                parent = parent.parent
            page_full = False
            for candidate in reversed(ancestors):
                if add_candidate(candidate):
                    page_full = True
                    break
            if page_full:
                break
            else:
                add_candidate(resolved)
    finally:
        if timed_out or stopped_early:
            stop_reader.set()
            stop_process()

    if timed_out:
        common._emit_tool_observation(
            event_sink,
            "tool_progress",
            status="running",
            progress={
                "phase": "timeout",
                "engine": "rg",
                "scanned_entries": scanned_entries,
                "matched_entries": observed_count if early_page else len(matches),
            },
        )
        return None, "ripgrep_timeout"

    if not stopped_early:
        remaining = max(0.001, deadline - time.monotonic())
        try:
            return_code = proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            stop_reader.set()
            stop_process()
            return None, "ripgrep_timeout"
        stdout_thread.join(timeout=0.2)
        stderr_thread.join(timeout=0.2)
        if return_code not in {0, 1}:
            return None, _ripgrep_failure_reason("".join(stderr_lines))

    if not files_only and root.is_dir() and not stopped_early:
        ignore_matcher = common._GitIgnoreMatcher.for_root(root, workspace)
        for candidate in root.rglob("*"):
            scanned_entries += 1
            resolved = candidate.resolve()
            if not resolved.is_dir():
                continue
            if common._should_skip_python_path(
                resolved,
                root,
                hidden=include_hidden,
                no_ignore=respect_gitignore is False,
                ignore_matcher=ignore_matcher,
            ):
                continue
            add_candidate(resolved)
            emit_progress()

    if sort == "mtime":
        matches.sort(key=lambda path: (-path.stat().st_mtime, workspace.relative(path)))
    elif sort == "path" and not (files_only and early_page):
        matches.sort(key=lambda path: workspace.relative(path))

    total_known = not stopped_for_page and not stopped_early
    if total_known:
        observed_count = len(matches) if not early_page else observed_count
    if early_page:
        page_paths = matches
    else:
        page_paths = matches[offset:offset + limit]
    truncated = stopped_for_page or (total_known and offset + limit < observed_count)
    emit_progress(phase="complete", force=True)
    return (common._glob_payload(
        workspace=workspace,
        root=root,
        page=[workspace.relative(path) for path in page_paths],
        engine="rg",
        sort=sort,
        scanned_entries=scanned_entries,
        observed_count=observed_count,
        total_known=total_known,
        truncated=truncated,
        offset=offset,
    ), None)
