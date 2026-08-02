import asyncio
from contextlib import suppress
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx

from ...config.types import StreamController


class StreamStopped(Exception):
    """Raised when the caller stops a provider stream."""


T = TypeVar("T")


async def _await_or_stop(
    awaitable: Awaitable[T],
    stream_controller: StreamController | None,
    deadline: float,
) -> T:
    task = asyncio.create_task(awaitable)
    loop = asyncio.get_running_loop()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait({task}, timeout=min(0.05, remaining))
            if done:
                return task.result()
            if stream_controller and await stream_controller.is_stopped():
                raise StreamStopped
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def iter_sse_lines(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    config: dict[str, Any],
    http_error_type: Callable[[int, str, dict[str, str]], Exception],
    stream_controller: StreamController | None = None,
) -> AsyncIterator[str]:
    transport = config["model_transport"]
    connect_timeout = float(transport["connect_timeout_seconds"])
    first_event_timeout = float(transport["first_event_timeout_seconds"])
    idle_timeout = float(transport["stream_idle_timeout_seconds"])
    loop = asyncio.get_running_loop()
    first_event_deadline = loop.time() + first_event_timeout
    timeout = httpx.Timeout(connect=connect_timeout, read=None, write=None, pool=connect_timeout)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            request = client.build_request("POST", url, json=body, headers=headers)
            try:
                response = await _await_or_stop(
                    client.send(request, stream=True),
                    stream_controller,
                    first_event_deadline,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Provider response headers timed out after {first_event_timeout:g}s"
                ) from exc

            try:
                if response.status_code >= 400:
                    try:
                        error_body = await _await_or_stop(
                            response.aread(),
                            stream_controller,
                            loop.time() + idle_timeout,
                        )
                    except TimeoutError:
                        error_body = b""
                    raise http_error_type(
                        response.status_code,
                        error_body.decode("utf-8", errors="replace"),
                        dict(response.headers),
                    )

                lines = response.aiter_lines()
                first_line = True
                while True:
                    deadline = first_event_deadline if first_line else loop.time() + idle_timeout
                    try:
                        line = await _await_or_stop(anext(lines), stream_controller, deadline)
                    except StopAsyncIteration:
                        break
                    except TimeoutError as exc:
                        phase = "first SSE event" if first_line else "SSE idle"
                        limit = first_event_timeout if first_line else idle_timeout
                        raise TimeoutError(f"Provider {phase} timed out after {limit:g}s") from exc
                    if line:
                        first_line = False
                    yield line
            finally:
                await response.aclose()
    except StreamStopped:
        raise
    except httpx.TimeoutException as exc:
        raise TimeoutError(str(exc)) from exc
    except httpx.RequestError as exc:
        raise ConnectionError(str(exc)) from exc
