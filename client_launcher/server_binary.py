from __future__ import annotations

import re

from backend.core.server import SERVER_VERSION


REQUIRED_SERVER_VERSION = SERVER_VERSION
_VERSION_RE = re.compile(
    r"^chattree-server\s+([0-9]+(?:\.[0-9]+){2}(?:[-+][^\s]+)?)$"
)


class ServerBinaryVersionError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        observed_version: str | None = None,
        required_version: str = REQUIRED_SERVER_VERSION,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.observed_version = observed_version
        self.required_version = required_version


def parse_chattree_server_version(output: str) -> str:
    for line in output.splitlines():
        match = _VERSION_RE.match(line.strip())
        if match:
            return match.group(1)
    raise ServerBinaryVersionError(
        "server_version_invalid",
        "chattree-server --version output is invalid",
    )


def ensure_supported_server_version(version: str) -> None:
    if version != REQUIRED_SERVER_VERSION:
        raise ServerBinaryVersionError(
            "server_version_incompatible",
            (
                "ChatTree Server version is incompatible: "
                f"required {REQUIRED_SERVER_VERSION}, got {version}"
            ),
            observed_version=version,
        )
