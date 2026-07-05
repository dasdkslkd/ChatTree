import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

from backend.core.tools import code_tools
from backend.core.tools.base import BaseTool
from backend.core.tools.code_tools import (
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


def make_config(tmp_path: Path, **overrides) -> CodeToolConfig:
    config = {
        "workspace_roots": [str(tmp_path)],
        "protected_paths": [".git", "data/config.json"],
        "command_timeout_seconds": 2,
        "max_read_chars": 20,
        "max_output_chars": 200,
        "allow_parent_dir_creation": False,
    }
    config.update(overrides)
    return CodeToolConfig.from_dict(config)


def test_run_command_default_initial_wait_is_120_seconds(tmp_path):
    config = CodeToolConfig.from_dict({
        "workspace_roots": [str(tmp_path)],
        "protected_paths": [".git"],
    })

    assert config.run_command_initial_wait_seconds == 120.0


def test_list_files_lists_workspace_files_and_hides_protected_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    tool = ListFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(path=".", recursive=True, max_results=10)))

    assert {item["path"] for item in result["items"]} == {"src", "src/app.py"}
    assert result["truncated"] is False


def test_read_file_reads_utf8_slice_and_next_offset(tmp_path):
    (tmp_path / "notes.txt").write_text("0123456789abcdef", encoding="utf-8")
    tool = ReadFileTool(make_config(tmp_path, max_read_chars=5))

    result = load(run(tool.execute(path="notes.txt", offset=2, limit=5)))

    assert result == {
        "path": "notes.txt",
        "content": "23456",
        "next_offset": 7,
        "truncated": True,
    }


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


def test_search_files_returns_matching_lines_and_skips_non_utf8(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "src" / "other.txt").write_text("no match\n", encoding="utf-8")
    (tmp_path / "src" / "bad.py").write_bytes(b"\xff\xfe")
    tool = SearchFilesTool(make_config(tmp_path))

    result = load(run(tool.execute(query="needle", path="src", glob="*.py", max_results=10)))

    assert result["matches"] == [{
        "path": "src/app.py",
        "line": 2,
        "preview": "needle here",
    }]
    assert result["skipped_non_utf8"] == ["src/bad.py"]
    assert result["truncated"] is False


def test_edit_file_replaces_unique_match_and_rejects_ambiguous_edit(tmp_path):
    (tmp_path / "app.py").write_text("old\nkeep\nold\n", encoding="utf-8")
    tool = EditFileTool(make_config(tmp_path))

    ambiguous = load(run(tool.execute(path="app.py", old_string="old", new_string="new")))
    assert ambiguous["error"]["type"] == "edit_not_unique"

    ok = load(run(tool.execute(
        path="app.py",
        old_string="old\nkeep\n",
        new_string="new\nkeep\n",
    )))

    assert ok["path"] == "app.py"
    assert ok["replacements"] == 1
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "new\nkeep\nold\n"


def test_write_file_writes_utf8_and_rejects_parent_creation_by_default(tmp_path):
    tool = WriteFileTool(make_config(tmp_path))

    missing_parent = load(run(tool.execute(path="new/file.txt", content="hello")))
    assert missing_parent["error"]["type"] == "not_found"

    ok = load(run(tool.execute(path="file.txt", content="hello")))
    assert ok["path"] == "file.txt"
    assert ok["bytes_written"] == 5
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "hello"


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

    result = load(run(tool.execute(command="python -c \"print('hello')\"")))

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


def test_run_command_windows_multiline_python_c_uses_argument_list(tmp_path, monkeypatch):
    real_run = subprocess.run
    calls = []
    code = "\nimport sys\nsys.stdout.write('no newline')\nsys.stdout.flush()\n"
    command = f'"{sys.executable}" -c "{code}"'

    def record_run(*args, **kwargs):
        calls.append(kwargs.copy())
        return real_run(*args, **kwargs)

    monkeypatch.setattr(code_tools.os, "name", "nt")
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

    monkeypatch.setattr(code_tools.os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="echo plain shell")))

    assert result["stdout"] == "plain shell"
    assert calls[0]["args"] == "echo plain shell"
    assert calls[0]["shell"] is True


def test_run_command_rejects_cwd_outside_workspace(tmp_path):
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="git status", cwd=str(tmp_path.parent))))

    assert result["error"]["type"] == "invalid_path"


def test_tool_manager_returns_structured_error_when_tool_raises_not_implemented():
    class BrokenTool(BaseTool):
        @property
        def name(self) -> str:
            return "run_command"

        @property
        def description(self) -> str:
            return "broken test tool"

        def parameters_schema(self):
            return {"type": "object", "properties": {}}

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

    result = load(run(manager.execute_tool("run_command", {"command": "echo hi"})))

    assert result["error"] == {
        "type": "NotImplementedError",
        "message": "",
        "tool_name": "run_command",
    }


def test_tool_manager_coding_exposure_hides_raw_write_file_but_keeps_it_executable(tmp_path):
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

    assert "search_files" in names
    assert "edit_file" in names
    assert "apply_patch" in names
    assert "write_file" not in names
    result = load(run(manager.execute_tool("write_file", {"path": "notes.txt", "content": "ok"})))
    assert result["path"] == "notes.txt"


def test_tool_manager_full_exposure_can_show_write_file(tmp_path):
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

    assert "write_file" in names


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

    result = load(run(manager.execute_tool("run_command", {"arguments": "python -c \"print('compact')\""})))

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "compact"


def test_run_command_does_not_use_event_loop_subprocess(tmp_path, monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("asyncio subprocess should not be used")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fail_if_called)
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="python -c \"print('selector-safe')\"")))

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

    result = load(run(tool.execute(command="python -c \"print('slow')\"", timeout_seconds=1)))

    assert result == {
        "command": "python -c \"print('slow')\"",
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
        "tool_name": "run_command",
        "command": "python -V",
        "cwd": ".",
    }
