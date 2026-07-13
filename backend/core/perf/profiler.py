from __future__ import annotations

import contextvars
import random
import time
import uuid
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any

from .config import PerfConfig, load_perf_config
from .sink import JsonlSink


_ATTR_STACK: contextvars.ContextVar[tuple[dict[str, Any], ...]] = contextvars.ContextVar(
    "chattree_perf_attr_stack",
    default=(),
)
_PROFILER: "BaseProfiler" | None = None


class BaseProfiler:
    config: PerfConfig
    enabled: bool

    def span(self, name: str, **attrs: Any) -> AbstractContextManager[None]:
        raise NotImplementedError

    def mark(self, name: str, **attrs: Any) -> None:
        raise NotImplementedError

    def record(self, event: dict[str, Any]) -> None:
        raise NotImplementedError

    def record_frontend_events(self, events: list[dict[str, Any]]) -> int:
        raise NotImplementedError


class _NoopSpan(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


class NoopProfiler(BaseProfiler):
    def __init__(self, config: PerfConfig | None = None) -> None:
        self.config = config or load_perf_config({})
        self.enabled = False

    def span(self, name: str, **attrs: Any) -> AbstractContextManager[None]:
        return _NoopSpan()

    def mark(self, name: str, **attrs: Any) -> None:
        return None

    def record(self, event: dict[str, Any]) -> None:
        return None

    def record_frontend_events(self, events: list[dict[str, Any]]) -> int:
        return 0


class _PerfSpan(AbstractContextManager[None]):
    def __init__(self, profiler: "PerfProfiler", name: str, attrs: dict[str, Any]) -> None:
        self.profiler = profiler
        self.name = name
        self.attrs = attrs
        self.started = 0.0
        self.token: contextvars.Token[tuple[dict[str, Any], ...]] | None = None

    def __enter__(self) -> None:
        self.started = time.perf_counter()
        stack = _ATTR_STACK.get()
        merged = dict(stack[-1]) if stack else {}
        merged.update(self.attrs)
        self.token = _ATTR_STACK.set((*stack, merged))
        self.profiler.mark(f"{self.name}.start", **self.attrs)
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        duration_ms = (time.perf_counter() - self.started) * 1000.0
        if self.token is not None:
            _ATTR_STACK.reset(self.token)
        attrs = dict(self.attrs)
        if exc_type is not None:
            attrs["error_type"] = exc_type.__name__
        self.profiler.record({
            "type": "span",
            "name": self.name,
            "duration_ms": duration_ms,
            "attrs": attrs,
        })
        return False


class PerfProfiler(BaseProfiler):
    def __init__(self, config: PerfConfig) -> None:
        self.config = config
        self.enabled = config.enabled
        self.backend_sink = JsonlSink(config.backend_events_path)
        self.frontend_sink = JsonlSink(config.frontend_events_path)

    def span(self, name: str, **attrs: Any) -> AbstractContextManager[None]:
        if not self._should_sample():
            return _NoopSpan()
        return _PerfSpan(self, name, attrs)

    def mark(self, name: str, **attrs: Any) -> None:
        if not self._should_sample():
            return
        self.record({
            "type": "mark",
            "name": name,
            "attrs": attrs,
        })

    def record(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = self._base_event("backend")
        payload.update(self._sanitize_event(event))
        context_attrs: dict[str, Any] = {}
        stack = _ATTR_STACK.get()
        if stack:
            context_attrs.update(stack[-1])
        attrs = payload.get("attrs")
        if isinstance(attrs, dict):
            context_attrs.update(attrs)
        if context_attrs:
            payload["attrs"] = self._sanitize_attrs(context_attrs)
        self.backend_sink.write(payload)

    def record_frontend_events(self, events: list[dict[str, Any]]) -> int:
        if not self.enabled:
            return 0
        written = 0
        for event in events[: self.config.max_batch_events]:
            payload = self._base_event("frontend")
            payload.update(self._sanitize_event(event))
            payload["attrs"] = self._sanitize_attrs(payload.get("attrs") or {})
            self.frontend_sink.write(payload)
            written += 1
        return written

    def _base_event(self, source: str) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "source": source,
            "perf_run_id": self.config.perf_run_id,
            "ts": time.time(),
            "ts_ns": time.time_ns(),
        }

    def _should_sample(self) -> bool:
        if not self.enabled:
            return False
        if self.config.sample_rate >= 1.0:
            return True
        return random.random() <= self.config.sample_rate

    def _sanitize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in dict(event).items():
            if key == "attrs" and isinstance(value, dict):
                clean[key] = self._sanitize_attrs(value)
            elif key in {"type", "name"}:
                clean[key] = str(value)
            elif key in {"duration_ms", "run_id", "conversation_id", "node_id", "client_run_id"}:
                clean[key] = self._sanitize_value(value)
        return clean

    def _sanitize_attrs(self, attrs: dict[str, Any]) -> dict[str, Any]:
        return {
            str(key)[:80]: self._sanitize_value(value)
            for key, value in dict(attrs).items()
            if value is not None
        }

    def _sanitize_value(self, value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return value
        text = str(value)
        if len(text) <= self.config.max_attr_length:
            return text
        return text[: self.config.max_attr_length] + f"...[len={len(text)}]"


def configure_profiler(config: PerfConfig) -> BaseProfiler:
    global _PROFILER
    _PROFILER = PerfProfiler(config) if config.enabled else NoopProfiler(config)
    return _PROFILER


def get_profiler() -> BaseProfiler:
    global _PROFILER
    if _PROFILER is None:
        _PROFILER = configure_profiler(load_perf_config({}))
    return _PROFILER
