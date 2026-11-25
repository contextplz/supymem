"""OT-18: harvested Layer B cannot score as sendable or auto-personalize."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.gates import HARVESTED_SOURCES, GateInput, evaluate_gates
from app.gtm.relationships.score import ScoreInput, draft_connect, score_contact


def test_harvested_sources_void_and_refuse_personalize():
    for source in sorted(HARVESTED_SOURCES):
        gates = evaluate_gates(
            GateInput(
                role="founder",
                icp_roles=("founder",),
                recipient_geo="US",
                sender_postal_address="1 Main St, Oakland, CA 94612",
                why_this_week="They posted a hiring complaint this week",
                why_this_week_confirmed=True,
                source=source,
            )
        )
        assert gates.void is True, source
        result = score_contact(
            ScoreInput(
                role="founder",
                icp_roles=("founder",),
                recipient_geo="US",
                sender_postal_address="1 Main St, Oakland, CA 94612",
                why_this_week="They posted a hiring complaint this week",
                why_this_week_confirmed=True,
                source=source,
                quote_text="we still run this workflow in a spreadsheet",
                quote_source="extracted",
                trigger_code="hiring_complaint",
                trigger_confidence="observed",
            )
        )
        assert result.void is True, source
        assert result.sum is None
        assert result.personalize != "ok"


def test_paste_fixture_can_pass_gates():
    result = score_contact(
        ScoreInput(
            role="founder",
            icp_roles=("founder",),
            recipient_geo="US",
            sender_postal_address="1 Main St, Oakland, CA 94612",
            why_this_week="They posted a hiring complaint this week",
            why_this_week_confirmed=True,
            source="paste",
            quote_text="we still run this workflow in a spreadsheet",
            quote_source="extracted",
        )
    )
    assert result.void is False
    assert result.personalize == "ok"
    draft = draft_connect(
        quote="we still run this workflow in a spreadsheet",
        trigger_code=None,
        trigger_confidence=None,
    )
    assert draft["blocked"] is False


def test_harvested_cannot_mark_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot18sent.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    load_company_os_app()
    from app import state as state_mod
    from app.gtm.relationships.service import OutreachError, create_draft, mark_sent, patch_contact
    from app.gtm.supymem.pipeline import ingest_leads
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    wid = ensure_desk(LOCAL_PERSON_ID).id
    ingested = ingest_leads(
        wid,
        [
            {
                "first_name": "Harvest",
                "email": "h@harvested.test",
                "email_confidence": "safe",
                "organization": "Harvest Co",
                "source": "web_icp_search",
                "title": "founder",
            }
        ],
        enrich=False,
    )
    cid = ingested["contacts"][0]["id"]
    patch_contact(
        wid,
        cid,
        {
            "why_this_week": "They posted a hiring complaint this week",
            "recipient_geo": "US",
            "role": "founder",
        },
    )
    draft = create_draft(wid, cid, kind="connect")
    with pytest.raises(OutreachError) as exc:
        mark_sent(wid, draft["id"], channel="linkedin_connect")
    assert exc.value.code == "harvested_quarantine"
