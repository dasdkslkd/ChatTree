from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.home import resolve_chattree_home


def _truthy(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


@dataclass(frozen=True)
class PerfConfig:
    enabled: bool = False
    perf_run_id: str = ""
    output_dir: Path | None = None
    sample_rate: float = 1.0
    max_attr_length: int = 512
    max_batch_events: int = 500

    @property
    def backend_events_path(self) -> Path:
        if self.output_dir is None:
            return resolve_chattree_home() / "perf" / "disabled" / "backend-events.jsonl"
        return self.output_dir / "backend-events.jsonl"

    @property
    def frontend_events_path(self) -> Path:
        if self.output_dir is None:
            return resolve_chattree_home() / "perf" / "disabled" / "frontend-events.jsonl"
        return self.output_dir / "frontend-events.jsonl"

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "perf_run_id": self.perf_run_id,
            "output_dir": str(self.output_dir) if self.output_dir is not None else None,
            "sample_rate": self.sample_rate,
            "max_attr_length": self.max_attr_length,
            "max_batch_events": self.max_batch_events,
        }


def load_perf_config(config_data: dict[str, Any] | None = None) -> PerfConfig:
    data = config_data if isinstance(config_data, dict) else {}
    perf_data = data.get("performance") if isinstance(data.get("performance"), dict) else {}

    env_enabled = _truthy(os.environ.get("CHATTREE_PERF_ENABLED"))
    cfg_enabled = _truthy(perf_data.get("enabled"))
    enabled = bool(env_enabled if env_enabled is not None else cfg_enabled if cfg_enabled is not None else False)

    sample_rate_raw = os.environ.get("CHATTREE_PERF_SAMPLE_RATE", perf_data.get("sample_rate", 1.0))
    try:
        sample_rate = float(sample_rate_raw)
    except (TypeError, ValueError):
        sample_rate = 1.0
    sample_rate = min(1.0, max(0.0, sample_rate))

    max_attr_length_raw = perf_data.get("max_attr_length", 512)
    try:
        max_attr_length = max(64, min(4096, int(max_attr_length_raw)))
    except (TypeError, ValueError):
        max_attr_length = 512

    max_batch_events_raw = perf_data.get("max_batch_events", 500)
    try:
        max_batch_events = max(1, min(2000, int(max_batch_events_raw)))
    except (TypeError, ValueError):
        max_batch_events = 500

    perf_run_id = str(
        os.environ.get("CHATTREE_PERF_RUN_ID")
        or perf_data.get("run_id")
        or f"perf_{uuid.uuid4().hex[:12]}"
    )

    output_root = os.environ.get("CHATTREE_PERF_OUTPUT_DIR") or perf_data.get("output_dir")
    if output_root:
        output_dir = Path(str(output_root)).expanduser()
    else:
        output_dir = resolve_chattree_home() / "perf" / "runs" / perf_run_id

    return PerfConfig(
        enabled=enabled,
        perf_run_id=perf_run_id,
        output_dir=output_dir,
        sample_rate=sample_rate,
        max_attr_length=max_attr_length,
        max_batch_events=max_batch_events,
    )
