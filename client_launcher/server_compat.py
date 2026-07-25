from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from backend.core.server import PROTOCOL_VERSION
from client_launcher.server_binary import (
    REQUIRED_SERVER_VERSION,
    ServerBinaryVersionError,
    ensure_supported_server_version,
    parse_chattree_server_version as _parse_chattree_server_version,
)


MIN_PROTOCOL_VERSION = PROTOCOL_VERSION
MAX_PROTOCOL_VERSION = PROTOCOL_VERSION
MIN_SERVER_VERSION = REQUIRED_SERVER_VERSION


@dataclass(frozen=True)
class VersionCheck:
    compatible: bool
    observed_version: str
    minimum_version: str


def parse_chattree_server_version(output: str) -> str | None:
    try:
        return _parse_chattree_server_version(output)
    except ServerBinaryVersionError:
        return None


def is_supported_server_version(value: str) -> bool:
    try:
        ensure_supported_server_version(value)
    except ServerBinaryVersionError:
        return False
    return True


def check_server_version(value: str) -> VersionCheck:
    return VersionCheck(
        compatible=is_supported_server_version(value),
        observed_version=value,
        minimum_version=MIN_SERVER_VERSION,
    )


def handshake_protocol_error(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("protocol_version")
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            "Server protocol version is incompatible: "
            f"supported {MIN_PROTOCOL_VERSION}-{MAX_PROTOCOL_VERSION}, got {value!r}"
        )
    if value < MIN_PROTOCOL_VERSION or value > MAX_PROTOCOL_VERSION:
        return (
            "Server protocol version is incompatible: "
            f"supported {MIN_PROTOCOL_VERSION}-{MAX_PROTOCOL_VERSION}, got {value}"
        )
    return None


def handshake_protocol_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "minimum_protocol_version": MIN_PROTOCOL_VERSION,
        "maximum_protocol_version": MAX_PROTOCOL_VERSION,
        "observed_protocol_version": payload.get("protocol_version"),
    }


def handshake_version_details(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "minimum_server_version": MIN_SERVER_VERSION,
        "observed_server_version": payload.get("server_version"),
    }
