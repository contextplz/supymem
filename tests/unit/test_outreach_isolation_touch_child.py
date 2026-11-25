"""OT-30: child touch ids are workspace-scoped."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot30touch.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.service import create_draft, patch_contact, patch_onboarding
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    ws_b = create_workspace(
        kind="company",
        name="TenantB",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    patch_onboarding(
        desk_a.id,
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
        desk_a.id,
        source="profile_paste",
        raw_text='Ada Lovelace\nada@sample-agency.test\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    patch_contact(
        desk_a.id,
        ingested["contact_id"],
        {
            "why_this_week": "They posted a hiring complaint this week",
            "recipient_geo": "US",
            "role": "founder",
        },
    )
    draft = create_draft(desk_a.id, ingested["contact_id"], kind="connect")
    return {
        "main": main,
        "desk_a": desk_a.id,
        "ws_b": ws_b.id,
        "draft_id": draft["id"],
    }


def test_foreign_touch_copy_and_sent_404(setup):
    with TestClient(setup["main"].create_app()) as client:
        stolen = client.post(
            f"/api/v1/outreach/drafts/{setup['draft_id']}/copy",
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
        assert stolen.status_code == 404
        sent = client.post(
            f"/api/v1/outreach/drafts/{setup['draft_id']}/sent",
            json={"channel": "linkedin_connect"},
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
        assert sent.status_code == 404
        outcome = client.post(
            f"/api/v1/outreach/touches/{setup['draft_id']}/outcome",
            json={"outcome": "connection_accepted"},
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
        assert outcome.status_code == 404
        ok = client.post(
            f"/api/v1/outreach/drafts/{setup['draft_id']}/copy",
            headers={"X-Workspace-Id": setup["desk_a"]},
        )
        assert ok.status_code == 200
