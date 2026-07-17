from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


PRIVATE_TASK_METADATA_KEYS = {
    "task_generation_id",
    "task_revision",
    "task_step_position",
}


def public_run_dict(run: Dict[str, Any]) -> Dict[str, Any]:
    data = deepcopy(dict(run))
    for private_key in ("id", "idempotency_key", "request_fingerprint"):
        data.pop(private_key, None)
    metadata = dict(data.get("metadata") or {})
    step = metadata.pop("task_step_position", None)
    for key in PRIVATE_TASK_METADATA_KEYS:
        metadata.pop(key, None)
    data["metadata"] = metadata
    if step is not None:
        data["step"] = step
    return data


def public_run_event(event: Dict[str, Any]) -> Dict[str, Any]:
    data = deepcopy(dict(event))
    for key, value in list(data.items()):
        if isinstance(value, dict):
            data[key] = public_run_event(value)
        elif isinstance(value, list):
            data[key] = [
                public_run_event(item) if isinstance(item, dict) else deepcopy(item)
                for item in value
            ]
    if isinstance(data.get("metadata"), dict):
        data = public_run_dict(data)
    return data
