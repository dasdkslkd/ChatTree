import codecs
from typing import Any, Iterator


def iter_decoded_sse_lines(response: Any, chunk_size: int = 1024) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""

    while True:
        chunk = response.read(chunk_size)
        if not chunk:
            break
        buffer += decoder.decode(chunk, final=False)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")

    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer.rstrip("\r")
