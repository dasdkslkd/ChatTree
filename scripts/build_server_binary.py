from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import venv
import zipfile
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "packaging" / "chattree-server.spec"
DEFAULT_BUILD_ROOT = REPO_ROOT / ".build" / "server-binary"
DEFAULT_DIST_DIR = REPO_ROOT / "dist"
PYINSTALLER_VERSION = "6.21.0"
PYINSTALLER_HOOKS_CONTRIB_VERSION = "2026.6"
RIPGREP_VERSION = "15.1.0"
RIPGREP_ASSETS = {
    ("win32", "x64"): (
        "x86_64-pc-windows-msvc.zip",
        "124510b94b6baa3380d051fdf4650eaa80a302c876d611e9dba0b2e18d87493a",
    ),
    ("darwin", "arm64"): (
        "aarch64-apple-darwin.tar.gz",
        "378e973289176ca0c6054054ee7f631a065874a352bf43f0fa60ef079b6ba715",
    ),
    ("darwin", "x64"): (
        "x86_64-apple-darwin.tar.gz",
        "64811cb24e77cac3057d6c40b63ac9becf9082eedd54ca411b475b755d334882",
    ),
    ("linux", "x64"): (
        "x86_64-unknown-linux-musl.tar.gz",
        "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599",
    ),
}


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
    ripgrep_binary = prepare_bundled_ripgrep(build_root)
    run_pyinstaller(
        python,
        dist_dir=dist_dir,
        work_dir=work_dir,
        clean=args.clean,
        one_dir=args.one_dir,
        ripgrep_binary=ripgrep_binary,
    )

    binary = binary_path(dist_dir, one_dir=args.one_dir)
    if not binary.exists():
        raise SystemExit(f"missing binary: {binary}")
    version = run_checked([str(binary), "--version"], cwd=REPO_ROOT)
    print(version.strip())
    if args.smoke:
        smoke_start(binary)
    print(f"built {binary}")
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the distributable chattree-server binary."
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove previous output for this server binary build",
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
        help="directory for built server artifacts",
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
        help="start the built server on a temporary home and verify /handshake",
    )
    return parser.parse_args(argv)


def ensure_build_venv(venv_dir: Path) -> None:
    python = venv_python(venv_dir)
    if python.exists() and (venv_dir / "pyvenv.cfg").exists():
        completed = subprocess.run(
            [str(python), "-m", "pip", "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            return
        _remove_path(venv_dir)
    if not python.exists() or not (venv_dir / "pyvenv.cfg").exists():
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)


def install_build_dependencies(python: Path) -> None:
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "wheel",
            f"pyinstaller=={PYINSTALLER_VERSION}",
            f"pyinstaller-hooks-contrib=={PYINSTALLER_HOOKS_CONTRIB_VERSION}",
        ],
        cwd=str(REPO_ROOT),
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            str(REPO_ROOT),
        ],
        cwd=str(REPO_ROOT),
        check=True,
    )


def prepare_bundled_ripgrep(build_root: Path) -> Path:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    asset = RIPGREP_ASSETS.get((sys.platform, arch))
    if asset is None:
        raise SystemExit(f"unsupported ripgrep build platform: {sys.platform}-{arch}")
    asset_suffix, expected_sha256 = asset
    asset_name = f"ripgrep-{RIPGREP_VERSION}-{asset_suffix}"
    cache_dir = build_root / "ripgrep"
    archive_path = cache_dir / asset_name
    executable_name = "rg.exe" if sys.platform == "win32" else "rg"
    binary_path = cache_dir / executable_name
    cache_dir.mkdir(parents=True, exist_ok=True)

    if not archive_path.is_file() or _sha256(archive_path) != expected_sha256:
        archive_path.unlink(missing_ok=True)
        request = urllib.request.Request(
            f"https://github.com/BurntSushi/ripgrep/releases/download/{RIPGREP_VERSION}/{asset_name}",
            headers={"User-Agent": "ChatTree-build"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
    actual_sha256 = _sha256(archive_path)
    if actual_sha256 != expected_sha256:
        raise SystemExit(
            f"ripgrep archive checksum mismatch: expected {expected_sha256}, got {actual_sha256}"
        )

    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as archive:
            member = next(
                name
                for name in archive.namelist()
                if Path(name).name == executable_name
            )
            with archive.open(member) as source, binary_path.open("wb") as output:
                shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive_path, "r:gz") as archive:
            member = next(
                item
                for item in archive.getmembers()
                if item.isfile() and Path(item.name).name == executable_name
            )
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit(f"ripgrep archive member is unreadable: {member.name}")
            with source, binary_path.open("wb") as output:
                shutil.copyfileobj(source, output)
    if sys.platform != "win32":
        binary_path.chmod(
            binary_path.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
    return binary_path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_pyinstaller(
    python: Path,
    *,
    dist_dir: Path,
    work_dir: Path,
    clean: bool,
    one_dir: bool,
    ripgrep_binary: Path,
) -> None:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["CHATTREE_REPO_ROOT"] = str(REPO_ROOT)
    env["CHATTREE_PYINSTALLER_ONE_DIR"] = "1" if one_dir else "0"
    env["CHATTREE_BUNDLED_RIPGREP"] = str(ripgrep_binary)
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


def run_checked(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"command failed ({completed.returncode}): {' '.join(argv)}\n"
            f"{completed.stdout}"
        )
    return completed.stdout


def smoke_start(binary: Path) -> None:
    home = Path(tempfile.mkdtemp(prefix="chattree-server-smoke-"))
    pid: int | None = None
    try:
        output = run_checked(
            [
                str(binary),
                "start",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--home",
                str(home),
            ],
            cwd=REPO_ROOT,
        )
        payload = _json_from_tail(output)
        port = payload.get("port")
        if isinstance(payload.get("pid"), int):
            pid = int(payload["pid"])
        if not isinstance(port, int):
            raise SystemExit(f"start did not return a port:\n{output}")
        server_instance_id = _wait_for_handshake(port)
        _request_shutdown(port, server_instance_id)
        _wait_for_process_exit(pid, timeout_seconds=10)
        pid = None
    finally:
        if pid is not None:
            _terminate_process(pid)
        shutil.rmtree(home, ignore_errors=True)


def binary_path(dist_dir: Path, *, one_dir: bool) -> Path:
    executable_name = "chattree-server.exe" if os.name == "nt" else "chattree-server"
    if one_dir:
        return dist_dir / "chattree-server" / executable_name
    return dist_dir / executable_name


def artifact_path(dist_dir: Path, *, one_dir: bool) -> Path:
    if one_dir:
        return dist_dir / "chattree-server"
    return binary_path(dist_dir, one_dir=False)


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _json_from_tail(output: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            return value
    raise SystemExit(f"command did not return JSON:\n{output}")


def _wait_for_handshake(port: int) -> str:
    deadline = time.monotonic() + 30
    last_error: BaseException | None = None
    url = f"http://127.0.0.1:{port}/api/v1/handshake"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    payload = json.loads(response.read().decode("utf-8"))
                    server_instance_id = payload.get("server_instance_id")
                    if isinstance(server_instance_id, str):
                        return server_instance_id
        except BaseException as exc:
            last_error = exc
        time.sleep(0.1)
    raise SystemExit(f"server binary smoke timed out: {last_error}")


def _request_shutdown(port: int, server_instance_id: str) -> None:
    body = json.dumps(
        {"expected_server_instance_id": server_instance_id},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/server/shutdown",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 202:
            raise SystemExit(f"server binary smoke shutdown returned {response.status}")


def _terminate_process(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _wait_for_process_exit(pid: int | None, *, timeout_seconds: float) -> None:
    if pid is None:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.1)
    raise SystemExit(f"server binary smoke process did not exit: pid={pid}")


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_path(path: Path) -> None:
    resolved = path.resolve()
    repo_root = REPO_ROOT.resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(f"refusing to remove path outside repo: {resolved}") from exc
    if not resolved.exists():
        return
    if resolved.is_dir():
        shutil.rmtree(resolved, ignore_errors=True)
    else:
        try:
            resolved.unlink()
        except FileNotFoundError:
            return


if __name__ == "__main__":
    raise SystemExit(main())
