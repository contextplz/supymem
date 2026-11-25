"""OT-39: evidence sources record intent; do not fake connected OAuth data."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot39.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    return {"main": main, "wid": desk.id}


def test_gmail_connected_without_account_is_409(setup):
    from app.gtm.relationships.evidence import EvidenceError, record_evidence_source

    with pytest.raises(EvidenceError) as exc:
        record_evidence_source(setup["wid"], provider="gmail", action="connected")
    assert exc.value.code == "oauth_not_connected"


def test_slack_cannot_fake_connected(setup):
    from app.gtm.relationships.evidence import EvidenceError, record_evidence_source

    with pytest.raises(EvidenceError) as exc:
        record_evidence_source(setup["wid"], provider="slack", action="connected")
    assert exc.value.code == "oauth_not_connected"


def test_http_start_and_list(setup):
    headers = {"X-Workspace-Id": setup["wid"]}
    with TestClient(setup["main"].create_app()) as client:
        r = client.post(
            "/api/v1/outreach/evidence-sources",
            json={"provider": "slack", "action": "start"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "started"
        assert r.json()["fake"] is False
        listed = client.get("/api/v1/outreach/evidence-sources", headers=headers)
        assert listed.status_code == 200
        slack = next(s for s in listed.json()["sources"] if s["provider"] == "slack")
        assert slack["status"] == "started"
        assert slack["connected_account_id"] is None
