import socket

from backend.core.config.config import DEFAULT_MODEL_TRANSPORT
from backend.core.model.providers.retry import (
    RetryPolicy,
    RetryableHTTPError,
    classify_retry_error,
    retry_delay_seconds,
)


def test_retry_matrix_marks_transient_errors_retryable():
    policy = RetryPolicy.from_config({"model_transport": DEFAULT_MODEL_TRANSPORT})

    retryable = [
        RetryableHTTPError(408, "request timeout"),
        RetryableHTTPError(409, "conflict"),
        RetryableHTTPError(429, "rate limited"),
        RetryableHTTPError(500, "server error"),
        RetryableHTTPError(502, "bad gateway"),
        RetryableHTTPError(503, "unavailable"),
        RetryableHTTPError(504, "gateway timeout"),
        RetryableHTTPError(529, "overloaded"),
        TimeoutError("timed out"),
        socket.timeout("socket timed out"),
        ConnectionResetError("reset"),
    ]

    for error in retryable:
        decision = classify_retry_error(error, policy)
        assert decision.retryable, f"{error!r} should be retryable"


def test_retry_matrix_rejects_permanent_errors():
    policy = RetryPolicy.from_config({"model_transport": DEFAULT_MODEL_TRANSPORT})

    permanent = [
        RetryableHTTPError(400, "invalid request"),
        RetryableHTTPError(401, "unauthorized"),
        RetryableHTTPError(403, "forbidden"),
        RetryableHTTPError(404, "not found"),
        RetryableHTTPError(413, "context too large"),
        ValueError("local bug"),
    ]

    for error in permanent:
        decision = classify_retry_error(error, policy)
        assert not decision.retryable, f"{error!r} should not be retryable"


def test_retry_delay_uses_exponential_backoff_with_cap_and_retry_after():
    policy = RetryPolicy(
        max_request_retries=3,
        max_stream_retries=1,
        base_delay_seconds=0.5,
        max_delay_seconds=2.0,
        jitter_fraction=0.0,
    )

    assert retry_delay_seconds(1, policy) == 0.5
    assert retry_delay_seconds(2, policy) == 1.0
    assert retry_delay_seconds(3, policy) == 2.0
    assert retry_delay_seconds(4, policy) == 2.0

    error = RetryableHTTPError(429, "rate limited", headers={"Retry-After": "3"})
    decision = classify_retry_error(error, policy)
    assert decision.retry_after_seconds == 3.0
    assert retry_delay_seconds(1, policy, decision) == 2.0
