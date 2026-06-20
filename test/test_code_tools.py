import asyncio
import json
from pathlib import Path

from backend.core.tools.code_tools import (
    ApplyPatchTool,
    CodeToolConfig,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    WriteFileTool,
)


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


def test_run_command_rejects_cwd_outside_workspace(tmp_path):
    tool = RunCommandTool(make_config(tmp_path))

    result = load(run(tool.execute(command="git status", cwd=str(tmp_path.parent))))

    assert result["error"]["type"] == "invalid_path"
