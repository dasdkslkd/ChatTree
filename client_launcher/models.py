from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping
from uuid import UUID, uuid4


ProfileKind = Literal["local", "ssh"]
ConnectionStatus = Literal["disconnected", "connecting", "ready", "error"]


class LauncherError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        status_code: int = 400,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.details = dict(details or {})


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


def _required_canonical_uuid(value: Any, field_name: str) -> str:
    normalized = _required_string(value, field_name)
    try:
        canonical = str(UUID(normalized))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    if canonical != normalized:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return normalized


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
class SshTarget:
    config_host: str
    remote_server_port: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "config_host",
            _required_string(self.config_host, "config_host"),
        )
        if (
            isinstance(self.remote_server_port, bool)
            or not isinstance(self.remote_server_port, int)
            or not 0 <= self.remote_server_port <= 65535
        ):
            raise ValueError("remote_server_port must be an integer from 0 to 65535")

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_host": self.config_host,
            "remote_server_port": self.remote_server_port,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SshTarget:
        if not isinstance(value, Mapping):
            raise ValueError("ssh target must be an object")
        _require_exact_keys(
            value,
            {"config_host", "remote_server_port"},
            "ssh target",
        )
        return cls(
            config_host=value["config_host"],
            remote_server_port=value["remote_server_port"],
        )


def ssh_profile_id(config_host: str) -> str:
    host = _required_string(config_host, "config_host")
    encoded = base64.urlsafe_b64encode(host.encode("utf-8")).decode("ascii")
    return f"ssh:{encoded.rstrip('=')}"


@dataclass(frozen=True)
class ServerProfile:
    id: str
    label: str
    kind: ProfileKind
    auto_connect: bool
    bound_server_instance_id: str | None
    local: LocalTarget | None = None
    ssh: SshTarget | None = None

    def __post_init__(self) -> None:
        _required_string(self.id, "profile id")
        _required_string(self.label, "profile label")
        if self.kind not in {"local", "ssh"}:
            raise ValueError("profile kind must be 'local' or 'ssh'")
        if not isinstance(self.auto_connect, bool):
            raise ValueError("auto_connect must be a boolean")
        if self.bound_server_instance_id is not None:
            _required_string(
                self.bound_server_instance_id,
                "bound_server_instance_id",
            )
        if self.kind == "local":
            if not isinstance(self.local, LocalTarget) or self.ssh is not None:
                raise ValueError("local profile must have local and no ssh target")
        elif not isinstance(self.ssh, SshTarget) or self.local is not None:
            raise ValueError("ssh profile must have ssh and no local target")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "auto_connect": self.auto_connect,
            "bound_server_instance_id": self.bound_server_instance_id,
        }
        if self.kind == "local":
            assert self.local is not None
            payload["local"] = self.local.to_dict()
        else:
            assert self.ssh is not None
            payload["ssh"] = self.ssh.to_dict()
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ServerProfile:
        if not isinstance(value, Mapping):
            raise ValueError("profile must be an object")
        common = {"id", "label", "kind", "auto_connect", "bound_server_instance_id"}
        kind = value.get("kind")
        if kind == "local":
            _require_exact_keys(value, {*common, "local"}, "profile")
            local = LocalTarget.from_dict(value["local"])
            ssh = None
        elif kind == "ssh":
            _require_exact_keys(value, {*common, "ssh"}, "profile")
            local = None
            ssh = SshTarget.from_dict(value["ssh"])
        else:
            raise ValueError("profile kind must be 'local' or 'ssh'")
        return cls(
            id=value["id"],
            label=value["label"],
            kind=kind,
            auto_connect=value["auto_connect"],
            bound_server_instance_id=value["bound_server_instance_id"],
            local=local,
            ssh=ssh,
        )


@dataclass(frozen=True)
class ConnectionErrorInfo:
    code: str
    message: str
    retryable: bool
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _required_string(self.code, "error code")
        _required_string(self.message, "error message")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable must be a boolean")
        if self.details is not None:
            if not isinstance(self.details, Mapping):
                raise ValueError("error details must be an object")
            object.__setattr__(self, "details", dict(self.details))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True)
class EndpointLease:
    endpoint: str
    profile_id: str
    server_instance_id: str
    connection_epoch: int
    connection_lease_id: str
    invalidated: asyncio.Event

    def __post_init__(self) -> None:
        _required_string(self.endpoint, "endpoint")
        _required_string(self.profile_id, "profile_id")
        _required_string(self.server_instance_id, "server_instance_id")
        if (
            isinstance(self.connection_epoch, bool)
            or not isinstance(self.connection_epoch, int)
            or self.connection_epoch <= 0
        ):
            raise ValueError("connection_epoch must be a positive integer")
        _required_canonical_uuid(
            self.connection_lease_id,
            "connection_lease_id",
        )
        if not isinstance(self.invalidated, asyncio.Event):
            raise ValueError("invalidated must be an asyncio.Event")


@dataclass
class ServerSession:
    profile_id: str
    status: ConnectionStatus = "disconnected"
    phase: str | None = None
    connection_epoch: int = 0
    connection_lease_id: str = field(default_factory=lambda: str(uuid4()))
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
        _required_canonical_uuid(
            self.connection_lease_id,
            "connection_lease_id",
        )
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
            "connection_lease_id": self.connection_lease_id,
            "server_instance_id": self.server_instance_id,
            "error": self.error.to_dict() if self.error is not None else None,
        }
