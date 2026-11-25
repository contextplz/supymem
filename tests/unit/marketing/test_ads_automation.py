"""Paid-campaign workflow tests: no approval or provider proof, no campaign."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


def _payload() -> dict[str, object]:
    return {
        "platform": "google",
        "idea": "A scheduling assistant for independent fitness studios.",
        "goal": "leads",
        "landing_url": "https://example.com/book-demo",
        "audience": "Owners of independent fitness studios",
        "geography": "US",
        "daily_budget_usd": 10,
    }


def _desk(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ads.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/ads.db")
    load_company_os_app()
    from app.state import reset_db_factory
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID)


def test_proposal_requires_approval_before_provider_creation(tmp_path, monkeypatch):
    desk = _desk(tmp_path, monkeypatch)
    from app.marketing.ads import service

    proposal = service.create_proposal(desk.id, _payload())
    assert proposal["status"] == "draft"
    assert proposal["blueprint"]["status_on_create"] == "PAUSED"

    with pytest.raises(service.AdsError, match="Approve this campaign"):
        service.create_paused(desk.id, proposal["id"])


def test_approved_proposal_creates_one_paused_campaign(tmp_path, monkeypatch):
    desk = _desk(tmp_path, monkeypatch)
    from app.marketing.ads import service
    from app.marketing.media import probes
    from app.workspaces.resolve import LOCAL_PERSON_ID

    proposal = service.create_proposal(desk.id, _payload())
    requested = service.request_launch(desk.id, proposal["id"])
    assert requested["status"] == "pending_approval"
    approved = service.decide_launch(
        desk.id, proposal["id"], decision="approved", decided_by=LOCAL_PERSON_ID
    )
    assert approved["status"] == "approved"

    monkeypatch.setattr(
        probes,
        "create_paused_ad",
        lambda **_: {
            "ok": True,
            "status": "done",
            "result": {
                "status": "PAUSED",
                "resource_name": "customers/123/campaigns/456",
            },
            "proof": {"mode": "api", "external_id": "customers/123/campaigns/456"},
        },
    )
    created = service.create_paused(desk.id, proposal["id"])
    assert created["ok"]
    assert created["campaign"]["status"] == "PAUSED"
    assert created["campaign"]["external_id"] == "customers/123/campaigns/456"

    replay = service.create_paused(desk.id, proposal["id"])
    assert replay["replayed"] is True
    assert len(service.list_campaigns(desk.id)) == 1


def test_proposal_rejects_unsafe_budget_and_landing_page(tmp_path, monkeypatch):
    desk = _desk(tmp_path, monkeypatch)
    from app.marketing.ads import service

    invalid_url = _payload() | {"landing_url": "not-a-url"}
    with pytest.raises(service.AdsError, match="http"):
        service.create_proposal(desk.id, invalid_url)

    invalid_budget = _payload() | {"daily_budget_usd": 26}
    with pytest.raises(service.AdsError, match="25.00"):
        service.create_proposal(desk.id, invalid_budget)


def test_blueprint_contains_runnable_paused_ad_spec(tmp_path, monkeypatch):
    desk = _desk(tmp_path, monkeypatch)
    from app.marketing.ads import service

    proposal = service.create_proposal(desk.id, _payload())
    blueprint = proposal["blueprint"]
    assert blueprint["keywords"]
    assert len(blueprint["creative"]["google_headlines"]) == 3
    assert len(blueprint["creative"]["google_descriptions"]) == 2
    assert all(len(text) <= 30 for text in blueprint["creative"]["google_headlines"])
    assert all(len(text) <= 90 for text in blueprint["creative"]["google_descriptions"])


def test_provider_adapters_fail_closed_without_credentials(monkeypatch):
    from app.marketing.adapters import ads_google, ads_meta

    for key in (*ads_google.REQUIRED_ENV, *ads_meta.REQUIRED_ENV):
        monkeypatch.delenv(key, raising=False)
    spec = {
        "landing_url": "https://example.com/demo",
        "creative": {
            "google_headlines": ["First", "Second", "Third"],
            "google_descriptions": ["First description", "Second description"],
        },
        "keywords": ["bookkeeping"],
    }
    google = ads_google.create_paused_campaign(
        name="Test", daily_budget="10", campaign_spec=spec
    )
    meta = ads_meta.create_paused_campaign(
        name="Test", daily_budget="10", campaign_spec=spec
    )
    assert google["status"] == "blocked"
    assert google["error"] == "google_ads_credentials_missing"
    assert meta["status"] == "blocked"
    assert meta["error"] == "meta_ads_credentials_missing"


def test_proposals_are_workspace_scoped(tmp_path, monkeypatch):
    desk = _desk(tmp_path, monkeypatch)
    from app.marketing.ads import service

    proposal = service.create_proposal(desk.id, _payload())
    assert service.list_proposals("wsp_other") == []
    with pytest.raises(service.AdsError) as exc:
        service.get_proposal("wsp_other", proposal["id"])
    assert exc.value.status == 404


def test_ads_api_click_path_creates_only_a_paused_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ads-api.db")
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path}/ads-api.db")
    main = load_company_os_app()
    from app.marketing.media import probes
    from tests.company_os_harness import company_os_test_client as TestClient

    monkeypatch.setattr(
        probes,
        "create_paused_ad",
        lambda **_: {
            "ok": True,
            "status": "done",
            "result": {"status": "PAUSED", "campaign_id": "meta-campaign-123"},
            "proof": {"mode": "api", "external_id": "meta-campaign-123"},
        },
    )
    with TestClient(main.app) as client:
        from app.workspaces.resolve import ensure_desk

        client.headers["X-Workspace-Id"] = ensure_desk().id
        created = client.post(
            "/api/v1/ads/proposals", json=_payload() | {"platform": "meta"}
        )
        assert created.status_code == 201, created.text
        proposal_id = created.json()["id"]

        requested = client.post(f"/api/v1/ads/proposals/{proposal_id}/request-launch")
        assert requested.status_code == 200, requested.text
        assert requested.json()["status"] == "pending_approval"

        decided = client.post(
            f"/api/v1/ads/proposals/{proposal_id}/decide", json={"decision": "approved"}
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "approved"

        launched = client.post(f"/api/v1/ads/proposals/{proposal_id}/create-paused")
        assert launched.status_code == 200, launched.text
        assert launched.json()["campaign"]["status"] == "PAUSED"

        campaigns = client.get("/api/v1/ads/campaigns")
        assert campaigns.status_code == 200
        assert campaigns.json()["items"][0]["external_id"] == "meta-campaign-123"
