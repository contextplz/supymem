"""Synthetic clock/schedule/budget fixtures; no provider calls."""
from datetime import datetime, timezone

import pytest
from tests.company_os_harness import load_company_os_app


@pytest.fixture(autouse=True, scope="module")
def app_modules():
    load_company_os_app()


def test_one_time_and_explicit_recurring_trigger():
    from app.tasks.contracts import trigger_from_words, next_fire
    now = datetime(2026, 10, 8, 0, tzinfo=timezone.utc)
    assert trigger_from_words("Research my competitors", "Asia/Kolkata", now) == ("one_time", {})
    kind, trigger = trigger_from_words("Every morning at 8, research my competitors", "Asia/Kolkata", now)
    assert kind == "scheduled" and trigger["tz"] == "Asia/Kolkata"
    assert next_fire(trigger, now) == datetime(2026, 10, 8, 2, 30)
    assert trigger_from_words("When someone replies, draft a follow-up", "UTC", now)[0] == "event"
    with pytest.raises(ValueError, match="frequency"):
        trigger_from_words("Every blue moon check this", "UTC", now)


def test_dst_gap_is_skipped_and_overlap_fires_once():
    from app.tasks.contracts import next_fire
    trigger = {"rrule": "FREQ=DAILY;BYHOUR=2;BYMINUTE=30;BYSECOND=0", "tz": "America/New_York", "start_at": "2026-03-01T00:00:00+00:00"}
    assert next_fire(trigger, datetime(2026, 3, 8, 0, tzinfo=timezone.utc)) == datetime(2026, 3, 9, 6, 30)
    trigger["rrule"] = "FREQ=DAILY;BYHOUR=1;BYMINUTE=30;BYSECOND=0"
    assert next_fire(trigger, datetime(2026, 11, 1, 5, 31, tzinfo=timezone.utc)) == datetime(2026, 11, 2, 6, 30)
    assert next_fire(trigger, datetime(2026, 11, 1, 6, 1, tzinfo=timezone.utc)) == datetime(2026, 11, 2, 6, 30)


def test_relative_dates_use_founder_clock():
    from app.tasks.contracts import resolve_relative
    now = datetime(2026, 10, 8, 21, tzinfo=timezone.utc)  # Friday in India
    assert resolve_relative("tomorrow 9am", "Asia/Kolkata", now) == datetime(2026, 10, 10, 3, 30)
    assert resolve_relative("next Friday", "Asia/Kolkata", now) == datetime(2026, 10, 16, 3, 30)
    with pytest.raises(ValueError, match="absolute"):
        resolve_relative("soonish", "UTC", now)


def test_stuck_limits_and_monitor_exemption():
    from app.tasks.contracts import stuck
    same = [{"action": "a", "result": "same"}] * 4
    assert stuck(same)
    assert not stuck(same, waiting=True)
    assert stuck([{"error": "same"}] * 3)
    assert stuck([{"action": "a", "result": "1"}, {"action": "b", "result": "2"}] * 6)
    assert stuck([{"metric": 1}, {"metric": 1}])
    assert not stuck([{"metric": 1}, {"metric": 2}])


@pytest.mark.parametrize("code,kind", [("needs_auth", "needs_auth"), ("policy_refused", "policy"), ("content_filter", "policy"), ("budget_exhausted", "budget_exhausted"), ("approval_required", "needs_approval"), ("research_idea_missing", "needs_input"), ("outcome_uncertain", "needs_input"), ("budget_outcome_uncertain", "needs_input"), ("provider_timeout", "needs_input"), ("insufficient_credits", "needs_auth")])
def test_typed_blockers_never_expose_internal_code(code, kind):
    from app.tasks.contracts import blocker
    result = blocker(code)
    assert result["class"] == kind
    assert code not in result["reason"] and result["unblock_action"]


@pytest.mark.parametrize("code,kind,phrase", [
    ("pagebuilder_plan_rejected", "needs_input", "usable page plan"),
    ("pagebuilder_output_truncated", "needs_input", "usable page plan"),
    ("pagebuilder_outcome_uncertain", "needs_input", "without a confirmed result"),
    ("pagebuilder_provider_rejected", "needs_auth", "refused the request"),
    ("pagebuilder_provider_rate_limited", "needs_input", "temporarily busy"),
    ("pagebuilder_reference_unavailable", "needs_input", "safely saved"),
    ("pagebuilder_candidate_invalid", "needs_input", "source could not be verified"),
    ("pagebuilder_new_version_unavailable", "needs_input", "could not start"),
    ("pagebuilder_resources_changed", "needs_input", "resources changed"),
    ("pagebuilder_qa_failed", "needs_input", "failed these checks"),
    ("qa_checks_failed", "needs_input", "did not pass"),
    ("qa_task_stopped_before_report", "needs_input", "runner stopped"),
    ("build_compute_usage_unverified", "needs_input", "not a failed page-content check"),
    ("subscription_static_qa_deadline", "needs_input", "ran out of time"),
    ("qa_receipt_scope_invalid", "policy", "approved request"),
    ("pagebuilder_build_limit", "budget_exhausted", "limit"),
    ("pagebuilder_attempt_limit", "budget_exhausted", "eight-attempt safety limit"),
    ("hosting_provider_unavailable", "needs_input", "could not be confirmed"),
    ("waitlist_public_submission_unconfirmed", "needs_input", "could not be confirmed"),
])
def test_page_blockers_keep_codes_and_private_exception_prose_out_of_copy(code, kind, phrase):
    from app.tasks.contracts import blocker
    result = blocker(code + ": ProviderError secret=sk-private-id")
    assert result["class"] == kind and phrase in result["reason"]
    assert not any(value in str(result) for value in (code, "ProviderError", "sk-private-id"))


def test_task_deadline_phrase_is_resolved_by_code_and_past_dates_clarify():
    from app.tasks.contracts import estimated_deadline
    now = datetime(2026, 10, 8, 21, tzinfo=timezone.utc)
    assert estimated_deadline("Draft a note by tomorrow at 9am", "Asia/Kolkata", now, 900) == datetime(2026, 10, 10, 3, 30)
    with pytest.raises(ValueError, match="time has passed"):
        estimated_deadline("Draft by today 1am", "Asia/Kolkata", now, 900)


@pytest.mark.parametrize("detail", [
    "Give the exact subject and message I can review and send from my Gmail. Do not send anything.",
    "Write one interview email for review only. Do not send, post or schedule anything.",
])
def test_review_only_draft_and_founder_delivery_do_not_authorize_an_action(detail):
    from app.tasks.contracts import requires_reviewed_action

    assert not requires_reviewed_action("Draft one interview email for me to review", detail)


@pytest.mark.parametrize("detail", [
    "Draft an email and send it now.", "Review and send the email.",
    "Do not send, post or schedule anything; then send the email.",
    "Give me a message I can review and send; delete the original.",
    "Draft an email after sending the invitation.", "Draft a note and buy a laptop.",
])
def test_review_language_and_prohibitions_do_not_hide_separate_actions(detail):
    from app.tasks.contracts import requires_reviewed_action

    assert requires_reviewed_action("Draft a note", detail)
