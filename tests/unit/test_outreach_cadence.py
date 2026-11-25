"""OT-44: 6th connect copy in a week is 429. Caps live in cadence.py (used by abuse.py)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot44.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
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
    return desk.id, ingested["contact_id"]


def test_sixth_connect_copy_429(workspace):
    from app.gtm.relationships.service import OutreachError, create_draft, mark_copied

    wid, cid = workspace
    for i in range(5):
        draft = create_draft(wid, cid, kind="connect")
        assert draft.get("body")
        copied = mark_copied(wid, draft["id"])
        assert copied["copied_at"]
        assert copied["sent_at"] is None
    sixth = create_draft(wid, cid, kind="connect")
    with pytest.raises(OutreachError) as exc:
        mark_copied(wid, sixth["id"])
    assert exc.value.status == 429
    assert exc.value.code == "cadence_cap"
