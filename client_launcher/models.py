from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping


ProfileKind = Literal["local"]
ConnectionStatus = Literal["disconnected", "connecting", "ready", "error"]


class LauncherError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


def _require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    model_name: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{model_name} fields must be {sorted(expected)}; got {sorted(actual)}"
        )


def _normalized_home(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value)
    if not raw.strip():
        raise ValueError("server_home must not be empty")
    return str(Path(raw).expanduser().resolve())


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class LocalTarget:
    server_home: str
    server_port: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_home", _normalized_home(self.server_home))
        if (
            isinstance(self.server_port, bool)
            or not isinstance(self.server_port, int)
            or not 1 <= self.server_port <= 65535
        ):
            raise ValueError("server_port must be an integer from 1 to 65535")

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_home": self.server_home,
            "server_port": self.server_port,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LocalTarget:
        if not isinstance(value, Mapping):
            raise ValueError("local target must be an object")
        _require_exact_keys(value, {"server_home", "server_port"}, "local target")
        server_home = value["server_home"]
        if not isinstance(server_home, str):
            raise ValueError("server_home must be a string")
        return cls(
            server_home=server_home,
            server_port=value["server_port"],
        )


@dataclass(frozen=True)
class ServerProfile:
    id: str
    label: str
    kind: ProfileKind
    auto_connect: bool
    bound_server_instance_id: str | None
    local: LocalTarget

    def __post_init__(self) -> None:
        _required_string(self.id, "profile id")
        _required_string(self.label, "profile label")
        if self.kind != "local":
            raise ValueError("profile kind must be 'local'")
        if not isinstance(self.auto_connect, bool):
            raise ValueError("auto_connect must be a boolean")
        if self.bound_server_instance_id is not None:
            _required_string(
                self.bound_server_instance_id,
                "bound_server_instance_id",
            )
        if not isinstance(self.local, LocalTarget):
            raise ValueError("local must be a LocalTarget")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "auto_connect": self.auto_connect,
            "bound_server_instance_id": self.bound_server_instance_id,
            "local": self.local.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ServerProfile:
        if not isinstance(value, Mapping):
            raise ValueError("profile must be an object")
        _require_exact_keys(
            value,
            {
                "id",
                "label",
                "kind",
                "auto_connect",
                "bound_server_instance_id",
                "local",
            },
            "profile",
        )
        return cls(
            id=value["id"],
            label=value["label"],
            kind=value["kind"],
            auto_connect=value["auto_connect"],
            bound_server_instance_id=value["bound_server_instance_id"],
            local=LocalTarget.from_dict(value["local"]),
        )


@dataclass(frozen=True)
class ConnectionErrorInfo:
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        _required_string(self.code, "error code")
        _required_string(self.message, "error message")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass
class ServerSession:
    profile_id: str
    status: ConnectionStatus = "disconnected"
    phase: str | None = None
    connection_epoch: int = 0
    server_instance_id: str | None = None
    error: ConnectionErrorInfo | None = None

    def __post_init__(self) -> None:
        _required_string(self.profile_id, "profile_id")
        if self.status not in {"disconnected", "connecting", "ready", "error"}:
            raise ValueError("invalid connection status")
        if self.phase is not None:
            _required_string(self.phase, "connection phase")
        if (
            isinstance(self.connection_epoch, bool)
            or not isinstance(self.connection_epoch, int)
            or self.connection_epoch < 0
        ):
            raise ValueError("connection_epoch must be a non-negative integer")
        if self.server_instance_id is not None:
            _required_string(self.server_instance_id, "server_instance_id")
        if self.error is not None and not isinstance(self.error, ConnectionErrorInfo):
            raise ValueError("error must be ConnectionErrorInfo or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "phase": self.phase,
            "connection_epoch": self.connection_epoch,
            "server_instance_id": self.server_instance_id,
            "error": self.error.to_dict() if self.error is not None else None,
        }
