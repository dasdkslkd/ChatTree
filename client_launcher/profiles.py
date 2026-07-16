from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

from backend.core.persistence.home import resolve_chattree_home
from backend.core.storage.atomic import atomic_write_json

from .models import LauncherError, LocalTarget, ServerProfile
from .settings import (
    DEFAULT_LOCAL_PROFILE_ID,
    DEFAULT_LOCAL_SERVER_PORT,
    PROFILES_FILENAME,
    PROFILES_SCHEMA_VERSION,
    resolve_client_home,
)


class ProfileStore:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        default_server_home: str | os.PathLike[str] | None = None,
        default_server_port: int = DEFAULT_LOCAL_SERVER_PORT,
    ) -> None:
        self.path = (
            Path(path).expanduser().resolve()
            if path is not None
            else resolve_client_home() / PROFILES_FILENAME
        )
        self._lock = threading.RLock()
        self._default_server_home = (
            Path(default_server_home).expanduser().resolve()
            if default_server_home is not None
            else resolve_chattree_home()
        )
        self._default_server_port = default_server_port
        self._profiles: tuple[ServerProfile, ...] = ()

        with self._lock:
            if self.path.exists():
                self._profiles = self._load()
            else:
                profiles = (self._default_profile(),)
                self._persist(profiles)
                self._profiles = profiles

    def list(self) -> tuple[ServerProfile, ...]:
        with self._lock:
            return self._profiles

    def get(self, profile_id: str) -> ServerProfile:
        with self._lock:
            return self._get(profile_id)

    def create(self, profile: ServerProfile) -> ServerProfile:
        if not isinstance(profile, ServerProfile):
            raise LauncherError(
                "profile_invalid",
                "profile must be a ServerProfile",
                False,
                422,
            )
        with self._lock:
            if any(existing.id == profile.id for existing in self._profiles):
                raise LauncherError(
                    "profile_id_duplicate",
                    f"Profile id is already in use: {profile.id}",
                    False,
                    409,
                )
            profiles = (*self._profiles, profile)
            self._validate_profiles(profiles)
            self._replace_profiles(profiles)
            return profile

    def update(
        self,
        profile_id: str,
        *,
        label: str | None = None,
        auto_connect: bool | None = None,
        local: LocalTarget | None = None,
    ) -> ServerProfile:
        with self._lock:
            current = self._get(profile_id)
            try:
                updated = ServerProfile(
                    id=current.id,
                    label=current.label if label is None else label,
                    kind=current.kind,
                    auto_connect=(
                        current.auto_connect
                        if auto_connect is None
                        else auto_connect
                    ),
                    bound_server_instance_id=current.bound_server_instance_id,
                    local=current.local if local is None else local,
                )
            except ValueError as exc:
                raise LauncherError(
                    "profile_invalid",
                    str(exc),
                    False,
                    422,
                ) from exc
            if updated == current:
                return current
            profiles = self._with_replacement(profile_id, updated)
            self._validate_profiles(profiles)
            self._replace_profiles(profiles)
            return updated

    def delete(self, profile_id: str) -> ServerProfile:
        with self._lock:
            profile = self._get(profile_id)
            if profile_id == DEFAULT_LOCAL_PROFILE_ID:
                raise LauncherError(
                    "default_profile_required",
                    "The default local profile cannot be deleted",
                    False,
                    409,
                )
            profiles = tuple(
                existing for existing in self._profiles if existing.id != profile_id
            )
            self._validate_profiles(profiles)
            self._replace_profiles(profiles)
            return profile

    def bind(self, profile_id: str, server_instance_id: str) -> ServerProfile:
        instance_id = self._validate_instance_id(server_instance_id)
        with self._lock:
            current = self._get(profile_id)
            if current.bound_server_instance_id == instance_id:
                return current
            if current.bound_server_instance_id is not None:
                raise LauncherError(
                    "server_identity_changed",
                    "The profile is already bound to a different Server instance",
                    False,
                    409,
                    details={
                        "bound_server_instance_id": (
                            current.bound_server_instance_id
                        ),
                        "observed_server_instance_id": instance_id,
                    },
                )
            return self._set_binding(current, instance_id)

    def rebind(self, profile_id: str, server_instance_id: str) -> ServerProfile:
        instance_id = self._validate_instance_id(server_instance_id)
        with self._lock:
            current = self._get(profile_id)
            if current.bound_server_instance_id == instance_id:
                return current
            return self._set_binding(current, instance_id)

    def _set_binding(
        self,
        current: ServerProfile,
        server_instance_id: str,
    ) -> ServerProfile:
        for profile in self._profiles:
            if (
                profile.id != current.id
                and profile.bound_server_instance_id == server_instance_id
            ):
                raise LauncherError(
                    "server_instance_already_bound",
                    "The Server instance is already bound to another profile",
                    False,
                    409,
                    details={
                        "existing_profile_id": profile.id,
                        "observed_server_instance_id": server_instance_id,
                    },
                )
        updated = ServerProfile(
            id=current.id,
            label=current.label,
            kind=current.kind,
            auto_connect=current.auto_connect,
            bound_server_instance_id=server_instance_id,
            local=current.local,
        )
        profiles = self._with_replacement(current.id, updated)
        self._validate_profiles(profiles)
        self._replace_profiles(profiles)
        return updated

    def _default_profile(self) -> ServerProfile:
        return ServerProfile(
            id=DEFAULT_LOCAL_PROFILE_ID,
            label="Local",
            kind="local",
            auto_connect=True,
            bound_server_instance_id=None,
            local=LocalTarget(
                server_home=str(self._default_server_home),
                server_port=self._default_server_port,
            ),
        )

    def _get(self, profile_id: str) -> ServerProfile:
        for profile in self._profiles:
            if profile.id == profile_id:
                return profile
        raise LauncherError(
            "profile_not_found",
            f"Profile not found: {profile_id}",
            False,
            404,
        )

    def _with_replacement(
        self,
        profile_id: str,
        replacement: ServerProfile,
    ) -> tuple[ServerProfile, ...]:
        return tuple(
            replacement if profile.id == profile_id else profile
            for profile in self._profiles
        )

    def _replace_profiles(self, profiles: Iterable[ServerProfile]) -> None:
        replacement = tuple(profiles)
        self._persist(replacement)
        self._profiles = replacement

    def _persist(self, profiles: tuple[ServerProfile, ...]) -> None:
        document = {
            "schema_version": PROFILES_SCHEMA_VERSION,
            "profiles": [profile.to_dict() for profile in profiles],
        }
        try:
            atomic_write_json(str(self.path), document, fsync=True)
        except OSError as exc:
            raise LauncherError(
                "profiles_write_failed",
                f"Could not write profiles file: {self.path}",
                True,
                500,
            ) from exc

    def _load(self) -> tuple[ServerProfile, ...]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            document = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LauncherError(
                "profiles_invalid",
                f"Could not read a valid profiles file: {self.path}",
                False,
                500,
            ) from exc
        if not isinstance(document, Mapping) or set(document) != {
            "schema_version",
            "profiles",
        }:
            raise LauncherError(
                "profiles_invalid",
                "Profiles file must contain schema_version and profiles",
                False,
                500,
            )
        version = document["schema_version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != PROFILES_SCHEMA_VERSION
        ):
            raise LauncherError(
                "profiles_version_unsupported",
                f"Unsupported profiles schema version: {version!r}",
                False,
                500,
            )
        values = document["profiles"]
        if not isinstance(values, list):
            raise LauncherError(
                "profiles_invalid",
                "profiles must be an array",
                False,
                500,
            )
        try:
            profiles = tuple(ServerProfile.from_dict(value) for value in values)
        except (KeyError, TypeError, ValueError) as exc:
            raise LauncherError(
                "profiles_invalid",
                f"Invalid profile: {exc}",
                False,
                500,
            ) from exc
        self._validate_profiles(profiles)
        return profiles

    @staticmethod
    def _validate_profiles(profiles: Iterable[ServerProfile]) -> None:
        profile_ids: set[str] = set()
        home_keys: set[str] = set()
        local_ports: set[int] = set()
        instance_ids: set[str] = set()
        has_default = False
        for profile in profiles:
            if profile.id in profile_ids:
                raise LauncherError(
                    "profile_id_duplicate",
                    f"Duplicate profile id: {profile.id}",
                    False,
                    500,
                )
            profile_ids.add(profile.id)
            has_default = has_default or profile.id == DEFAULT_LOCAL_PROFILE_ID

            home_key = os.path.normcase(os.path.normpath(profile.local.server_home))
            if home_key in home_keys:
                raise LauncherError(
                    "profile_home_duplicate",
                    f"Multiple profiles use Server home: {profile.local.server_home}",
                    False,
                    409,
                )
            home_keys.add(home_key)

            local_port = profile.local.server_port
            if local_port in local_ports:
                raise LauncherError(
                    "profile_port_duplicate",
                    f"Multiple profiles use local Server port: {local_port}",
                    False,
                    409,
                )
            local_ports.add(local_port)

            instance_id = profile.bound_server_instance_id
            if instance_id is not None:
                if instance_id in instance_ids:
                    raise LauncherError(
                        "server_instance_already_bound",
                        f"Server instance is bound more than once: {instance_id}",
                        False,
                        409,
                    )
                instance_ids.add(instance_id)
        if not has_default:
            raise LauncherError(
                "default_profile_required",
                "Profiles file does not contain the default local profile",
                False,
                500,
            )

    @staticmethod
    def _validate_instance_id(server_instance_id: str) -> str:
        if not isinstance(server_instance_id, str) or not server_instance_id.strip():
            raise LauncherError(
                "server_instance_id_invalid",
                "server_instance_id must be a non-empty string",
                False,
                422,
            )
        return server_instance_id
