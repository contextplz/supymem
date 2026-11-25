"""OT-17: honesty flags — calendar and pitch in connect are blocked."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.content.safety.linter import BAIT_PHRASES, SLOP_PHRASES  # noqa: E402
from app.gtm.relationships.honesty import check_draft  # noqa: E402


def test_connect_calendly_blocked():
    result = check_draft(
        body="Hey — book a time: https://calendly.com/alex/15min",
        kind="connect",
        ask_rung="none",
        product_name=None,
        hook="we still run payroll in a spreadsheet",
    )
    assert result.blocked is True
    assert "premature_calendar" in result.flags
    assert result.overridable is False


def test_connect_product_name_blocked():
    result = check_draft(
        body="Hey, try Supymem for your outreach stack",
        kind="connect",
        ask_rung="connect",
        product_name="Supymem",
        hook="we still run payroll in a spreadsheet",
    )
    assert result.blocked is True
    assert "pitch_in_connection_request" in result.flags


@pytest.mark.parametrize("field", ["{{first_name}}", "{{ company }}", "[Company]", "<name>"])
def test_leaked_merge_fields_require_review(field):
    result = check_draft(body=f"Hi {field}, I saw your payroll post.", kind="value_return",
                         ask_rung="value_return", product_name=None, hook="payroll")
    assert "merge_field" in result.flags
    assert result.overridable and not result.blocked


@pytest.mark.parametrize("body", [
    "What slows payroll down? Would you try a demo?",
    "Please reply with a time. Also send your feedback.",
    "Book a call and share your feedback.",
    "Could you try the demo and send feedback?",
    "Open to a call? Please join the waitlist.",
    "Tell me your thoughts. Book a call.",
    "Reply with a time, join the waitlist.",
    "Please review the guide and download the demo.",
])
def test_multiple_asks_require_review(body):
    result = check_draft(body=body, kind="value_return", ask_rung="value_return",
                         product_name=None, hook="payroll")
    assert "multiple_asks" in result.flags
    assert result.overridable and not result.blocked


@pytest.mark.parametrize("body", [
    "What slows payroll down?",
    "Please reply with a time.",
    "Would you be open to a call?",
    "Open to a call?",
    "Can you book a call or reply with a time?",
    "I joined a payroll team and shared a guide.",
    "Sharing a [payroll guide](https://example.com/guide), no meeting ask.",
])
def test_one_or_no_ask_is_not_flagged(body):
    result = check_draft(body=body, kind="value_return", ask_rung="value_return",
                         product_name=None, hook="payroll")
    assert "multiple_asks" not in result.flags
    assert "merge_field" not in result.flags


@pytest.mark.parametrize("phrase", SLOP_PHRASES)
def test_shared_slop_phrases_require_review(phrase):
    result = check_draft(body=f"Payroll: {phrase.upper()}.", kind="value_return",
                         ask_rung="value_return", product_name=None, hook="payroll")
    assert any(flag.startswith("slop_phrase:") for flag in result.flags)
    assert result.overridable and not result.blocked


@pytest.mark.parametrize("phrase", BAIT_PHRASES)
def test_shared_bait_phrases_require_review(phrase):
    result = check_draft(body=f"Payroll: {phrase.upper()}.", kind="value_return",
                         ask_rung="value_return", product_name=None, hook="payroll")
    assert any(flag.startswith("engagement_bait:") for flag in result.flags)
    assert result.overridable and not result.blocked


def test_quality_flags_do_not_override_hard_blocks():
    result = check_draft(body="Hi {{first_name}}, book a call at https://calendly.com/alex.",
                         kind="connect", ask_rung="none", product_name=None, hook="payroll")
    assert {"merge_field", "premature_calendar"} <= set(result.flags)
    assert result.blocked and not result.overridable


def test_quality_flags_surface_in_rendered_drafts():
    from app.gtm.relationships.drafts import render_draft

    result = render_draft(kind="question", first_name="{{first_name}}", quote="delve into payroll",
                          trigger_code=None, trigger_confidence="observed", company="Payroll Co",
                          ask_rung="connect")
    assert "merge_field" in result["honesty"]["flags"]
    assert "slop_phrase:delve into" in result["honesty"]["flags"]
    assert result["honesty"]["overridable"]


def test_ask_with_its_own_logistics_is_one_ask():
    from app.gtm.relationships import honesty
    body = ("Hi Ada, I'm researching this workflow and would value your perspective. "
            "Would you be open to a 15-minute call? Reply with a time.")
    result = honesty.check_draft(body=body, kind="email", ask_rung="call", product_name=None, hook=None)
    assert "multiple_asks" not in result.flags
    two = "Would you be open to a call next week? I build tools for estimators. Also, could you share your pricing sheet?"
    assert "multiple_asks" in honesty.check_draft(body=two, kind="email", ask_rung="call", product_name=None, hook=None).flags
