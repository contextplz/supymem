"""Concept-page honesty: no present-tense claims about software that does not exist (beta PR-1)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

IDEA = "A tool that helps small gyms collect late membership payments"


def _copy(headline, subhead, bodies, how=(), cta="Join the waitlist"):
    return {
        "headline": headline,
        "subhead": subhead,
        "points": [{"title": "Why this", "body": b} for i, b in enumerate(bodies)],
        "how_it_works": [{"step": i + 1, "text": t} for i, t in enumerate(how)],
        "cta": {"label": cta, "kind": "waitlist"},
        "assumptions": ["Gym owners chase payments by hand."],
    }


GOOD = _copy(
    "We're building a calmer way for gyms to collect late payments",
    "Early idea for small gym owners. Join the waitlist to hear first.",
    [
        "The idea is a simple reminder flow gym owners can review.",
        "We want to start with one gym and learn what actually helps.",
        "Nothing here is final. We are talking to owners before writing code.",
    ],
    how=["We plan to interview gym owners.", "Then we will build the smallest useful slice."],
)

BAD = [
    _copy("Acme tracks every late payment", "For gym owners", ["Pending.", "Pending.", "Pending."]),
    _copy("Late payments, sorted", "Our app automatically sends reminders to members.", ["a.", "b.", "c."]),
    _copy("Late payments, sorted", "For gym owners", ["You can book a call in seconds.", "x.", "y."]),
    _copy("Late payments, sorted", "For gym owners", ["It syncs with your member list.", "x.", "y."]),
    _copy("Late payments, sorted", "For gym owners", ["We help gyms get paid faster.", "x.", "y."]),
]


def test_concept_copy_passes():
    from app.firstlook.lint import capability_claims, lint_copy

    assert capability_claims("\n".join([GOOD["headline"], GOOD["subhead"], *[p["body"] for p in GOOD["points"]]])) == []
    report = lint_copy(GOOD, idea_text=IDEA)
    assert report.ok, report.reasons


@pytest.mark.parametrize("copy", BAD)
def test_present_tense_product_claims_fail(copy):
    from app.firstlook.lint import lint_copy

    report = lint_copy(copy, idea_text=IDEA)
    assert any(f.rule == "capability_claim" for f in report.failures), report.reasons


def test_allowlisted_waitlist_phrases_are_not_claims():
    from app.firstlook.lint import capability_claims

    assert capability_claims("Join the waitlist.") == []
    assert capability_claims("We're building a reminder tool. Nothing here is final.") == []


@pytest.mark.parametrize("copy", ["RecallDesk is built to send appointment reminders.",
    "RecallDesk is designed to send appointment reminders.", "Get an SMS follow-up.",
    "Join the waitlist for SMS follow-up."])
def test_prelaunch_offer_phrasing_passes(copy):
    from app.firstlook.lint import capability_claims
    assert capability_claims(copy) == []


def test_capability_claims_return_all_exact_sentences_without_truncation():
    from app.firstlook.lint import capability_claims, lint_copy
    sentences = ["RecallDesk sends an SMS or WhatsApp follow‑up when an appointment is marked no‑show or cancelled in your practice system.",
                 "RecallDesk detects missed appointments."]
    assert len(sentences[0]) > 120
    assert capability_claims(" ".join(sentences)) == sentences
    report = lint_copy(_copy("Appointment follow-up", "For dental clinics", sentences + ["Request updates."]))
    assert [failure.detail for failure in report.failures if failure.rule == "capability_claim"] == sentences


@pytest.mark.parametrize("sentence,expected", [
    ("RecallDesk automatically detects cancellations.", "RecallDesk is built to automatically detect cancellations."),
    ("RecallDesk notifies patients.", "RecallDesk is built to notify patients."),
    ("RecallDesk matches appointment times.", "RecallDesk is built to match appointment times."),
    ("RecallDesk monitors your schedule and reaches out automatically.", "RecallDesk is built to monitor your schedule and reach out automatically."),
    ("RecallDesk detects cancellations and automatically sends messages.", "RecallDesk is built to detect cancellations and automatically send messages."),
    ("You can get a follow-up.", "Join the waitlist to get a follow-up."),
    ("We send appointment reminders.", "We're building RecallDesk to send appointment reminders."),
    ("Details get lost between shifts.", "Details get lost between shifts."),
    ("RecallDesk integrates with Salesforce.", "RecallDesk integrates with Salesforce."),
    ("RecallDesk provides certification.", "RecallDesk provides certification."),
])
def test_only_product_action_sentences_are_rephrased(sentence, expected):
    from app.firstlook.lint import capability_offer_rewrite
    assert capability_offer_rewrite(sentence, "RecallDesk") == expected
