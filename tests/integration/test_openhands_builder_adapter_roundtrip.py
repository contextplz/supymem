from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from coding_agents import load_adapter
from coding_agents.protocol import CodingRequest


@pytest.mark.asyncio
async def test_openhands_builder_adapter_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BUILDER_SERVICE_URL", "http://builder.test")
    monkeypatch.setenv("BUILDER_SERVICE_TOKEN", "tok")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/run"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "session_id": "oh-1",
                "changed_files": ["index.html"],
                "events": [{"kind": "finished", "text": "done"}],
                "error": None,
                "preview_url": "http://127.0.0.1:49152",
                "prompt_tokens": 10,
                "completion_tokens": 20,
            },
        )

    transport = httpx.MockTransport(handler)
    adapter = load_adapter("openhands_builder")

    real_async_client = httpx.AsyncClient

    class Wrapped(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Wrapped)

    req = CodingRequest(prompt="build a page", workdir=tmp_path, allow_write=True)
    result = await adapter.run(req)
    assert result.ok
    assert result.changed_files == ["index.html"]
    assert result.preview_url == "http://127.0.0.1:49152"
    assert result.argv[0] == "openhands-builder"
    assert real_async_client is not None


@pytest.mark.asyncio
async def test_openhands_builder_connect_error_is_infra(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BUILDER_SERVICE_URL", "http://127.0.0.1:1")
    adapter = load_adapter("openhands_builder")
    result = await adapter.run(CodingRequest(prompt="x", workdir=tmp_path))
    assert result.ok is False
    assert result.error == "infra"
