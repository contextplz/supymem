"""OT-14: eight factors summed 0–32 and send-priority bands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.score import (
    FACTOR_KEYS,
    ScoreInput,
    SendPriorityBand,
    band_for_sum,
    score_contact,
    trigger_score,
)

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _base(**kwargs: object) -> ScoreInput:
    data: dict[str, object] = {
        "role": "founder",
        "icp_roles": ("founder", "ceo"),
        "recipient_geo": "US",
        "channel": "email",
        "send_mode": "human_clipboard",
        "sender_postal_address": "1 Main St, Oakland, CA 94612",
        "why_this_week": "They posted a hiring complaint this week",
        "why_this_week_confirmed": True,
        "source": "paste",
        "company_name": "Acme",
        "workflow_evidenced": True,
        "status_quo_evidenced": True,
        "trigger_code": "hiring_complaint",
        "trigger_confidence": "observed",
        "trigger_observed_at": NOW - timedelta(days=3),
        "now": NOW,
        "quote_text": "we still run this workflow in a spreadsheet",
        "quote_source": "extracted",
        "sku_key": "2026-09-bench-01",
        "sku_status": "approved",
        "mutual_count": 3,
        "sender_seniority": "founder",
        "recipient_seniority": "founder",
        "ask_rung": "connect",
        "prior_warmth": "inbound",
    }
    data.update(kwargs)
    return ScoreInput(**data)  # type: ignore[arg-type]


def test_max_sum_is_32_not_28():
    result = score_contact(_base())
    assert result.void is False
    assert result.sum == 32
    assert result.band is SendPriorityBand.PRIORITY
    assert result.band.value == "priority"
    assert isinstance(result.band, SendPriorityBand)
    assert all(result.factors[k] == 4 for k in FACTOR_KEYS)


def test_missing_evidence_scores_zero_not_two():
    result = score_contact(
        _base(
            workflow_evidenced=False,
            status_quo_evidenced=False,
            trigger_code=None,
            trigger_observed_at=None,
            quote_text=None,
            quote_source=None,
            sku_key=None,
            sku_status=None,
            i_actually_know_them=False,
            mutual_count=0,
            sender_seniority=None,
            recipient_seniority=None,
            ask_rung="call_15",
            prior_warmth=None,
            role="intern",
            buyer_tag=None,
            economic_buyer_roles=(),
        )
    )
    # G1 fails (intern) unless we keep founder... intern fails G1 so void.
    assert result.void is True
    assert result.sum is None
    assert result.factors["problem"] == 0
    assert result.factors["trigger"] == 0
    assert result.factors["quote"] == 0
    assert result.factors["cred"] == 0
    assert result.factors["warmth"] == 0
    assert result.factors["ask"] == 0
    assert result.factors["status"] == 0


def test_bands_are_priority_not_probability():
    assert band_for_sum(0) is SendPriorityBand.DROP
    assert band_for_sum(7) is SendPriorityBand.DROP
    assert band_for_sum(8) is SendPriorityBand.WATCH
    assert band_for_sum(15) is SendPriorityBand.WATCH
    assert band_for_sum(16) is SendPriorityBand.PURSUE
    assert band_for_sum(23) is SendPriorityBand.PURSUE
    assert band_for_sum(24) is SendPriorityBand.PRIORITY
    assert band_for_sum(32) is SendPriorityBand.PRIORITY
    assert SendPriorityBand.__name__ == "SendPriorityBand"


def test_explain_format_and_why_now_sentence():
    result = score_contact(_base())
    lines = result.explain.splitlines()
    assert lines[0].startswith("GATES G1=pass")
    assert "VOID=0 SUM=32/32 BAND=priority" in lines[1]
    assert lines[2].startswith("F problem=4")
    assert "WHY_MACHINE" in lines[3]
    assert "PERSONALIZE ok" in result.explain
    assert result.why_now.startswith("Why this person, why now:")
    assert "trigger=hiring_complaint" in result.why_now


def test_void_sets_sum_null_and_blocked_band():
    result = score_contact(_base(do_not_contact=True))
    assert result.void is True
    assert result.sum is None
    assert result.band is SendPriorityBand.BLOCKED
    assert "VOID=1 SUM=NA/32 BAND=blocked" in result.explain
    assert result.why_now.startswith("Do not send:")


def test_trigger_decay():
    code = "funding"
    assert (
        trigger_score(
            trigger_code=code,
            confidence="observed",
            observed_at=NOW - timedelta(days=14),
            now=NOW,
        )
        == 4
    )
    assert (
        trigger_score(
            trigger_code=code,
            confidence="observed",
            observed_at=NOW - timedelta(days=15),
            now=NOW,
        )
        == 2
    )
    assert (
        trigger_score(
            trigger_code=code,
            confidence="observed",
            observed_at=NOW - timedelta(days=45),
            now=NOW,
        )
        == 2
    )
    assert (
        trigger_score(
            trigger_code=code,
            confidence="observed",
            observed_at=NOW - timedelta(days=46),
            now=NOW,
        )
        == 1
    )
    assert (
        trigger_score(
            trigger_code=code,
            confidence="observed",
            observed_at=NOW - timedelta(days=91),
            now=NOW,
        )
        == 0
    )
    assert (
        trigger_score(
            trigger_code=code,
            confidence="inferred",
            observed_at=NOW - timedelta(days=3),
            now=NOW,
        )
        == 2
    )
    assert trigger_score(trigger_code=None, confidence="observed", observed_at=NOW, now=NOW) == 0
