"""OT-23: funnel rates expose n, d, and d_meaning."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot23.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    return ensure_desk(LOCAL_PERSON_ID).id


def _ready_contact(workspace_id: str) -> str:
    from app.gtm.relationships.ingest import ingest_paste
    from app.gtm.relationships.service import patch_contact, patch_onboarding

    patch_onboarding(
        workspace_id,
        {
            "who": "Founders at developer-tool companies who live in Slack",
            "roles": ["founder"],
            "excludes": ["competitor.com"],
            "sender_geo": "US",
            "sender_postal_address": "1 Main St, Oakland, CA 94612",
            "outbound_mode": "human_clipboard",
            "voice": {"write_like": "short and human"},
        },
    )
    out = ingest_paste(
        workspace_id,
        source="profile_paste",
        raw_text=(
            "Sam Builder\nsam@sample-tools.test\nFounder\n"
            '"we still run this workflow in a spreadsheet"\n'
        ),
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
    return out["contact_id"]


def test_accept_rate_n_d_of_sent(workspace):
    from app.gtm.relationships.funnel import funnel_metrics
    from app.gtm.relationships.service import create_draft, log_outcome, mark_copied, mark_sent

    cid = _ready_contact(workspace)
    draft = create_draft(workspace, cid, kind="connect")
    mark_copied(workspace, draft["id"])
    sent = mark_sent(workspace, draft["id"], channel="linkedin_connect")
    log_outcome(workspace, sent["id"], "connection_accepted")

    funnel = funnel_metrics(workspace, window_days=30)
    accepted = funnel["stages"]["accepted"]
    assert accepted["n"] == 1
    assert accepted["d"] == 1
    assert accepted["d_meaning"] == "of_sent"
    assert accepted["rate"] == 1.0
    replied = funnel["stages"]["replied"]
    assert replied["d"] == 1
    assert replied["d_meaning"] == "of_accepted"
    assert replied["n"] == 0
    for stage in funnel["stages"].values():
        assert "n" in stage and "d" in stage and "d_meaning" in stage
        assert "rate" in stage
