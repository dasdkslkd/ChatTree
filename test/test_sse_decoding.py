import sys

sys.path.insert(0, ".")

from backend.core.model.providers.sse import iter_decoded_sse_lines


class ChunkedResponse:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, _size):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


def test_iter_decoded_sse_lines_preserves_split_utf8_characters():
    text = 'data: {"content":"你好🙂"}\n\n'
    raw = text.encode("utf-8")
    first_split = raw.index("你".encode("utf-8")) + 1
    second_split = raw.index("🙂".encode("utf-8")) + 2
    chunks = [
        raw[:first_split],
        raw[first_split:second_split],
        raw[second_split:],
    ]

    lines = list(iter_decoded_sse_lines(ChunkedResponse(chunks)))

    assert lines == ['data: {"content":"你好🙂"}', ""]
    assert "�" not in "".join(lines)


def test_iter_decoded_sse_lines_flushes_final_line_without_newline():
    lines = list(iter_decoded_sse_lines(ChunkedResponse([b"data: ", "尾行".encode("utf-8")])))

    assert lines == ["data: 尾行"]
