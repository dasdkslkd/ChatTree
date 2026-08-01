import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core import subprocess_utils
from backend.core.tools import code as code_tools
from backend.core.tools.base import BaseTool
from backend.core.tools.code import common as code_common
from backend.core.tools.code import python_fallback as code_python_fallback
from backend.core.tools.code import ripgrep as code_ripgrep
from backend.core.tools.code import (
    ApplyPatchTool,
    CodeToolConfig,
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchFilesTool,
    WriteFileTool,
)
from backend.core.tools.tool_manager import ToolManager


def run(coro):
    return asyncio.run(coro)


def load(payload: str):
    return json.loads(payload)


def test_default_code_workspace_uses_chattree_home(monkeypatch, tmp_path):
    home = tmp_path / "chattree-home"
    monkeypatch.setenv("CHATTREE_HOME", str(home))

    config = CodeToolConfig.from_dict({})

    assert code_tools.DEFAULT_CODE_WORKSPACE == Path("workspaces") / "default"
    assert config.workspace_roots == [(home / "workspaces" / "default").resolve()]


def test_explicit_code_workspace_roots_override_default(monkeypatch, tmp_path):
    home = tmp_path / "chattree-home"
    project = tmp_path / "project"
    monkeypatch.setenv("CHATTREE_HOME", str(home))

    config = CodeToolConfig.from_dict({"workspace_roots": [str(project)]})

    assert config.workspace_roots == [project.resolve()]


class FakePopen:
    def __init__(self, args, *, stdout_text: str = "", stderr_text: str = "", returncode: int = 0, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, timeout=None):
        return "", self.stderr.read()

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class HangingPopen:
    class BlockingStdout:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            while not self.closed:
                import time
                time.sleep(0.01)
            raise StopIteration

        def close(self):
            self.closed = True

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = self.BlockingStdout()
        self.stderr = io.StringIO("")
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -15
        return self.returncode

    def communicate(self, timeout=None):
        return "", ""

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class LongRunningPopen(FakePopen):
    def __init__(self, args, *, stdout_text: str = "", stderr_text: str = "", **kwargs):
        super().__init__(args, stdout_text=stdout_text, stderr_text=stderr_text, returncode=0, **kwargs)
        self.returncode = None

    def poll(self):
        return self.returncode


def make_config(tmp_path: Path, **overrides) -> CodeToolConfig:
    config = {
        "workspace_roots": [str(tmp_path)],
        "protected_paths": [".git", "data/config.json"],
        "command_timeout_seconds": 2,
        "max_read_chars": 20,
        "max_output_chars": 200,
    }
    config.update(overrides)
    return CodeToolConfig.from_dict(config)


def test_run_command_default_initial_wait_is_120_seconds(tmp_path):
    config = CodeToolConfig.from_dict({
        "workspace_roots": [str(tmp_path)],
        "protected_paths": [".git"],
    })

    assert config.shell_initial_wait_seconds == 120.0


def test_glob_lists_workspace_files_and_hides_protected_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", limit=10)))

    assert set(result["files"]) == {"src/app.py"}
    assert result["truncated"] is False


def test_glob_recursive_listing_is_bounded_by_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "empty").mkdir()
    for index in range(100):
        (tmp_path / "src" / f"file_{index:03}.txt").write_text("x", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", limit=10)))

    assert result["engine"] == "python"
    assert len(result["files"]) == 10
    assert result["truncated"] is True
    assert result["next_offset"] == 10
    assert result["total"] is None
    assert result["total_known"] is False
    assert result["observed_count"] == 11


def test_glob_supports_path_regex_and_excludes(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / ".hidden.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "skip.py").write_text("print('skip')", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.py").write_text("print('deep')", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(
        path=".",
        pattern="*.py",
        path_regex=r"(^|/)\.hidden\.py$",
        files_only=True,
        respect_gitignore=False,
        include_hidden=True,
        exclude=["skip.py"],
        limit=10,
    )))

    assert result["files"] == [".hidden.py"]


def test_glob_uses_ripgrep_by_default_and_documents_parameters(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "src" / "skip.txt").write_text("skip", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(args, stdout_text="src/app.py\nsrc/skip.txt\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(
        path=".",
        pattern="**/*.py",
        path_regex=r"src/",
        respect_gitignore=False,
        include_hidden=True,
        exclude=["**/skip.txt"],
        limit=10,
    )))

    assert result["engine"] == "rg"
    assert result["files"] == ["src/app.py"]
    args, kwargs = calls[0]
    assert args[:4] == [str(fake_rg), "--files", "--color", "never"]
    assert "--no-ignore" in args
    assert "--hidden" in args
    assert "--glob" not in args
    assert kwargs["cwd"] == str(tmp_path)
    assert "do not use a `query` argument" in tool.description
    assert "total_known" in tool.description
    assert "sort=mtime" in tool.description


def test_glob_ripgrep_sort_path_uses_rg_sort_and_pages_early(tmp_path, monkeypatch):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(args, stdout_text="a.py\nb.py\nc.py\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", pattern="*.py", sort="path", limit=2)))

    assert result["engine"] == "rg"
    assert result["sort"] == "path"
    assert result["files"] == ["a.py", "b.py"]
    assert result["truncated"] is True
    assert result["total"] is None
    assert result["total_known"] is False
    assert result["observed_count"] == 3
    assert result["scanned_entries"] == 3
    args, _kwargs = calls[0]
    assert args[args.index("--sort"):args.index("--sort") + 2] == ["--sort", "path"]


def test_glob_mtime_requires_full_scan_and_reports_known_total(tmp_path, monkeypatch):
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    mid = tmp_path / "mid.py"
    for path in (old, new, mid):
        path.write_text("x", encoding="utf-8")
    os.utime(old, (10, 10))
    os.utime(mid, (20, 20))
    os.utime(new, (30, 30))
    fake_rg = tmp_path / "rg.exe"
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(args, stdout_text="old.py\nnew.py\nmid.py\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", pattern="*.py", sort="mtime", limit=2)))

    assert result["engine"] == "rg"
    assert result["sort"] == "mtime"
    assert result["files"] == ["new.py", "mid.py"]
    assert result["truncated"] is True
    assert result["total"] == 3
    assert result["total_known"] is True
    assert result["observed_count"] == 3
    assert "--sort" not in calls[0][0]


def test_glob_ripgrep_matches_workspace_relative_path_patterns(tmp_path, monkeypatch):
    (tmp_path / "backend" / "core" / "tools").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "tools" / "code_tools.py").write_text("x", encoding="utf-8")
    (tmp_path / "backend" / "core" / "tools" / "notes.txt").write_text("x", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"

    def fake_popen(args, **kwargs):
        assert "--glob" not in args
        return FakePopen(
            args,
            stdout_text="backend/core/tools/code_tools.py\nbackend/core/tools/notes.txt\n",
            **kwargs,
        )

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", pattern=r"backend\core\tools\*.py", limit=10)))

    assert result["engine"] == "rg"
    assert result["files"] == ["backend/core/tools/code_tools.py"]


def test_glob_python_fallback_matches_workspace_relative_path_patterns(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "backend" / "core" / "tools").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "tools" / "code_tools.py").write_text("x", encoding="utf-8")
    (tmp_path / "backend" / "core" / "tools" / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "README.md").write_text("root", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    path_pattern = load(run(tool.execute(path=".", pattern=r"backend\core\tools\*.py", limit=10)))
    default_pattern = load(run(tool.execute(path=".", limit=10)))

    assert path_pattern["engine"] == "python"
    assert path_pattern["files"] == ["backend/core/tools/code_tools.py"]
    assert "README.md" in default_pattern["files"]


def test_glob_double_star_matches_root_files_like_ripgrep(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "main.py").write_text("root", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("nested", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", pattern="**/*.py", limit=10)))

    assert result["files"] == ["main.py", "pkg/mod.py"]


def test_glob_star_matches_direct_children_only(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name == "deep.txt":
            raise AssertionError(f"direct glob traversed a nested directory: {path}")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    result = load(run(tool.execute(path=".", pattern="*", sort="path", limit=10)))

    assert result["files"] == ["root.txt"]


def test_glob_ripgrep_filters_direct_pattern_without_positive_glob(tmp_path, monkeypatch):
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(args)
        return FakePopen(args, stdout_text="root.txt\nnested/deep.txt\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", pattern="*", sort="path", limit=10)))

    assert result["files"] == ["root.txt"]
    assert "--glob" not in calls[0]
    assert "--no-ignore" not in calls[0]
    assert "--no-ignore-dot" in calls[0]
    assert calls[0][calls[0].index("--max-depth") + 1] == "1"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_glob_real_ripgrep_matches_python_ignore_and_hidden_rules(tmp_path, monkeypatch):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(
        "local-secret.py\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / ".ignore").write_text("global-secret.py\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("hidden", encoding="utf-8")
    (tmp_path / "global-secret.py").write_text("not a gitignore rule", encoding="utf-8")
    (tmp_path / "local-secret.py").write_text("git exclude rule", encoding="utf-8")
    (tmp_path / "visible.py").write_text("visible", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".gitignore").write_text(
        "nested-secret.py\n",
        encoding="utf-8",
    )
    (tmp_path / "nested" / "keep.py").write_text("keep", encoding="utf-8")
    (tmp_path / "nested" / "nested-secret.py").write_text("skip", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "skip.py").write_text("skip", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    rg_result = load(run(tool.execute(
        path=".",
        pattern="**/*.py",
        sort="path",
        limit=20,
    )))
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    python_result = load(run(tool.execute(
        path=".",
        pattern="**/*.py",
        sort="path",
        limit=20,
    )))

    assert rg_result["files"] == [
        "global-secret.py",
        "nested/keep.py",
        "visible.py",
    ]
    assert python_result["files"] == rg_result["files"]


def test_glob_python_fallback_skips_unresolvable_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    good = tmp_path / "good.txt"
    broken = tmp_path / "broken-link"
    good.write_text("good", encoding="utf-8")
    broken.write_text("", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == broken:
            raise OSError("unresolvable reparse point")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    result = load(run(tool.execute(path=".", pattern="*", sort="path", limit=10)))

    assert result["files"] == ["good.txt"]


def test_glob_python_fallback_prunes_ignored_and_excluded_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "ignored-child.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "excluded").mkdir()
    (tmp_path / "excluded" / "excluded-child.txt").write_text("excluded", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".gitignore").write_text("secret/\n", encoding="utf-8")
    (tmp_path / "nested" / "secret").mkdir()
    (tmp_path / "nested" / "secret" / "nested-secret.txt").write_text(
        "ignored",
        encoding="utf-8",
    )
    (tmp_path / "good.txt").write_text("good", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name in {
            "ignored-child.txt",
            "excluded-child.txt",
            "nested-secret.txt",
        }:
            raise AssertionError(f"pruned directory was traversed: {path}")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    result = load(run(tool.execute(
        path=".",
        pattern="**/*",
        exclude=["excluded/**"],
        sort="path",
        limit=10,
    )))

    assert result["files"] == ["good.txt"]


def test_glob_gitignore_star_does_not_cross_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(
        "local-secret.txt\n",
        encoding="utf-8",
    )
    (tmp_path / ".gitignore").write_text("docs/*.txt\n", encoding="utf-8")
    (tmp_path / "local-secret.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "docs" / "direct.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "docs" / "sub" / "deep.txt").write_text("visible", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(
        path=".",
        pattern="**/*.txt",
        sort="path",
        limit=10,
    )))

    assert result["files"] == ["docs/sub/deep.txt"]


def test_glob_uses_ripgrep_for_directory_listing(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    (tmp_path / "README.md").write_text("notes", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(args, stdout_text="src/app.py\nREADME.md\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", pattern="*", files_only=False, limit=10)))

    assert result["engine"] == "rg"
    assert "src" in result["files"]
    assert "empty" in result["files"]
    assert "README.md" in result["files"]
    assert result["total_known"] is True
    assert calls
    assert "--glob" not in calls[0][0]
    assert calls[0][0][calls[0][0].index("--max-depth") + 1] == "1"


def test_glob_streaming_ripgrep_stops_after_page_plus_one(tmp_path, monkeypatch):
    for index in range(1005):
        (tmp_path / f"file_{index:04}.txt").write_text("x", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    proc_holder = {}

    def fake_popen(args, **kwargs):
        stdout_text = "".join(f"file_{index:04}.txt\n" for index in range(1005))
        proc = LongRunningPopen(args, stdout_text=stdout_text, **kwargs)
        proc_holder["proc"] = proc
        return proc

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = ListFilesTool(make_config(tmp_path, command_timeout_seconds=30))

    result = load(run(tool.execute(path=".", pattern="*", limit=10)))

    assert result["engine"] == "rg"
    assert result["truncated"] is True
    assert result["files"] == [f"file_{index:04}.txt" for index in range(10)]
    assert result["next_offset"] == 10
    assert result["total"] is None
    assert result["total_known"] is False
    assert result["observed_count"] == 11
    assert result["scanned_entries"] == 11
    assert proc_holder["proc"].terminated is True


def test_glob_python_fallback_when_ripgrep_times_out(tmp_path, monkeypatch):
    fake_rg = tmp_path / "rg.exe"
    fallback_calls = []

    def hanging_popen(args, **kwargs):
        return HangingPopen(args, **kwargs)

    def fake_python(**kwargs):
        fallback_calls.append(kwargs)
        return {
            "root": ".",
            "files": ["fallback.txt"],
            "count": 1,
            "total": 1,
            "total_known": True,
            "observed_count": 1,
            "truncated": False,
            "next_offset": None,
            "engine": "python",
            "sort": kwargs["sort"],
            "scanned_entries": 1,
        }

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", hanging_popen)
    monkeypatch.setattr(code_python_fallback, "_glob_files_python", fake_python)
    tool = ListFilesTool(make_config(tmp_path, command_timeout_seconds=1))

    result = load(run(tool.execute(path=".", files_only=True, limit=10)))

    assert result["engine"] == "python"
    assert result["fallback_reason"] == "ripgrep_timeout"
    assert result["files"] == ["fallback.txt"]
    assert fallback_calls


def test_glob_observation_events_do_not_change_result(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))
    events = []

    plain = load(run(tool.execute(path=".", pattern="*.py", limit=10)))
    observed = load(run(tool.execute(
        path=".",
        pattern="*.py",
        limit=10,
        _runtime_context={"tool_event_sink": events.append},
    )))

    assert observed == plain
    assert any(event["event_type"] == "tool_progress" for event in events)


def test_tool_manager_preserves_glob_pattern_argument(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "src" / "app.ts").write_text("console.log('hi')", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(args, stdout_text="src/app.py\nsrc/app.ts\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "web_search": {"enabled": False},
                "code": {
                    "enabled": True,
                    "workspace_roots": [str(tmp_path)],
                },
            },
        }
    })

    result = load(run(manager.execute_tool("glob", {
        "path": ".",
        "pattern": "**/*.py",
        "limit": 10,
    })))

    assert result["engine"] == "rg"
    assert result["files"] == ["src/app.py"]
    args, _kwargs = calls[0]
    assert "--glob" not in args


def test_tool_manager_normalizes_glob_single_argument_wildcard_as_pattern(tmp_path, monkeypatch):
    (tmp_path / "backend" / "core" / "tools").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "tools" / "code_tools.py").write_text("x", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"

    def fake_popen(args, **kwargs):
        assert kwargs["cwd"] == str(tmp_path)
        assert "--glob" not in args
        return FakePopen(args, stdout_text="backend/core/tools/code_tools.py\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "web_search": {"enabled": False},
                "code": {
                    "enabled": True,
                    "workspace_roots": [str(tmp_path)],
                },
            },
        }
    })

    result = load(run(manager.execute_tool("glob", {"arguments": r"backend\core\tools\*.py"})))

    assert result["engine"] == "rg"
    assert result["files"] == ["backend/core/tools/code_tools.py"]


def test_read_file_reads_utf8_line_slice(tmp_path):
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    tool = ReadFileTool(make_config(tmp_path, max_read_chars=200))

    result = load(run(tool.execute(
        path="notes.txt",
        targets=[],
        tool_result_id="",
        start_line=2,
        line_count=2,
    )))

    assert result["path"] == "notes.txt"
    assert result["start_line"] == 2
    assert result["content"] == "2\ttwo\n3\tthree"
    assert result["line_count"] == 2
    assert result["total_lines"] == 4
    assert "version" not in result
    assert result["truncated"] is True


def test_read_file_schema_has_three_unambiguous_inputs(tmp_path):
    schema = ReadFileTool(make_config(tmp_path)).parameters_schema()

    assert "source" not in schema["properties"]
    assert "id" not in schema["properties"]
    assert schema["properties"]["targets"]["minItems"] == 1
    assert "default" not in schema["properties"]["start_line"]
    assert "default" not in schema["properties"]["format"]
    assert "expected_version" not in EditFileTool(make_config(tmp_path)).parameters_schema()["properties"]
    assert "expected_version" not in WriteFileTool(make_config(tmp_path)).parameters_schema()["properties"]


def test_read_file_reads_multiple_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    tool = ReadFileTool(make_config(tmp_path, max_read_chars=200))

    context = {"file_observations": {}}
    result = load(run(tool.execute(
        targets=[{"path": "a.txt"}, {"path": "b.txt"}], _runtime_context=context,
    )))

    assert [file["path"] for file in result["files"]] == ["a.txt", "b.txt"]
    assert [file["content"] for file in result["files"]] == ["1\talpha", "1\tbeta"]
    assert all("version" not in file for file in result["files"])
    assert len(context["file_observations"]) == 2


def test_read_file_streams_window_without_read_text(tmp_path, monkeypatch):
    target = tmp_path / "large.txt"
    target.write_text("0123456789abcdef", encoding="utf-8")

    def fail_read_text(*args, **kwargs):
        raise AssertionError("read_file should not read the whole file")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    tool = ReadFileTool(make_config(tmp_path, max_read_chars=4))

    result = load(run(tool.execute(path="large.txt", max_chars_per_file=4)))

    assert result["path"] == "large.txt"
    assert result["start_line"] == 1
    assert result["content"] == "1\t0123"
    assert result["truncated"] is True


def test_read_file_rejects_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    tool = ReadFileTool(make_config(tmp_path))

    result = load(run(tool.execute(path=str(outside))))

    assert result["error"]["type"] == "invalid_path"


def test_read_file_rejects_non_utf8(tmp_path):
    (tmp_path / "bad.bin").write_bytes(b"\xff\xfe\xfd")
    tool = ReadFileTool(make_config(tmp_path))

    result = load(run(tool.execute(path="bad.bin")))

    assert result["error"]["type"] == "not_utf8"


def test_grep_returns_matching_lines_and_skips_non_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "src" / "other.txt").write_text("no match\n", encoding="utf-8")
    (tmp_path / "src" / "bad.py").write_bytes(b"\xff\xfe")
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(pattern="needle", path="src", glob="*.py", regex=False, output="content", limit=10)))

    assert result["matches"] == [{
        "path": "src/app.py",
        "line": 2,
        "text": "needle here",
        "type": "match",
    }]
    assert result["skipped_non_utf8"] == ["src/bad.py"]
    assert result["truncated"] is False


def test_grep_uses_ripgrep_and_translates_options(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha\nNeedle HERE\n", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    event = {
        "type": "match",
        "data": {
            "path": {"text": "app.py"},
            "lines": {"text": "Needle HERE\n"},
            "line_number": 2,
        },
    }
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(args, stdout_text=json.dumps(event, ensure_ascii=False) + "\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(
        pattern="needle",
        path="src",
        glob="*.py",
        regex=False,
        ignore_case=True,
        respect_gitignore=False,
        include_hidden=True,
        output="content",
        limit=10,
    )))

    args = calls[0][0]
    assert args[0] == str(fake_rg)
    assert "--fixed-strings" in args
    assert "--ignore-case" in args
    assert "--no-ignore" in args
    assert "--hidden" in args
    assert args[args.index("--glob") + 1] == "*.py"
    assert args[-3:] == ["--", "needle", "."]
    assert calls[0][1]["cwd"] == str(tmp_path / "src")
    assert result["engine"] == "rg"
    assert result["matches"] == [{
        "path": "src/app.py",
        "line": 2,
        "text": "Needle HERE",
        "type": "match",
    }]


def test_ripgrep_resolver_prefers_packaged_binary(tmp_path, monkeypatch):
    executable_name = "rg.exe" if os.name == "nt" else "rg"
    bundled = tmp_path / "tools" / "ripgrep" / executable_name
    bundled.parent.mkdir(parents=True)
    bundled.write_text("", encoding="utf-8")
    if os.name != "nt":
        bundled.chmod(0o755)
    monkeypatch.setattr(code_ripgrep.sys, "_MEIPASS", str(tmp_path), raising=False)

    resolved = code_ripgrep._resolve_ripgrep_executable()

    assert resolved == bundled


def test_grep_ripgrep_normalizes_backslash_glob(tmp_path, monkeypatch):
    (tmp_path / "backend" / "core" / "tools").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "tools" / "code_tools.py").write_text("class ListFilesTool:\n", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(args, stdout_text="backend/core/tools/code_tools.py\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(
        pattern="class ListFilesTool",
        path=".",
        glob=r"backend\core\tools\*.py",
        output="files",
        limit=10,
    )))

    args = calls[0][0]
    assert args[args.index("--glob") + 1] == "backend/core/tools/*.py"
    assert result["files"] == ["backend/core/tools/code_tools.py"]


def test_grep_single_file_matches_workspace_relative_glob(tmp_path, monkeypatch):
    (tmp_path / "backend" / "core" / "tools").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "tools" / "code_tools.py").write_text("class ListFilesTool:\n", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"

    def fake_popen(args, **kwargs):
        return FakePopen(args, stdout_text="code_tools.py\n", **kwargs)

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(
        pattern="class ListFilesTool",
        path="backend/core/tools/code_tools.py",
        glob="backend/core/tools/*.py",
        output="files",
        limit=10,
    )))

    assert result["engine"] == "rg"
    assert result["files"] == ["backend/core/tools/code_tools.py"]


def test_grep_single_file_rg_fast_path_deduplicates_context(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    events = [
        {
            "type": "context",
            "data": {
                "path": {"text": "app.py"},
                "lines": {"text": "old context\n"},
                "line_number": 1,
            },
        },
        {
            "type": "context",
            "data": {
                "path": {"text": "app.py"},
                "lines": {"text": "duplicate context\n"},
                "line_number": 1,
            },
        },
        {
            "type": "match",
            "data": {
                "path": {"text": "app.py"},
                "lines": {"text": "needle\n"},
                "line_number": 1,
            },
        },
    ]

    def fake_popen(args, **kwargs):
        return FakePopen(
            args,
            stdout_text="\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
            **kwargs,
        )

    def fail_visibility_check(self, path):
        raise AssertionError("single-file rg events should not repeat workspace visibility checks")

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(code_tools.CodeWorkspace, "is_visible", fail_visibility_check)
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(pattern="needle", path="src/app.py", output="content", context=1)))

    assert result["engine"] == "rg"
    assert result["matches"] == [{
        "path": "src/app.py",
        "line": 1,
        "text": "needle",
        "type": "match",
    }]


def test_grep_count_uses_ripgrep_when_available(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\nneedle\n", encoding="utf-8")
    fake_rg = tmp_path / "rg.exe"
    events = [
        {
            "type": "match",
            "data": {
                "path": {"text": "app.py"},
                "lines": {"text": "needle\n"},
                "line_number": 1,
            },
        },
        {
            "type": "match",
            "data": {
                "path": {"text": "app.py"},
                "lines": {"text": "needle\n"},
                "line_number": 2,
            },
        },
    ]
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakePopen(
            args,
            stdout_text="\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
            **kwargs,
        )

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(pattern="needle", path="src", output="count", limit=10)))

    assert result["engine"] == "rg"
    assert result["counts"] == [{"path": "src/app.py", "count": 2}]
    assert "--json" in calls[0][0]


def test_grep_python_fallback_when_ripgrep_times_out(tmp_path, monkeypatch):
    fake_rg = tmp_path / "rg.exe"
    fallback_calls = []

    def hanging_popen(args, **kwargs):
        return HangingPopen(args, **kwargs)

    def fake_python(**kwargs):
        fallback_calls.append(kwargs)
        return {
            "pattern": kwargs["pattern"],
            "matches": [{"path": "fallback.txt", "line": 1, "preview": "needle", "type": "match"}],
            "searched_files": 1,
            "skipped_non_utf8": [],
            "truncated": False,
            "engine": "python",
        }

    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: fake_rg)
    monkeypatch.setattr(subprocess, "Popen", hanging_popen)
    monkeypatch.setattr(code_python_fallback, "_grep_python", fake_python)
    tool = SearchFilesTool(make_config(tmp_path, command_timeout_seconds=1))

    result = load(run(tool.execute(pattern="needle", path=".", output="content")))

    assert result["engine"] == "python"
    assert result["fallback_reason"] == "ripgrep_timeout"
    assert result["matches"] == [{"path": "fallback.txt", "line": 1, "text": "needle", "type": "match"}]
    assert fallback_calls


def test_grep_python_fallback_supports_regex_and_ignore_case(tmp_path, monkeypatch):
    monkeypatch.setattr(code_ripgrep, "_resolve_ripgrep_executable", lambda: None)
    (tmp_path / "notes.txt").write_text("Alpha\nneedle-42\n", encoding="utf-8")
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(pattern=r"NEEDLE-\d+", ignore_case=True, output="content")))

    assert result["engine"] == "python"
    assert result["fallback_reason"] == "ripgrep_not_installed"
    assert result["matches"][0]["text"] == "needle-42"


def test_edit_file_replaces_unique_match_and_rejects_ambiguous_edit(tmp_path):
    (tmp_path / "app.py").write_text("old\nkeep\nold\n", encoding="utf-8")
    tool = EditFileTool(make_config(tmp_path))
    assert "create_parents" not in tool.parameters_schema()["properties"]

    ambiguous = load(run(tool.execute(
        operation="replace",
        path="app.py",
        replacements=[{"old": "old", "new": "new"}],
    )))
    assert ambiguous["error"]["type"] == "edit_not_unique"

    ok = load(run(tool.execute(
        operation="replace",
        path="app.py",
        replacements=[{"old": "old\nkeep\n", "new": "new\nkeep\n"}],
    )))

    assert ok["path"] == "app.py"
    assert ok["replacements"] == 1
    assert "version" not in ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "new\nkeep\nold\n"

    created = load(run(tool.execute(
        operation="create",
        path="nested/new.txt",
        content="hello",
    )))
    assert created["path"] == "nested/new.txt"
    assert (tmp_path / "nested" / "new.txt").read_text(encoding="utf-8") == "hello"


def test_edit_file_preserves_exact_replacement_guards(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("same\nsame\n", encoding="utf-8")
    tool = EditFileTool(make_config(tmp_path))

    missing = load(run(tool.execute(
        operation="replace", path="app.py", replacements=[{"old": "absent", "new": "new"}],
    )))
    mismatch = load(run(tool.execute(
        operation="replace",
        path="app.py",
        replacements=[{"old": "same", "new": "new", "replace_all": True, "expected_count": 1}],
    )))
    numbered = load(run(tool.execute(
        operation="replace", path="app.py", replacements=[{"old": "1\tsame", "new": "new"}],
    )))
    replaced = load(run(tool.execute(
        operation="replace",
        path="app.py",
        replacements=[{"old": "same", "new": "new", "replace_all": True, "expected_count": 2}],
    )))

    assert missing["error"]["type"] == "edit_not_found"
    assert mismatch["error"]["type"] == "edit_count_mismatch"
    assert numbered["error"]["type"] == "invalid_edit"
    assert replaced["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "new\nnew\n"


def test_write_file_writes_utf8_and_creates_missing_parents(tmp_path):
    tool = WriteFileTool(make_config(tmp_path))

    nested = load(run(tool.execute(path="new/file.txt", content="hello")))
    assert nested["path"] == "new/file.txt"
    assert (tmp_path / "new" / "file.txt").read_text(encoding="utf-8") == "hello"

    ok = load(run(tool.execute(path="file.txt", content="hello")))
    assert ok["path"] == "file.txt"
    assert ok["bytes_written"] == 5
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.parametrize("output_format", ["numbered", "raw", "json"])
def test_complete_read_authorizes_overwrite_and_write_refreshes_observation(tmp_path, output_format):
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    context = {"file_observations": {}}
    config = make_config(tmp_path, max_read_chars=200)

    read_result = load(run(ReadFileTool(config).execute(
        path="file.txt", format=output_format, _runtime_context=context,
    )))
    first = load(run(WriteFileTool(config).execute(
        path="file.txt", content="first\n", mode="overwrite", _runtime_context=context,
    )))
    second = load(run(WriteFileTool(config).execute(
        path="file.txt", content="second\n", mode="overwrite", _runtime_context=context,
    )))

    assert "version" not in read_result
    assert "version" not in first
    assert second["mode"] == "overwrite"
    assert target.read_text(encoding="utf-8") == "second\n"


@pytest.mark.parametrize("read_args", [{"start_line": 2}, {"max_chars_per_file": 3}])
def test_partial_or_truncated_read_does_not_authorize_overwrite(tmp_path, read_args):
    target = tmp_path / "file.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    context = {"file_observations": {}}
    config = make_config(tmp_path, max_read_chars=200)

    load(run(ReadFileTool(config).execute(path="file.txt", _runtime_context=context, **read_args)))
    result = load(run(WriteFileTool(config).execute(
        path="file.txt", content="new\n", mode="overwrite", _runtime_context=context,
    )))

    assert result["error"]["type"] == "stale_file"
    assert "version" not in json.dumps(result)
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"


def test_partial_read_preserves_only_a_matching_prior_observation(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    context = {"file_observations": {}}
    tool = ReadFileTool(make_config(tmp_path, max_read_chars=200))

    load(run(tool.execute(path="file.txt", _runtime_context=context)))
    load(run(tool.execute(path="file.txt", start_line=2, _runtime_context=context)))
    assert len(context["file_observations"]) == 1

    target.write_text("external\nsecond\n", encoding="utf-8")
    load(run(tool.execute(path="file.txt", start_line=2, _runtime_context=context)))
    assert context["file_observations"] == {}


def test_overwrite_requires_fresh_same_run_observation(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    config = make_config(tmp_path, max_read_chars=200)
    observed = {"file_observations": {}}
    other_run = {"file_observations": {}}

    unobserved = load(run(WriteFileTool(config).execute(
        path="file.txt", content="new\n", mode="overwrite", _runtime_context=other_run,
    )))
    load(run(ReadFileTool(config).execute(path="file.txt", _runtime_context=observed)))
    target.write_text("external\n", encoding="utf-8")
    stale = load(run(WriteFileTool(config).execute(
        path="file.txt", content="new\n", mode="overwrite", _runtime_context=observed,
    )))

    assert unobserved["error"]["type"] == "stale_file"
    assert stale["error"]["type"] == "stale_file"
    assert "current_version" not in stale["error"]
    assert target.read_text(encoding="utf-8") == "external\n"


def test_overwrite_does_not_recreate_observed_file_deleted_externally(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("old\n", encoding="utf-8")
    context = {"file_observations": {}}
    config = make_config(tmp_path, max_read_chars=200)

    load(run(ReadFileTool(config).execute(path="file.txt", _runtime_context=context)))
    target.unlink()
    result = load(run(WriteFileTool(config).execute(
        path="file.txt", content="new\n", mode="overwrite", _runtime_context=context,
    )))

    assert result["error"]["type"] == "stale_file"
    assert not target.exists()


def test_create_existing_file_fails_without_hash(tmp_path):
    (tmp_path / "file.txt").write_text("old\n", encoding="utf-8")

    result = load(run(WriteFileTool(make_config(tmp_path)).execute(
        path="file.txt", content="new\n", mode="create", _runtime_context={"file_observations": {}},
    )))

    assert result["error"]["type"] == "file_exists"
    assert "read it completely before overwriting" in result["error"]["message"]
    assert "version" not in json.dumps(result)


def test_successful_create_establishes_observation_for_overwrite(tmp_path):
    context = {"file_observations": {}}
    tool = WriteFileTool(make_config(tmp_path))

    created = load(run(tool.execute(
        path="file.txt", content="first\n", mode="create", _runtime_context=context,
    )))
    overwritten = load(run(tool.execute(
        path="file.txt", content="second\n", mode="overwrite", _runtime_context=context,
    )))

    assert created["mode"] == "create"
    assert overwritten["mode"] == "overwrite"
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "second\n"


def test_write_file_rejects_protected_path(tmp_path):
    (tmp_path / ".git").mkdir()
    tool = WriteFileTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".git/config", content="oops")))

    assert result["error"]["type"] == "protected_path"


def test_apply_patch_updates_existing_file(tmp_path):
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8")
    tool = ApplyPatchTool(make_config(tmp_path))
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print('old')
+print('new')
"""

    result = load(run(tool.execute(patch=patch)))

    assert result["applied"] is True
    assert result["files_changed"] == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('new')\n"


@pytest.mark.parametrize("operation", ["replace", "patch"])
def test_targeted_edits_invalidate_full_file_observation(tmp_path, operation):
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    context = {"file_observations": {}}
    config = make_config(tmp_path, max_read_chars=200)
    load(run(ReadFileTool(config).execute(path="app.py", _runtime_context=context)))

    if operation == "replace":
        result = load(run(EditFileTool(config).execute(
            operation="replace",
            path="app.py",
            replacements=[{"old": "old", "new": "new"}],
            _runtime_context=context,
        )))
    else:
        result = load(run(ApplyPatchTool(config).execute(
            patch="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            _runtime_context=context,
        )))
    overwrite = load(run(WriteFileTool(config).execute(
        path="app.py", content="whole\n", mode="overwrite", _runtime_context=context,
    )))

    assert "error" not in result
    assert overwrite["error"]["type"] == "stale_file"
    assert target.read_text(encoding="utf-8") == "new\n"


def test_apply_patch_offsets_hunk_when_context_matches_uniquely(tmp_path):
    (tmp_path / "notes.md").write_text(
        "# Title\n\nIntro\n\n## Tasks\n- [ ] Search\n- [ ] Run\n",
        encoding="utf-8",
    )
    tool = ApplyPatchTool(make_config(tmp_path))
    patch = """--- a/notes.md
+++ b/notes.md
@@ -3,5 +3,5 @@
 ## Tasks
-- [ ] Search
-- [ ] Run
+- [x] Search
+- [x] Run
"""

    result = load(run(tool.execute(patch=patch)))

    assert result["applied"] is True
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == (
        "# Title\n\nIntro\n\n## Tasks\n- [x] Search\n- [x] Run\n"
    )


def test_apply_patch_streams_rewrite_without_read_text_or_write_text(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("first\nsecond\nthird\n", encoding="utf-8")
    tool = ApplyPatchTool(make_config(tmp_path))
    patch = """--- a/notes.md
+++ b/notes.md
@@ -2 +2 @@
-second
+updated
"""

    def fail_whole_file_method(*args, **kwargs):
        raise AssertionError("apply_patch should not use whole-file read_text/write_text")

    monkeypatch.setattr(Path, "read_text", fail_whole_file_method)
    monkeypatch.setattr(Path, "write_text", fail_whole_file_method)

    result = load(run(tool.execute(patch=patch)))

    assert result["applied"] is True
    assert (tmp_path / "notes.md").open(encoding="utf-8").read() == "first\nupdated\nthird\n"


def test_apply_patch_rejects_ambiguous_offset_match(tmp_path):
    (tmp_path / "notes.md").write_text(
        "## Tasks\n- [ ] Search\n- [ ] Run\n\n## Tasks\n- [ ] Search\n- [ ] Run\n",
        encoding="utf-8",
    )
    tool = ApplyPatchTool(make_config(tmp_path))
    patch = """--- a/notes.md
+++ b/notes.md
@@ -99,3 +99,3 @@
 ## Tasks
-- [ ] Search
-- [ ] Run
+- [x] Search
+- [x] Run
"""

    result = load(run(tool.execute(patch=patch)))

    assert result["error"]["type"] == "patch_failed"
    assert "multiple matching locations" in result["error"]["message"]


def test_apply_patch_rejects_delete_file_patch(tmp_path):
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8")
    tool = ApplyPatchTool(make_config(tmp_path))
    patch = """--- a/app.py
+++ /dev/null
@@ -1 +0,0 @@
-print('old')
"""

    result = load(run(tool.execute(patch=patch)))

    assert result["error"]["type"] == "patch_failed"
    assert (tmp_path / "app.py").exists()


def test_run_command_runs_in_workspace_and_returns_output(tmp_path):
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command=f'"{sys.executable}" -c "print(\'hello\')"')))

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello"
    assert result["timed_out"] is False


def test_run_command_passes_utf8_python_env_without_overwriting_existing_values(tmp_path, monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs.copy())
        return subprocess.CompletedProcess(kwargs["args"], 0, b"ok", b"")

    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="python -c \"print('ignored')\"")))

    assert result["exit_code"] == 0
    env = calls[0]["env"]
    assert env is not os.environ
    assert env["PYTHONIOENCODING"] == "latin-1"
    assert env["PYTHONUTF8"] == "1"


def test_run_command_python_stdout_defaults_to_utf8_for_unicode(tmp_path):
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command=f'"{sys.executable}" -c "print(\'Emoji 😀♪€\')"')))

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "Emoji 😀♪€"
    assert result["stderr"] == ""


def test_run_command_uses_chardet_for_gbk_output_and_keeps_truncation(tmp_path, monkeypatch):
    stdout_text = "工具测试报告额外"
    stderr_text = "错误测试报告额外"

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=stdout_text.encode("gbk"),
            stderr=stderr_text.encode("gbk"),
        )

    fake_chardet = SimpleNamespace(detect=lambda value: {"encoding": "cp936", "confidence": 0.99})
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setitem(sys.modules, "chardet", fake_chardet)
    tool = RunCommandTool(make_config(tmp_path, max_output_chars=6))

    result = load(run(tool.execute(command="python -c \"print('ignored')\"")))

    assert result["exit_code"] == 0
    assert result["stdout"] == "工具测试报告"
    assert result["stderr"] == "错误测试报告"


def test_run_command_windows_python_c_uses_argument_list(tmp_path, monkeypatch):
    real_run = subprocess.run
    calls = []
    code = "\nimport sys\nsys.stdout.write('no newline')\nsys.stdout.flush()\n"
    command = f'"{sys.executable}" -c "{code}"'

    def record_run(*args, **kwargs):
        calls.append(kwargs.copy())
        return real_run(*args, **kwargs)

    monkeypatch.setattr(code_common, "_windows_python_c_args", lambda value: [sys.executable, "-c", code] if value == command else None)
    monkeypatch.setattr(subprocess, "run", record_run)
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command=command)))

    assert result["command"] == command
    assert result["exit_code"] == 0
    assert result["stdout"] == "no newline"
    assert result["stderr"] == ""
    assert result["timed_out"] is False
    assert calls[0]["args"] == [sys.executable, "-c", code]
    assert calls[0]["shell"] is False


def test_run_command_non_python_command_keeps_shell_path(tmp_path, monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs.copy())
        return subprocess.CompletedProcess(kwargs["args"], 0, b"plain shell", b"")

    monkeypatch.setattr(code_common, "_windows_python_c_args", lambda value: None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="echo plain shell")))

    assert result["stdout"] == "plain shell"
    assert calls[0]["args"][-1] == (
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); echo plain shell"
    )
    assert calls[0]["shell"] is False


def test_run_command_hides_windows_console_window(tmp_path, monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs.copy())
        return subprocess.CompletedProcess(kwargs["args"], 0, b"quiet", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess_utils.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        subprocess_utils.subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
        raising=False,
    )
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="echo quiet")))

    assert result["stdout"] == "quiet"
    assert calls[0]["creationflags"] == 0x08000000


def test_run_command_rejects_cwd_outside_workspace(tmp_path):
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="git status", cwd=str(tmp_path.parent))))

    assert result["error"]["type"] == "invalid_path"


def test_tool_manager_returns_structured_error_when_tool_raises_not_implemented():
    class BrokenTool(BaseTool):
        @property
        def name(self) -> str:
            return "shell"

        @property
        def description(self) -> str:
            return "broken test tool"

        def parameters_schema(self):
            return {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            }

        async def execute(self, **kwargs) -> str:
            raise NotImplementedError()

    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "web_search": {"enabled": False},
                "code": {"enabled": False},
            },
        }
    })
    manager.register(BrokenTool())

    result = load(run(manager.execute_tool("shell", {"command": "echo hi"})))

    assert result["error"] == {
        "type": "NotImplementedError",
        "message": "",
        "tool_name": "shell",
    }


def test_tool_manager_registers_only_canonical_code_tools(tmp_path):
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "web_search": {"enabled": False},
                "code": {
                    "enabled": True,
                    "workspace_roots": [str(tmp_path)],
                },
            },
        }
    })

    names = [tool["function"]["name"] for tool in manager.get_openai_tools()]

    assert "grep" in names
    assert "glob" in names
    assert "read" in names
    assert "edit" in names
    assert "patch" not in names
    assert "shell" in names
    assert "write" not in names
    assert manager.get_tool("write") is None
    assert manager.get_tool("patch") is None


def test_tool_manager_full_exposure_keeps_write_internal(tmp_path):
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "exposure": "full",
                "web_search": {"enabled": False},
                "code": {
                    "enabled": True,
                    "workspace_roots": [str(tmp_path)],
                },
            },
        }
    })

    names = [tool["function"]["name"] for tool in manager.get_openai_tools()]

    assert "edit" in names
    assert "write" not in names


def test_tool_manager_normalizes_compact_run_command_arguments(tmp_path):
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "web_search": {"enabled": False},
                "code": {
                    "enabled": True,
                    "workspace_roots": [str(tmp_path)],
                },
            },
        }
    })

    result = load(run(manager.execute_tool("shell", {"arguments": f'"{sys.executable}" -c "print(\'compact\')"'})))

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "compact"


def test_run_command_does_not_use_event_loop_subprocess(tmp_path, monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("asyncio subprocess should not be used")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fail_if_called)
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command=f'"{sys.executable}" -c "print(\'selector-safe\')"')))

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "selector-safe"
    assert result["timed_out"] is False


def test_run_command_returns_structured_timeout(tmp_path, monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=kwargs["args"],
            timeout=kwargs["timeout"],
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    tool = RunCommandTool(make_config(tmp_path))

    command = f'"{sys.executable}" -c "print(\'slow\')"'

    result = load(run(tool.execute(command=command, timeout_seconds=1)))

    assert result == {
        "command": command,
        "cwd": ".",
        "exit_code": None,
        "stdout": "partial stdout",
        "stderr": "partial stderr",
        "timed_out": True,
    }


def test_run_command_returns_structured_error_when_execution_fails(tmp_path, monkeypatch):
    def raise_os_error(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", raise_os_error)
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="python -V")))

    assert result["error"] == {
        "type": "OSError",
        "message": "boom",
            "tool_name": "shell",
        "command": "python -V",
        "cwd": ".",
    }
