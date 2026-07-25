import asyncio
from typing import Any, Optional

from ...config.types import StreamController


class StreamStopped(Exception):
    """Raised when the caller has requested a streaming response to stop."""


async def get_queue_item_or_stop(
    queue: asyncio.Queue,
    stream_controller: Optional[StreamController],
    *,
    poll_interval_seconds: float = 0.05,
) -> Any:
    if stream_controller is None:
        return await queue.get()

    while True:
        if await stream_controller.is_stopped():
            raise StreamStopped()
        try:
            return await asyncio.wait_for(queue.get(), timeout=poll_interval_seconds)
        except asyncio.TimeoutError:
            continue
