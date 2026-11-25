"""Foundry 5xx/429 beyond retry budget falls back to Bedrock."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_gateway import sole
from llm_gateway.errors import ProviderCallError
from llm_gateway.reliability import Reliability
from llm_gateway.sole import set_reliability_for, set_transmit_hooks, transmit
from llm_gateway.boundary import Decision, register_filter


@pytest.fixture(autouse=True)
def _passthrough_boundary():
    register_filter(lambda messages, **_k: Decision(messages=list(messages)))
    yield
    register_filter(None)


def _completion(text: str, provider: str) -> SimpleNamespace:
    msg = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="bedrock-test",
        provider_used=provider,
    )


@pytest.mark.asyncio
async def test_foundry_503_falls_back_to_bedrock(monkeypatch) -> None:
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "bedrock")
    set_reliability_for("flash", Reliability(max_attempts=1, base_delay=0, jitter=False))

    async def primary(*_a, **_k):
        raise ProviderCallError("provider_down", "boom", status=503, retryable=True)

    async def fallback(*_a, **_k):
        return _completion("from bedrock", "bedrock")

    set_transmit_hooks(primary=primary, fallback=fallback)
    try:
        resp = await transmit(
            [{"role": "user", "content": "hi"}],
            purpose="intake.classify",
        )
        assert getattr(resp, "provider_used") == "bedrock"
        assert resp.choices[0].message.content == "from bedrock"
    finally:
        set_transmit_hooks(primary=None, fallback=None)
        set_reliability_for("flash", None)


@pytest.mark.asyncio
async def test_non_retryable_does_not_fallback(monkeypatch) -> None:
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "bedrock")
    set_reliability_for("flash", Reliability(max_attempts=1, base_delay=0, jitter=False))

    async def primary(*_a, **_k):
        raise ProviderCallError("bad_api_key", "nope", status=401, retryable=False)

    called = {"fb": False}

    async def fallback(*_a, **_k):
        called["fb"] = True
        return _completion("should not", "bedrock")

    set_transmit_hooks(primary=primary, fallback=fallback)
    try:
        with pytest.raises(ProviderCallError) as ei:
            await transmit([{"role": "user", "content": "hi"}], purpose="intake.classify")
        assert ei.value.code == "bad_api_key"
        assert called["fb"] is False
    finally:
        set_transmit_hooks(primary=None, fallback=None)
        set_reliability_for("flash", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "audience"), [
    ("local", "equipment maintenance crews"), ("sample", "family hiking groups"),
])
async def test_local_tier_does_not_dial(monkeypatch, provider, audience) -> None:
    monkeypatch.setattr(sole, "sample_mode_enabled", lambda: provider == "sample")
    monkeypatch.setattr(sole, "resolve_override", lambda *args, **kwargs: pytest.fail("Guard must not resolve a network provider"))
    resp = await transmit(
        [{"role": "user", "content": audience}],
        purpose="guard.injection",
    )
    assert resp.provider_used == resp.model == provider
    assert resp.usage.total_tokens == 0
