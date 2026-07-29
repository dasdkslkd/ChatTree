from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Sequence

from build_server_binary import (
    REPO_ROOT,
    ensure_build_venv,
    install_build_dependencies,
    _remove_path,
    run_checked,
    venv_python,
)


SPEC_PATH = REPO_ROOT / "packaging" / "chattree-launcher.spec"
DEFAULT_BUILD_ROOT = REPO_ROOT / ".build" / "launcher-binary"
DEFAULT_DIST_DIR = REPO_ROOT / "dist"
LAUNCHER_READY_PREFIX = "CHATTREE_LAUNCHER_READY "


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    build_root = args.build_root.expanduser().resolve()
    dist_dir = args.dist_dir.expanduser().resolve()
    venv_dir = build_root / "venv"
    work_dir = build_root / "work"
    artifact = artifact_path(dist_dir, one_dir=args.one_dir)

    if args.clean:
        _remove_path(work_dir)
        _remove_path(artifact)

    ensure_build_venv(venv_dir)
    python = venv_python(venv_dir)
    if not args.skip_install:
        install_build_dependencies(python)
    run_pyinstaller(
        python,
        dist_dir=dist_dir,
        work_dir=work_dir,
        clean=args.clean,
        one_dir=args.one_dir,
    )

    binary = binary_path(dist_dir, one_dir=args.one_dir)
    if not binary.exists():
        raise SystemExit(f"missing binary: {binary}")
    if args.smoke:
        smoke_start(binary)
    print(f"built {binary}")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the distributable chattree-launcher binary."
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove previous output for this launcher binary build",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="reuse the isolated build venv without installing dependencies",
    )
    parser.add_argument(
        "--one-dir",
        action="store_true",
        help="build an onedir artifact instead of the default single executable",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=DEFAULT_DIST_DIR,
        help="directory for built launcher artifacts",
    )
    parser.add_argument(
        "--build-root",
        type=Path,
        default=DEFAULT_BUILD_ROOT,
        help="directory for the isolated build venv and PyInstaller work files",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="start the built launcher on a temporary home and verify status endpoint",
    )
    return parser.parse_args(argv)


def run_pyinstaller(
    python: Path,
    *,
    dist_dir: Path,
    work_dir: Path,
    clean: bool,
    one_dir: bool,
) -> None:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["CHATTREE_REPO_ROOT"] = str(REPO_ROOT)
    env["CHATTREE_PYINSTALLER_ONE_DIR"] = "1" if one_dir else "0"
    command = [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
    ]
    if clean:
        command.append("--clean")
    command.append(str(SPEC_PATH))
    subprocess.run(command, cwd=str(REPO_ROOT), env=env, check=True)


def smoke_start(binary: Path) -> None:
    home = Path(tempfile.mkdtemp(prefix="chattree-launcher-smoke-"))
    process: subprocess.Popen | None = None
    try:
        env = os.environ.copy()
        env["CHATTREE_CLIENT_HOME"] = str(home)
        env["CHATTREE_CLIENT_PORT"] = "0"
        env["CHATTREE_SERVER_BINARY"] = ""
        process = subprocess.Popen(
            [str(binary)],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _wait_for_status(_wait_for_ready_port(process))
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        shutil.rmtree(home, ignore_errors=True)


def _wait_for_ready_port(process: subprocess.Popen[str]) -> int:
    if process.stdout is None:
        raise SystemExit("launcher binary smoke has no stdout")
    lines: queue.Queue[str] = queue.Queue()

    def read_lines() -> None:
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=read_lines, daemon=True).start()
    deadline = time.monotonic() + 30
    captured: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.2).strip()
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        captured.append(line)
        if not line.startswith(LAUNCHER_READY_PREFIX):
            continue
        try:
            ready = json.loads(line.removeprefix(LAUNCHER_READY_PREFIX))
            host = ready["host"]
            port = ready["port"]
        except (KeyError, TypeError, ValueError):
            break
        if host == "127.0.0.1" and isinstance(port, int) and 1 <= port <= 65535:
            return port
        break
    tail = "\n".join(captured[-20:])
    raise SystemExit(f"launcher binary did not report a valid ready endpoint:\n{tail}")


def _wait_for_status(port: int) -> None:
    deadline = time.monotonic() + 30
    url = f"http://127.0.0.1:{port}/client/v1/profiles/local/status"
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("profile_id") == "local":
                        return
        except BaseException as exc:
            last_error = exc
        time.sleep(0.2)
    raise SystemExit(f"launcher binary smoke timed out: {last_error}")


def binary_path(dist_dir: Path, *, one_dir: bool) -> Path:
    executable_name = "chattree-launcher.exe" if os.name == "nt" else "chattree-launcher"
    if one_dir:
        return dist_dir / "chattree-launcher" / executable_name
    return dist_dir / executable_name


def artifact_path(dist_dir: Path, *, one_dir: bool) -> Path:
    if one_dir:
        return dist_dir / "chattree-launcher"
    return binary_path(dist_dir, one_dir=False)


if __name__ == "__main__":
    raise SystemExit(main())
