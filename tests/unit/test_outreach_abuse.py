"""OT-34: cadence cap 429; admin pause 403 on copy/sent."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot34.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


def _draft(workspace_id: str) -> str:
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.service import create_draft, patch_contact, patch_onboarding

    patch_onboarding(
        workspace_id,
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
    out = ingest_paste(
        workspace_id,
        source="profile_paste",
        raw_text='Sam Builder\nsam@sample-tools.test\nFounder\n"we still run this workflow in a spreadsheet"',
        trigger_code="hiring_complaint",
        enrich=False,
    )
    patch_contact(
        workspace_id,
        out["contact_id"],
        {
            "why_this_week": "They posted a hiring complaint this week",
            "recipient_geo": "US",
            "role": "founder",
        },
    )
    draft = create_draft(workspace_id, out["contact_id"], kind="connect")
    return draft["id"]


def test_sixth_connect_copy_is_429(workspace):
    from app.gtm.relationships.service import OutreachError, mark_copied

    did = _draft(workspace)
    for _ in range(5):
        mark_copied(workspace, did)
    with pytest.raises(OutreachError) as exc:
        mark_copied(workspace, did)
    assert exc.value.status == 429
    assert exc.value.code == "cadence_cap"


def test_admin_pause_forbids_copy(workspace):
    from app.gtm.relationships.abuse import pause_sends
    from app.gtm.relationships.service import OutreachError, mark_copied

    did = _draft(workspace)
    pause_sends(workspace)
    with pytest.raises(OutreachError) as exc:
        mark_copied(workspace, did)
    assert exc.value.status == 403
    assert exc.value.code == "send_paused"
