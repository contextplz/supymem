"""Fail-closed warden client: unreachable warden → deny, no stale allows."""

from __future__ import annotations

import httpx
import pytest

from app.core.warden_client import WardenClient


@pytest.mark.asyncio
async def test_unreachable_warden_denies() -> None:
    emitted: list[tuple[str, str]] = []

    client = WardenClient(
        base_url="http://127.0.0.1:59999",  # nothing listening
        timeout_s=0.2,
        retries=1,
        emit=lambda r, d: emitted.append((r, d)),
    )
    resp = await client.check(capability="fs:read", venture_id="ven_x", target="README.md")
    assert resp.decision == "deny"
    assert resp.reason == "warden_unavailable"
    assert emitted
    assert emitted[0][0] == "warden_unavailable"


@pytest.mark.asyncio
async def test_timeout_denies() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    transport = httpx.MockTransport(handler)
    client = WardenClient(
        base_url="http://warden.test",
        timeout_s=0.05,
        retries=0,
        transport=transport,
    )
    resp = await client.check(capability="fs:read")
    assert resp.decision == "deny"
    assert resp.reason == "warden_unavailable"


@pytest.mark.asyncio
async def test_malformed_response_denies() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": True})

    client = WardenClient(
        base_url="http://warden.test",
        timeout_s=0.5,
        retries=0,
        transport=httpx.MockTransport(handler),
    )
    resp = await client.check(capability="fs:read")
    assert resp.decision == "deny"
    assert resp.reason == "warden_unavailable"


@pytest.mark.asyncio
async def test_http_500_denies_after_retry() -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    client = WardenClient(
        base_url="http://warden.test",
        timeout_s=0.5,
        retries=1,
        transport=httpx.MockTransport(handler),
    )
    resp = await client.check(capability="agent.spawn")
    assert resp.decision == "deny"
    assert resp.reason == "warden_unavailable"
    assert calls["n"] == 2  # original + one retry


@pytest.mark.asyncio
async def test_no_stale_cached_allow() -> None:
    """Client must not return a cached allow when warden later denies / dies."""
    state = {"allow": True}

    async def handler(request: httpx.Request) -> httpx.Response:
        if state["allow"]:
            return httpx.Response(
                200, json={"decision": "allow", "reason": "policy_permits"}
            )
        raise httpx.ConnectError("warden down")

    client = WardenClient(
        base_url="http://warden.test",
        timeout_s=0.5,
        retries=0,
        transport=httpx.MockTransport(handler),
    )
    first = await client.check(capability="fs:read")
    assert first.decision == "allow"
    state["allow"] = False
    second = await client.check(capability="fs:read")
    assert second.decision == "deny"
    assert second.reason == "warden_unavailable"


@pytest.mark.asyncio
async def test_agent_run_terminates_cleanly_on_deny() -> None:
    """Simulate an agent loop that aborts when check fails closed."""

    client = WardenClient(
        base_url="http://127.0.0.1:59998",
        timeout_s=0.1,
        retries=0,
    )

    async def agent_step() -> str:
        verdict = await client.check(capability="tool:post_public", agent_id="ag_1")
        if verdict.decision != "allow":
            return f"terminated:{verdict.reason}"
        return "posted"

    result = await agent_step()
    assert result == "terminated:warden_unavailable"
