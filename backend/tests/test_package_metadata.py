from __future__ import annotations

from importlib import resources
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from backend.core.server import SERVER_VERSION


ROOT = Path(__file__).resolve().parents[2]


def _read_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _requirements() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / "backend" / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_project_metadata_exposes_chattree_server_console_script():
    document = _read_pyproject()

    assert document["project"]["name"] == "chattree-server"
    assert document["project"]["version"] == SERVER_VERSION
    assert document["project"]["requires-python"] == ">=3.11"
    assert (
        document["project"]["scripts"]["chattree-server"]
        == "backend.server_cli:main"
    )


def test_project_dependencies_match_backend_requirements():
    dependencies = _read_pyproject()["project"]["dependencies"]

    assert dependencies == _requirements()


def test_build_extra_declares_pyinstaller():
    extras = _read_pyproject()["project"]["optional-dependencies"]

    assert extras["build"] == [
        "pyinstaller==6.21.0",
        "pyinstaller-hooks-contrib==2026.6",
    ]


def test_package_data_declares_required_non_python_assets():
    package_data = _read_pyproject()["tool"]["setuptools"]["package-data"]

    assert package_data["backend.core.model"] == ["model_metadata.toml"]
    assert package_data["backend.core.prompts"] == [
        "templates/*.md",
        "templates/agents/*.md",
    ]
    assert package_data["backend.workers"] == ["workflow_runtime.mjs"]


def test_required_assets_are_readable_as_package_resources():
    metadata = (
        resources.files("backend.core.model")
        .joinpath("model_metadata.toml")
        .read_text(encoding="utf-8")
    )
    core_prompt = (
        resources.files("backend.core.prompts")
        .joinpath("templates/core.md")
        .read_text(encoding="utf-8")
    )
    agent_templates = {
        name: (
            resources.files("backend.core.prompts")
            .joinpath(f"templates/agents/{name}.md")
            .read_text(encoding="utf-8")
        )
        for name in (
            "explorer",
            "implementer",
            "planner",
            "reviewer",
            "verifier",
            "workflow-worker",
        )
    }
    worker = (
        resources.files("backend.workers")
        .joinpath("workflow_runtime.mjs")
        .read_text(encoding="utf-8")
    )

    assert "[[rules]]" in metadata
    assert "You are ChatTree" in core_prompt
    assert all(f"name: {name}" in text for name, text in agent_templates.items())
    assert "workflow" in worker
