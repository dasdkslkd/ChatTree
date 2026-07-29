from __future__ import annotations

import json
from pathlib import Path

import scripts.set_release_version as set_release_version
from backend.core.server import SERVER_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_packaged_versions_are_synchronized():
    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    package_lock = json.loads(
        (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    )

    assert package["version"] == SERVER_VERSION
    assert package_lock["version"] == SERVER_VERSION
    assert package_lock["packages"][""]["version"] == SERVER_VERSION
    assert set_release_version.main([SERVER_VERSION, "--check"]) == 0
