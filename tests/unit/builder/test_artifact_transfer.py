"""Adversarial private-task stream bounds without cloud or model calls."""
import httpx
import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.builder import artifacts
from app.builder.model_proxy import BuildModelError


def test_slow_small_chunks_stop_before_buffering_full_response(monkeypatch):
    elapsed = [0]
    seen = []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(100):
                elapsed[0] += 10
                seen.append(index)
                yield b"a"

    monkeypatch.setattr(artifacts.time, "monotonic", lambda: elapsed[0])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "application/zip", "content-length": "100"}, stream=SlowStream()))) as client:
        with pytest.raises(BuildModelError, match="artifact_transfer_limit"):
            artifacts._read(client, "http://10.0.1.5:8000/archive", {}, limit=100,
                            content_type="application/zip", deadline=25)
    assert len(seen) == 3


def test_late_headers_and_empty_response_obey_deadline(monkeypatch):
    monkeypatch.setattr(artifacts.time, "monotonic", lambda: 31)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "application/zip"}, stream=httpx.ByteStream(b"")))) as client:
        with pytest.raises(BuildModelError, match="artifact_transfer_limit"):
            artifacts._read(client, "http://10.0.1.5:8000/archive", {}, limit=100,
                            content_type="application/zip", deadline=30)
