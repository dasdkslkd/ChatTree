from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from client_launcher.models import LauncherError


@dataclass(frozen=True)
class SshConfigSnapshot:
    path: str
    text: str
    hosts: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "text": self.text,
            "hosts": list(self.hosts),
            "warnings": list(self.warnings),
        }


def default_ssh_config_path() -> Path:
    return (Path.home() / ".ssh" / "config").resolve()


def parse_ssh_config_hosts(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    hosts: list[str] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if parts[0].lower() != "host":
            continue
        if len(parts) == 1:
            warnings.append(f"Line {line_number}: Host has no alias")
            continue
        for alias in parts[1:]:
            if any(marker in alias for marker in ("*", "?", "!")):
                continue
            if alias not in seen:
                seen.add(alias)
                hosts.append(alias)
    return tuple(hosts), tuple(warnings)


class SshConfigStore:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        parser: Callable[[str], tuple[tuple[str, ...], tuple[str, ...]]] = (
            parse_ssh_config_hosts
        ),
    ) -> None:
        self.path = (
            Path(path).expanduser().resolve()
            if path is not None
            else default_ssh_config_path()
        )
        self._parser = parser

    def read(self) -> SshConfigSnapshot:
        if not self.path.exists():
            return SshConfigSnapshot(str(self.path), "", ())
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise LauncherError(
                "ssh_config_read_failed",
                f"Could not read SSH config: {self.path}",
                True,
                500,
            ) from exc
        try:
            hosts, warnings = self._parser(text)
        except ValueError as exc:
            hosts = ()
            warnings = (str(exc),)
        return SshConfigSnapshot(str(self.path), text, hosts, warnings)

    def write(self, text: str) -> SshConfigSnapshot:
        if not isinstance(text, str):
            raise LauncherError(
                "ssh_config_invalid",
                "SSH config text must be a string",
                False,
                422,
            )
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LauncherError(
                "ssh_config_write_failed",
                f"Could not create SSH config directory: {parent}",
                True,
                500,
            ) from exc

        tmp = parent / f"{self.path.name}.tmp.{uuid.uuid4().hex}"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(str(tmp), flags, stat.S_IRUSR | stat.S_IWUSR)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise LauncherError(
                "ssh_config_write_failed",
                f"Could not write SSH config: {self.path}",
                True,
                500,
            ) from exc
        return self.read()
