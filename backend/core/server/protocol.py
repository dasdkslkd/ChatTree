from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any


PROTOCOL_VERSION = 1
SERVER_VERSION = "0.1.18"
PROTOCOL_FEATURES = (
    "conversations",
    "runs",
    "sse_replay",
    "tools",
    "tool_approvals",
    "multi_agent",
    "error_envelope_v1",
    "idempotent_run_start_v1",
    "cooperative_shutdown_v1",
)


def runtime_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def provider_is_configured(config: Mapping[str, Any]) -> bool:
    provider_id = config.get("default_provider")
    providers = config.get("provider")
    if not isinstance(provider_id, str) or not provider_id:
        return False
    if not isinstance(providers, Mapping):
        return False
    provider = providers.get(provider_id)
    return isinstance(provider, Mapping) and provider.get("enabled") is True
