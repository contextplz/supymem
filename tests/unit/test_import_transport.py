import asyncio
from types import SimpleNamespace

import pytest
from core.bounded_body import BodyRejected, zip_body


def request(chunks, **headers):
    async def stream():
        for part in chunks:
            yield part
    return SimpleNamespace(headers={"content-type": "application/zip", **headers}, stream=stream)


def test_bounded_actual_bytes_without_trusting_content_length():
    assert asyncio.run(zip_body(request([b"abc", b"def"]), maximum=6, expected=6)) == b"abcdef"
    for req in (request([b"abc", b"defg"], **{"content-length": "6"}), request([b"ab"], **{"content-length": "6"})):
        with pytest.raises(BodyRejected, match="size_invalid"):
            asyncio.run(zip_body(req, maximum=6, expected=6))


@pytest.mark.parametrize("headers", [{"content-encoding": "gzip"}, {"content-type": "application/json"},
    {"content-length": "-1"}, {"content-length": "99999999999"}])
def test_rejected_before_stream(headers):
    async def stream():
        raise AssertionError("body should not be consumed")
        yield b""
    with pytest.raises(BodyRejected):
        asyncio.run(zip_body(SimpleNamespace(headers={"content-type": "application/zip", **headers}, stream=stream)))


def test_slow_body_has_total_deadline():
    async def stream():
        yield b"a"
        await asyncio.sleep(1)
        yield b"b"
    with pytest.raises(BodyRejected, match="deadline"):
        asyncio.run(zip_body(SimpleNamespace(headers={"content-type": "application/zip"}, stream=stream), timeout=.01))
