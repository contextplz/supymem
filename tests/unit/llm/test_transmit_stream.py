"""transmit_stream yields deltas then a usage chunk."""

from __future__ import annotations

import pytest

from llm_gateway import sole
from llm_gateway.boundary import Decision, register_filter
from llm_gateway.sole import transmit_stream


@pytest.fixture(autouse=True)
def _passthrough_boundary():
    register_filter(lambda messages, **_k: Decision(messages=list(messages)))
    yield
    register_filter(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "audience"), [
    ("local", "warehouse dispatchers"), ("sample", "home bakers"),
])
async def test_local_stream_yields_usage(monkeypatch, provider, audience) -> None:
    monkeypatch.setattr(sole, "sample_mode_enabled", lambda: provider == "sample")
    monkeypatch.setattr(sole, "resolve_override", lambda *args, **kwargs: pytest.fail("Guard must not resolve a network provider"))
    chunks = [
        c
        async for c in transmit_stream(
            [{"role": "user", "content": audience}],
            purpose="guard.injection",
        )
    ]
    assert chunks[0]["type"] == "delta"
    assert chunks[-1]["type"] == "usage"
    assert chunks[-1]["provider_used"] == provider
    assert chunks[-1]["usage"]["prompt_tokens"] == 0
