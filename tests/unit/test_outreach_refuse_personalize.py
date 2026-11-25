"""OT-15: refuse auto-personalize without a quote or observed trigger."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.score import (
    PersonalizeRefused,
    ScoreInput,
    draft_connect,
    personalize_decision,
    require_personalize_ok,
    score_contact,
)


def test_no_quote_and_no_observed_trigger_is_not_ok():
    mode = personalize_decision(quote=None, trigger_code=None, trigger_confidence=None)
    assert mode in {"skip", "content_play"}
    assert mode != "ok"


def test_named_human_defaults_to_skip():
    assert (
        personalize_decision(
            quote="",
            trigger_code=None,
            trigger_confidence="inferred",
            named_human=True,
        )
        == "skip"
    )


def test_layer_a_only_is_content_play():
    assert (
        personalize_decision(
            quote=None,
            trigger_code=None,
            trigger_confidence=None,
            named_human=False,
        )
        == "content_play"
    )


def test_inferred_trigger_without_quote_is_not_ok():
    mode = personalize_decision(
        quote=None,
        trigger_code="funding",
        trigger_confidence="inferred",
    )
    assert mode != "ok"


def test_extracted_quote_allows_ok():
    assert (
        personalize_decision(
            quote="we still run payroll in a spreadsheet",
            trigger_code=None,
            trigger_confidence=None,
        )
        == "ok"
    )


def test_observed_trigger_allows_ok():
    assert (
        personalize_decision(
            quote=None,
            trigger_code="job_change",
            trigger_confidence="observed",
        )
        == "ok"
    )


def test_connect_draft_blocked_without_quote_or_trigger():
    out = draft_connect(quote=None, trigger_code=None, trigger_confidence=None)
    assert out["blocked"] is True
    assert out["mode"] != "ok"
    assert out["body"] is None
    with pytest.raises(PersonalizeRefused) as exc:
        require_personalize_ok(
            quote=None,
            trigger_code=None,
            trigger_confidence=None,
            draft_kind="connect",
        )
    assert exc.value.mode in {"skip", "content_play"}


def test_score_contact_refuses_personalize_without_evidence():
    result = score_contact(
        ScoreInput(
            role="founder",
            icp_roles=("founder",),
            recipient_geo="US",
            sender_postal_address="1 Main St, Oakland, CA 94612",
            why_this_week="They posted a hiring complaint this week",
            why_this_week_confirmed=True,
            quote_text=None,
            trigger_code=None,
            named_human=True,
        )
    )
    assert result.personalize != "ok"
    assert "PERSONALIZE skip" in result.explain
