from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))?$"
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize every packaged ChatTree version from a release tag."
    )
    parser.add_argument("version", help="SemVer release version, with an optional v prefix")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when a version is out of sync",
    )
    args = parser.parse_args(argv)
    version = args.version.removeprefix("v")
    if not VERSION_PATTERN.fullmatch(version):
        parser.error("version must be X.Y.Z or X.Y.Z-(alpha|beta|rc).N")

    release_files = _release_files(version)
    changed = [
        path
        for path, content in release_files.items()
        if path.read_text(encoding="utf-8") != content
    ]
    if args.check:
        if changed:
            mismatches = ", ".join(str(path.relative_to(ROOT)) for path in changed)
            print(f"version mismatch: {mismatches}")
            return 1
        print(f"version {version} is synchronized")
        return 0

    for path, content in release_files.items():
        path.write_text(content, encoding="utf-8", newline="\n")
    print(f"set ChatTree version to {version}")
    return 0


def _release_files(version: str) -> dict[Path, str]:
    pyproject = ROOT / "pyproject.toml"
    protocol = ROOT / "backend" / "core" / "server" / "protocol.py"
    package = ROOT / "frontend" / "package.json"
    package_lock = ROOT / "frontend" / "package-lock.json"

    pyproject_text = _replace_one(
        pyproject.read_text(encoding="utf-8"),
        r'(?m)^version = "[^"]+"$',
        f'version = "{version}"',
        pyproject,
    )
    protocol_text = _replace_one(
        protocol.read_text(encoding="utf-8"),
        r'(?m)^SERVER_VERSION = "[^"]+"$',
        f'SERVER_VERSION = "{version}"',
        protocol,
    )
    package_document = json.loads(package.read_text(encoding="utf-8"))
    package_document["version"] = version
    package_lock_document = json.loads(package_lock.read_text(encoding="utf-8"))
    package_lock_document["version"] = version
    package_lock_document["packages"][""]["version"] = version

    return {
        pyproject: pyproject_text,
        protocol: protocol_text,
        package: json.dumps(package_document, ensure_ascii=False, indent=2) + "\n",
        package_lock: json.dumps(package_lock_document, ensure_ascii=False, indent=2) + "\n",
    }


def _replace_one(text: str, pattern: str, replacement: str, path: Path) -> str:
    updated, count = re.subn(pattern, replacement, text)
    if count != 1:
        raise RuntimeError(
            f"expected exactly one version declaration in {path.relative_to(ROOT)}, found {count}"
        )
    return updated


if __name__ == "__main__":
    raise SystemExit(main())
