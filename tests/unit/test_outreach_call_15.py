"""OT-43: POST call_15 draft is 409 without value_return + approved SKU."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot43.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.service import patch_contact, patch_onboarding
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    patch_onboarding(
        desk.id,
        {
            "who": "Founders at developer-tool companies who live in Slack",
            "roles": ["founder"],
            "excludes": [],
            "sender_geo": "US",
            "sender_postal_address": "1 Main St, Oakland, CA 94612",
            "outbound_mode": "human_clipboard",
            "voice": {"write_like": "short"},
        },
    )
    ingested = ingest_paste(
        desk.id,
        source="profile_paste",
        raw_text='Sam\nsam@sample-tools.test\nFounder\n"we still run this workflow in a spreadsheet"',
        trigger_code="hiring_complaint",
        enrich=False,
    )
    patch_contact(
        desk.id,
        ingested["contact_id"],
        {
            "why_this_week": "They posted a hiring complaint this week",
            "recipient_geo": "US",
            "role": "founder",
        },
    )
    return {"main": main, "wid": desk.id, "cid": ingested["contact_id"]}


def test_meeting_draft_409_without_sku(setup):
    headers = {"X-Workspace-Id": setup["wid"]}
    with TestClient(setup["main"].create_app()) as client:
        r = client.post(
            "/api/v1/outreach/drafts",
            json={"contact_id": setup["cid"], "kind": "call_15"},
            headers=headers,
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "call_15_locked"
