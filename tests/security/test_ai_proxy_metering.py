"""Existing proxy metering checks use captured receipts and real wallet custody."""

from __future__ import annotations

import json
import httpx
from openai import AsyncOpenAI
import pytest
from sqlalchemy import select

from tests.company_os_harness import COMPANY_OS

if not (COMPANY_OS / "app/api/openai_api.py").is_file():
    pytest.skip("Hosted OpenAI proxy is not in the public local release.", allow_module_level=True)

from tests.security.test_openai_proxy_financial_capture import (
    MODEL, _post, _state, proxy as proxy,
)


def _expect_wire_limit(p, limit):
    """Only replace HTTP for this larger clamp case; money and auth stay real."""
    from app.tenancy import current_or_none
    from llm_gateway import sole

    def transport(request):
        scope = current_or_none()
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (p.wid, p.vid, p.key["id"])
        saved = _state(p)
        assert len(saved.holds) == 1 and saved.holds[0][2] == "reconciling"
        assert not saved.ledger and not saved.records
        assert any(topic.endswith(".calling") for topic, _ in saved.events)
        body = json.loads(request.content)
        assert body["model"] == MODEL and body["service_tier"] == "default" and body["max_output_tokens"] == limit
        assert request.headers["x-stainless-retry-count"] == "0"
        p.calls.append((saved.holds[0][0], saved.holds[0][1], body))
        return httpx.Response(200, content=json.dumps(p.payload).encode(), headers={"content-type": "application/json"})

    def factory(config):
        assert config.source == "platform_default" and config.routing_purpose == "app.proxy"
        client = AsyncOpenAI(api_key="offline-proxy-key", base_url="https://offline.invalid/openai/v1",
            max_retries=2, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport),
                follow_redirects=False, trust_env=False))
        p.clients.append(client)
        return client, config

    p.monkeypatch.setattr(sole, "create_async_client_from_config", factory)


def test_reserves_before_upstream_clamps_and_bills_tokens(proxy) -> None:
    p = proxy
    _expect_wire_limit(p, 2048)
    res = _post(p, max_tokens=10_000_000)
    assert res.status_code == 200, res.text
    saved = _state(p)
    assert p.calls[0][2]["max_output_tokens"] == 2048  # existing config ceiling reaches the real SDK
    assert saved.spent == 662 and saved.spent > 100  # original snapshot/cache counts, not a flat fee
    assert len(p.calls) == len(saved.holds) == len(saved.ledger) == len(saved.records) == 1
    assert saved.ledger[0][1:] == (p.wallet.id, p.calls[0][0], p.calls[0][1], -662)


def test_no_credit_account_refuses_before_upstream(proxy) -> None:
    from app.credits._txn import naive_utc
    from app.db.models import Person, Workspace
    from app.state import db
    from app.tenancy import bind
    p = proxy
    # A verified app can outlive an owner transfer. The new owner has no wallet.
    with bind(workspace_id=p.wid, venture_id=p.vid, actor="system"):
        with db()() as session:
            session.add(Person(id="per_proxy_without_wallet", display_name="Synthetic new owner", created_at=naive_utc()))
            session.flush()
            session.get(Workspace, p.wid).owner_id = "per_proxy_without_wallet"
            session.commit()
    res = _post(p)
    assert res.status_code == 402
    assert not p.calls and not p.clients and not _state(p).holds


def test_monthly_cap_checked_before_upstream(proxy) -> None:
    from app.db.supymem_models import AppApiKey
    from app.state import db
    from app.tenancy import bind
    p = proxy
    with bind(workspace_id=p.wid, venture_id=p.vid, actor="system"):
        with db()() as session:
            session.get(AppApiKey, p.key["id"]).monthly_cap_credits = 0
            session.commit()
    assert _post(p).status_code == 402
    assert not p.calls and not p.clients and not _state(p).holds


def test_hold_for_proxy_without_account_raises(proxy) -> None:
    from app.apps import usage
    from app.credits.errors import InsufficientCredits
    p = proxy
    with pytest.raises(InsufficientCredits):
        usage.hold_for_proxy(workspace_id="ws_without_wallet", venture_id="ven_without_wallet",
            amount_micro=100, request_id="req_without_wallet", model=MODEL)
    assert not p.calls and not p.clients and not _state(p).holds


@pytest.mark.parametrize("inputs,outputs,fee", [(0, 0, 0), (1, 0, 2), (0, 1, 10)])
def test_confirmed_zero_and_tiny_usage_never_adds_the_old_actual_fee_floor(proxy, inputs, outputs, fee):
    p = proxy
    p.payload["usage"] = {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs,
        "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
    response = _post(p)
    assert response.status_code == 200 and response.headers["x-supymem-credits-used"] == str(fee)
    saved = _state(p)
    assert saved.spent == fee and saved.spent < 100 and saved.reserved == 0
    assert len(saved.ledger) == len(saved.records) == len(p.calls) == 1


@pytest.mark.parametrize("route", ["offline", "tierless-chat"])
def test_unconfirmed_routes_refuse_before_reserve_and_sdk(proxy, route):
    p = proxy
    if route == "offline":
        p.monkeypatch.setenv("LLM_OFFLINE", "1")
    else:
        p.monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "{}")
        p.monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "DeepSeek-V4-Pro")
        p.monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "DeepSeek-V4-Pro")
    assert _post(p).status_code == 503
    assert not p.calls and not p.clients and not _state(p).holds


def test_existing_sse_delivery_follows_one_completed_wallet_settlement(proxy):
    p = proxy
    response = _post(p, stream=True)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    chunks = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(chunks) == 3 and chunks[-1] == "[DONE]"
    assert json.loads(chunks[0])["choices"][0]["delta"]["content"] == "synthetic"
    assert json.loads(chunks[1])["choices"][0]["finish_reason"] == "stop"
    saved = _state(p)
    assert saved.spent == 662 and saved.reserved == 0 and len(p.calls) == len(saved.ledger) == len(saved.records) == 1


def test_known_paid_output_beyond_budget_is_withheld_and_keeps_provider_actual(proxy):
    from app.db.models import CreditLedgerEntry
    from app.state import db
    from app.tenancy import bind
    p = proxy
    p.payload["usage"].update(output_tokens=50_000, total_tokens=50_100, output_tokens_details={"reasoning_tokens": 0})
    response = _post(p)
    assert response.status_code == 502 and "choices" not in response.json()
    saved = _state(p)
    assert 100 < saved.spent < 500162 and saved.reserved == 0 and len(p.calls) == len(saved.ledger) == len(saved.records) == 1
    assert response.headers["x-supymem-credits-used"] == str(saved.spent)
    with bind(workspace_id=p.wid, venture_id=p.vid, actor="system"):
        with db()() as session:
            row = session.scalar(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == p.calls[0][0]))
            meta = json.loads(row.meta_json)
            assert meta["provider_actual_micro"] == meta["raw_provider_actual_micro"] == 500162
            assert meta["clamped"] is True
