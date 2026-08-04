# model/providers/retry.py - Provider retry policy and error classification
import asyncio
import random
import socket
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Optional, TypeVar

import httpx


RETRYABLE_HTTP_STATUSES = {408, 409, 429, 500, 502, 503, 504, 529}


class RetryableHTTPError(RuntimeError):
    def __init__(
        self,
        status: int,
        body: str,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.status = status
        self.body = body
        self.headers = headers or {}
        super().__init__(f"HTTP {status}: {body}")


@dataclass(frozen=True)
class RetryPolicy:
    max_request_retries: int
    max_stream_retries: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_fraction: float

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "RetryPolicy":
        transport = config["model_transport"]
        return cls(
            max_request_retries=int(transport["max_request_retries"]),
            max_stream_retries=int(transport["max_stream_retries"]),
            base_delay_seconds=float(transport["retry_base_delay_seconds"]),
            max_delay_seconds=float(transport["retry_max_delay_seconds"]),
            jitter_fraction=float(transport["retry_jitter_fraction"]),
        )

    def max_retries(self, *, stream: bool = False) -> int:
        return self.max_stream_retries if stream else self.max_request_retries


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    category: str
    status: Optional[int] = None
    retry_after_seconds: Optional[float] = None


T = TypeVar("T")


def classify_retry_error(error: BaseException, policy: RetryPolicy) -> RetryDecision:
    status = getattr(error, "status", None)
    if isinstance(status, int):
        retry_after = _retry_after_seconds(getattr(error, "headers", None))
        if status in RETRYABLE_HTTP_STATUSES:
            if status == 429 or status == 529:
                category = "rate_limit"
            else:
                category = "server_error"
            return RetryDecision(True, category, status, retry_after)
        if status >= 500:
            return RetryDecision(True, "server_error", status, retry_after)
        if status in {401, 403}:
            return RetryDecision(False, "authentication_failed", status, retry_after)
        return RetryDecision(False, "permanent_http_error", status, retry_after)

    if isinstance(error, (TimeoutError, socket.timeout)):
        return RetryDecision(True, "timeout")
    if isinstance(error, urllib.error.URLError):
        return RetryDecision(True, "network")
    if isinstance(error, (ConnectionError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return RetryDecision(True, "network")
    if isinstance(error, httpx.TimeoutException):
        return RetryDecision(True, "timeout")
    if isinstance(error, httpx.RequestError):
        return RetryDecision(True, "network")

    return RetryDecision(False, "unknown")


def retry_delay_seconds(
    attempt: int,
    policy: RetryPolicy,
    decision: Optional[RetryDecision] = None,
) -> float:
    if decision and decision.retry_after_seconds is not None:
        base = decision.retry_after_seconds
    else:
        base = policy.base_delay_seconds * (2 ** max(attempt - 1, 0))
    delay = min(max(base, 0), policy.max_delay_seconds)
    if policy.jitter_fraction <= 0 or delay <= 0:
        return delay
    jitter = random.uniform(1 - policy.jitter_fraction, 1 + policy.jitter_fraction)
    return max(0, delay * jitter)


def run_with_retries(
    operation: Callable[[], T],
    policy: RetryPolicy,
    *,
    stream: bool = False,
    label: str = "provider request",
    logger: Any = None,
) -> T:
    failures = 0
    while True:
        try:
            return operation()
        except Exception as exc:
            decision = classify_retry_error(exc, policy)
            if not decision.retryable or failures >= policy.max_retries(stream=stream):
                raise
            failures += 1
            delay = retry_delay_seconds(failures, policy, decision)
            if logger:
                logger.warning(
                    f"{label} failed with retryable {decision.category}; "
                    f"retrying {failures}/{policy.max_retries(stream=stream)} in {delay:.2f}s: {exc}"
                )
            if delay > 0:
                time.sleep(delay)


async def sleep_before_retry(
    failures: int,
    policy: RetryPolicy,
    decision: RetryDecision,
) -> float:
    delay = retry_delay_seconds(failures, policy, decision)
    if delay > 0:
        await asyncio.sleep(delay)
    return delay


def _retry_after_seconds(headers: Any) -> Optional[float]:
    if not headers:
        return None
    raw = None
    if isinstance(headers, dict):
        raw = headers.get("Retry-After") or headers.get("retry-after")
    else:
        try:
            raw = headers.get("Retry-After")
        except Exception:
            raw = None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
