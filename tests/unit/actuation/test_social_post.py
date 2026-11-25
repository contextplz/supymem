from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.asyncio
async def test_social_post_is_simulated_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    load_company_os_app()
    from app.actuation.contract import ActuationMode
    from app.actuation.tools.social_post import social_post

    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    monkeypatch.delenv("OUTSTAND_API_KEY", raising=False)
    result = await social_post("ven_social", {"content": "hello"})
    assert result.mode is ActuationMode.SIMULATED
    assert result.reason == "no_credential"


def test_social_post_is_registered() -> None:
    load_company_os_app()
    from app.actuation.registry import get

    assert get("social.post") is not None


def test_zernio_selects_connected_accounts() -> None:
    load_company_os_app()
    from app.actuation.providers.zernio import parse_accounts, select_platforms

    rows = parse_accounts(
        {
            "accounts": [
                {"_id": "tw1", "platform": "twitter", "isActive": True},
                {"_id": "li1", "platform": "linkedin", "isActive": True},
                {"_id": "ig1", "platform": "instagram", "isActive": False},
            ]
        }
    )
    picked = select_platforms(rows, wanted=["x"])
    assert picked == [{"platform": "twitter", "accountId": "tw1"}]


@pytest.mark.asyncio
async def test_social_post_live_when_zernio_returns_id(monkeypatch: pytest.MonkeyPatch) -> None:
    load_company_os_app()
    from app.actuation.contract import ActuationMode
    from app.actuation.providers import zernio as zernio_api
    from app.actuation.tools.social_post import social_post

    monkeypatch.setenv("ZERNIO_API_KEY", "zn_test_not_a_real_key")
    monkeypatch.delenv("OUTSTAND_API_KEY", raising=False)

    async def fake_accounts(_client, _key):
        return [{"_id": "acc1", "platform": "twitter", "isActive": True}]

    async def fake_create(_client, _key, body, *, idempotency_key=None):
        assert body["content"] == "hello from test"
        assert body["publishNow"] is True
        assert body["platforms"] == [{"platform": "twitter", "accountId": "acc1"}]
        return {"_id": "post_1", "url": "https://x.com/acme/status/1"}

    monkeypatch.setattr(zernio_api, "list_accounts", fake_accounts)
    monkeypatch.setattr(zernio_api, "create_post", fake_create)

    result = await social_post("ven_social", {"content": "hello from test"})
    assert result.mode is ActuationMode.LIVE
    assert result.external_ref == "post_1"
    assert result.external_url == "https://x.com/acme/status/1"


@pytest.mark.asyncio
async def test_social_post_fails_without_connected_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    load_company_os_app()
    from app.actuation.contract import ActuationMode
    from app.actuation.providers import zernio as zernio_api
    from app.actuation.tools.social_post import social_post

    monkeypatch.setenv("ZERNIO_API_KEY", "zn_test_not_a_real_key")

    async def fake_accounts(_client, _key):
        return []

    monkeypatch.setattr(zernio_api, "list_accounts", fake_accounts)
    result = await social_post("ven_social", {"content": "hello"})
    assert result.mode is ActuationMode.FAILED
    assert result.reason == "no_connected_accounts"
