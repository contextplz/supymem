"""OT-30: B's queue never contains A's contact ids."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot30list.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.service import patch_contact, patch_onboarding
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
    for wid in (desk_a.id, ws_b.id):
        patch_onboarding(
            wid,
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
    return {
        "main": main,
        "desk_a": desk_a.id,
        "ws_b": ws_b.id,
        "contact_id": ingested["contact_id"],
    }


def test_queue_does_not_leak(setup):
    with TestClient(setup["main"].create_app()) as client:
        bq = client.get(
            "/api/v1/outreach/queue",
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
        assert bq.status_code == 200
        ids = {item["id"] for item in bq.json().get("items") or []}
        assert setup["contact_id"] not in ids
        aq = client.get(
            "/api/v1/outreach/queue",
            headers={"X-Workspace-Id": setup["desk_a"]},
        )
        assert aq.status_code == 200
        a_ids = {item["id"] for item in aq.json().get("items") or []}
        assert setup["contact_id"] in a_ids
