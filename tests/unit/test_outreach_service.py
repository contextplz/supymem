"""OT-21: copy does not set sent_at; SKU review required; calendar in connect blocked."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot21.db")
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
            "voice": {"write_like": "short, no pitch, quote their words"},
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


def test_copy_does_not_set_sent_at(workspace):
    from app.gtm.relationships.service import create_draft, mark_copied, mark_sent

    cid = _ready_contact(workspace)
    draft = create_draft(workspace, cid, kind="connect")
    assert draft["id"]
    copied = mark_copied(workspace, draft["id"])
    assert copied["copied_at"]
    assert copied["sent_at"] is None
    sent = mark_sent(workspace, draft["id"], channel="linkedin_connect")
    assert sent["sent_at"]


def test_sku_attach_without_review_409(workspace):
    from app.gtm.relationships.skus import SkuAttachError, attach_sku

    cid = _ready_contact(workspace)
    with pytest.raises(SkuAttachError) as exc:
        attach_sku(workspace, cid, "2026-09-bench-01", reviewed=False)
    assert exc.value.code == "sku_not_reviewed"


def test_calendar_in_connect_blocked():
    from app.gtm.relationships.honesty import check_draft

    result = check_draft(
        body="Connect with me: https://calendly.com/x",
        kind="connect",
        ask_rung="none",
        product_name=None,
        hook="we still run this workflow in a spreadsheet",
    )
    assert result.blocked is True
    assert "premature_calendar" in result.flags
