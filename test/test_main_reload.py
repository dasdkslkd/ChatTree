from pathlib import Path

from main import PROJECT_ROOT, uvicorn_reload_options


def test_uvicorn_reload_excludes_runtime_tool_workspace():
    options = uvicorn_reload_options()

    reload_dirs = [Path(path) for path in options["reload_dirs"]]
    assert reload_dirs == [PROJECT_ROOT / "backend"]
    assert options["reload_includes"] == ["*.py"]

    assert not (PROJECT_ROOT / "tmp").is_relative_to(reload_dirs[0])
    assert not (PROJECT_ROOT / "data").is_relative_to(reload_dirs[0])
    assert not (PROJECT_ROOT / "frontend").is_relative_to(reload_dirs[0])
    assert "**/__pycache__/**" in set(options["reload_excludes"])
