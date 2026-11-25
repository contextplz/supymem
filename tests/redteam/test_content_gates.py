"""Red-team suite for the content engine.

With no human approving posts, these gates are the only reviewer. Warden treats
a passing run of this file as the prerequisite for L3 `post_public`, so every
case here is an attack the engine must refuse rather than a nice-to-have.

Each test states the attack, not the implementation, so a rewrite of the linter
still has to survive it.
"""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.content.brief import BriefError, validate  # noqa: E402
from app.content.safety.cadence import check_cadence, next_slot, promo_allowed  # noqa: E402
from app.content.safety.linter import lint, unregistered_numbers  # noqa: E402
from app.content.safety.originality import is_duplicate, simhash  # noqa: E402
from app.content.writer import template_draft  # noqa: E402

BRIEF = {
    "product": {
        "name": "Acme",
        "one_liner": "An AI GTM operator.",
        "differentiators": ["Runs the whole loop"],
        "proof": [],
    },
    "icp": {
        "segments": [
            {
                "name": "Seed founders",
                "pains": ["No time to prospect"],
                "objections": ["Feels spammy"],
                "triggers": ["Just raised"],
                "watering_holes": ["r/SaaS"],
            }
        ]
    },
    "competitors": [],
    "voice": {"banned_phrases": ["synergy"]},
    "claims": [
        {"text": "Discovery returned 3 leads in 53 seconds.", "evidence": "local run"}
    ],
    "cadence": {"value_to_promo": 4},
}

CLEAN = (
    "Most founders lose the first hour of the day to prospecting and never get "
    "it back. The fix that worked for us was writing the list the night before, "
    "so the morning is execution rather than deciding who to contact."
)


def _lint(body: str, platform: str = "linkedin", **kw):
    return lint(body, platform=platform, brief=BRIEF, **kw)


# ---------------------------------------------------------------------------
# Fabrication
# ---------------------------------------------------------------------------


def test_invented_statistic_is_refused() -> None:
    """The single highest-risk failure: a number nobody gave the engine."""
    result = _lint(CLEAN + " We increased reply rates by 340% last quarter.")
    assert not result.ok
    assert any(r.startswith("unregistered_claim") for r in result.reasons)


def test_registered_claim_is_allowed() -> None:
    body = (
        "We measured this on our own machine rather than guessing. Discovery "
        "returned 3 leads in 53 seconds, which was slower than we wanted but "
        "fast enough to keep the morning free for actual conversations."
    )
    assert _lint(body).ok


def test_percentages_never_pass_as_incidental() -> None:
    """Small integers are prose; a percentage is always a claim."""
    assert unregistered_numbers("we saw 3 reasons for this", BRIEF) == []
    assert unregistered_numbers("we saw 3% lift", BRIEF) == ["3%"]


def test_empty_claims_registry_forbids_all_statistics() -> None:
    bare = {**BRIEF, "claims": [], "product": {"name": "A", "one_liner": "B"}}
    result = lint(CLEAN + " Conversion rose 12%.", platform="linkedin", brief=bare)
    assert not result.ok


# ---------------------------------------------------------------------------
# Reading as machine-written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "In today's fast-paced world, this matters.",
        "Let's delve into why this happens.",
        "This is a game-changer for teams.",
        "Buckle up, because the harsh truth is coming.",
    ],
)
def test_slop_phrases_are_refused(phrase: str) -> None:
    result = _lint(CLEAN + " " + phrase)
    assert not result.ok
    assert any(r.startswith("slop_phrase") for r in result.reasons)


def test_founder_supplied_banned_phrase_is_enforced() -> None:
    result = _lint(CLEAN + " It is real synergy.")
    assert not result.ok
    assert "synergy" in " ".join(result.reasons)


@pytest.mark.parametrize(
    "bait",
    ["Comment below if you agree.", "Tag someone who needs this.", "Link in bio."],
)
def test_engagement_bait_is_refused(bait: str) -> None:
    result = _lint(CLEAN + " " + bait)
    assert not result.ok
    assert any(r.startswith("engagement_bait") for r in result.reasons)


def test_emoji_spam_is_refused() -> None:
    result = _lint(CLEAN + " 🚀🚀🔥🔥💯")
    assert not result.ok
    assert any(r.startswith("emoji_heavy") for r in result.reasons)


# ---------------------------------------------------------------------------
# Platform rules
# ---------------------------------------------------------------------------


def test_x_rejects_over_length() -> None:
    result = lint("x" * 281, platform="x", brief=BRIEF)
    assert not result.ok
    assert any(r.startswith("too_long") for r in result.reasons)


def test_x_rejects_links_because_they_are_billed_at_a_premium() -> None:
    body = "A short concrete note about prospecting mornings. https://example.com"
    result = lint(body, platform="x", brief=BRIEF)
    assert not result.ok
    assert any(r.startswith("too_many_links") for r in result.reasons)


def test_linkedin_rejects_links_because_they_suppress_reach() -> None:
    result = _lint(CLEAN + " https://example.com")
    assert not result.ok
    assert any(r.startswith("too_many_links") for r in result.reasons)


def test_linkedin_promo_allows_only_one_exact_founder_supplied_conversion_url() -> None:
    brief = {**BRIEF, "product": {**BRIEF["product"], "url": "https://acme.example/"}}
    body = "Acme has an early-access page. The application is still being tested. Join updates at https://acme.example/"
    assert lint(body, platform="linkedin", intent="promo", brief=brief).ok
    offsite = lint(body.replace("acme.example", "other.example"), platform="linkedin", intent="promo", brief=brief)
    assert "unapproved_conversion_link" in offsite.reasons
    extra = lint(body + " https://acme.example/", platform="linkedin", intent="promo", brief=brief)
    assert any(reason.startswith("too_many_links") for reason in extra.reasons)
    assert not lint(body, platform="linkedin", intent="value", brief=brief).ok


def test_digits_in_approved_conversion_url_are_not_claims() -> None:
    brief = {**BRIEF, "product": {**BRIEF["product"], "url": "https://acme.example/launch/2026/"}}
    body = "Acme has an early-access page. The application is still being tested. Join updates at https://acme.example/launch/2026/"
    result = lint(body, platform="linkedin", intent="promo", brief=brief)
    assert result.ok, result.reasons


def test_hashtag_spam_is_refused() -> None:
    result = _lint(CLEAN + " #growth #saas #sales #founders #gtm")
    assert not result.ok
    assert any(r.startswith("too_many_hashtags") for r in result.reasons)


def test_reddit_refuses_promotional_intent_outright() -> None:
    result = lint("x" * 400, platform="reddit", brief=BRIEF, intent="promo")
    assert not result.ok
    assert "promo_on_reddit" in result.reasons


def test_reddit_enforces_live_fetched_subreddit_rules() -> None:
    """A rule fetched from the subreddit blocks the post, not a stale snapshot."""
    body = "y" * 300 + " no self promotion here"
    result = lint(
        body,
        platform="reddit",
        brief=BRIEF,
        subreddit_rules=["no self promotion"],
    )
    assert not result.ok
    assert any(r.startswith("subreddit_rule") for r in result.reasons)


def test_thin_post_is_refused() -> None:
    result = _lint("Prospecting is hard.")
    assert not result.ok
    assert any(r.startswith("too_short") for r in result.reasons)


# ---------------------------------------------------------------------------
# Repetition
# ---------------------------------------------------------------------------


def test_reposting_the_same_body_is_caught() -> None:
    duplicate, _ = is_duplicate(CLEAN, [simhash(CLEAN)])
    assert duplicate


def test_trivial_rewording_does_not_defeat_the_duplicate_gate() -> None:
    reworded = CLEAN.replace("Most founders", "Most builders").replace("fix", "answer")
    duplicate, distance = is_duplicate(reworded, [simhash(CLEAN)])
    assert duplicate, f"hamming {distance} slipped through"


def test_genuinely_different_posts_are_not_blocked() -> None:
    other = (
        "Pricing pages are where most trials die. We rewrote ours around the "
        "single question buyers actually asked on calls, and stopped listing "
        "features nobody weighed when deciding."
    )
    duplicate, _ = is_duplicate(other, [simhash(CLEAN)])
    assert not duplicate


# ---------------------------------------------------------------------------
# Cadence and rhythm
# ---------------------------------------------------------------------------


def test_daily_cap_is_enforced() -> None:
    verdict = check_cadence(
        platform="x", daily_cap=3, posts_today=3, last_post_at=None
    )
    assert not verdict.ok
    assert verdict.reason.startswith("daily_cap_reached")


def test_burst_posting_is_refused() -> None:
    from core.clock import utcnow

    verdict = check_cadence(
        platform="x", daily_cap=5, posts_today=1, last_post_at=utcnow()
    )
    assert not verdict.ok
    assert verdict.reason.startswith("too_soon")


def test_slots_are_jittered_rather_than_on_a_fixed_interval() -> None:
    """Identical spacing between posts is itself an automation fingerprint."""
    from core.clock import utcnow

    base = utcnow()
    slots = {next_slot(platform="x", after=base).isoformat() for _ in range(12)}
    assert len(slots) > 1


def test_slots_land_inside_working_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.clock import utcnow

    monkeypatch.setenv("CONTENT_TZ_OFFSET_MINUTES", "0")
    monkeypatch.setenv("CONTENT_ACTIVE_HOUR_START", "8")
    monkeypatch.setenv("CONTENT_ACTIVE_HOUR_END", "21")
    for _ in range(25):
        slot = next_slot(platform="x", after=utcnow())
        assert 8 <= slot.hour < 21, slot


# ---------------------------------------------------------------------------
# Brief integrity
# ---------------------------------------------------------------------------


def test_brief_without_an_icp_is_rejected() -> None:
    with pytest.raises(BriefError) as exc:
        validate({"product": {"name": "A", "one_liner": "B"}})
    assert any("icp.segments" in e for e in exc.value.errors)


def test_brief_without_a_product_is_rejected() -> None:
    with pytest.raises(BriefError) as exc:
        validate({"icp": {"segments": [{"name": "x"}]}})
    assert any("product.name" in e for e in exc.value.errors)


def test_brief_reports_every_problem_at_once() -> None:
    with pytest.raises(BriefError) as exc:
        validate({"competitors": [{"url": "https://x.com"}]})
    assert len(exc.value.errors) >= 3


def test_valid_brief_normalizes_without_loss() -> None:
    out = validate(BRIEF)
    assert out["product"]["name"] == "Acme"
    assert out["icp"]["segments"][0]["pains"] == ["No time to prospect"]
    assert out["claims"][0]["evidence"] == "local run"


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------


def test_planner_rationale_never_reaches_a_post_body() -> None:
    """`rationale` is the planner talking to itself and is not publishable."""
    topic = {
        "hook": "No time to prospect",
        "angle": "Why prospecting slips",
        "rationale": "Named pain for Seed founders; recognition post, no claims.",
        "icp_segment": "Seed founders",
        "intent": "value",
    }
    body = template_draft(topic, BRIEF, "linkedin")
    assert "recognition post" not in body
    assert "Named pain" not in body


def test_value_posts_do_not_mention_the_product() -> None:
    topic = {
        "hook": "No time to prospect",
        "angle": "Why prospecting slips",
        "rationale": "",
        "icp_segment": "Seed founders",
        "intent": "value",
    }
    body = template_draft(topic, BRIEF, "linkedin")
    assert "Acme" not in body
    assert "Runs the whole loop" not in body


# ---------------------------------------------------------------------------
# Promo budget
# ---------------------------------------------------------------------------


def test_promo_ratio_query_is_permissive_with_no_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    dbfile = tmp_path / "content_ratio.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{dbfile}")
    monkeypatch.setenv("DB_URL", f"sqlite:///{dbfile}")
    from app.db.models import Base
    from app.state import db, reset_db_factory

    reset_db_factory()
    Base.metadata.create_all(db().kw["bind"])
    verdict = promo_allowed(workspace_id="w1", platform="x", ratio=4)
    assert verdict.ok


@pytest.mark.parametrize("claim", [
    "I’m talking with independent groomers who use paper diaries.",
    "I keep hearing that no-shows eat into the day.",
    "I have been talking to groomers about their no-shows.",
    "We interviewed independent groomers about booking reminders.",
])
def test_invented_first_person_research_is_not_supported_by_an_audience_hypothesis(claim):
    brief = {**BRIEF, "claims": [], "product": {**BRIEF["product"], "proof": []},
             "icp": {"segments": [{"name": "Independent groomers", "pains": ["No-shows may leave slots empty"]}]}}
    body = claim + " What do you currently do to remind clients? I have no finished product and would like to learn about your own workflow without any client names or private messages."
    result = lint(body, platform="reddit", brief=brief)
    assert "unregistered_founder_research" in result.reasons and not result.ok


def test_registered_exact_research_statement_and_honest_discovery_questions_remain_allowed():
    claim = "I spoke with independent groomers about their reminder workflow"
    brief = {**BRIEF, "claims": [{"text": claim, "evidence": "Founder-reported conversation notes, unverified."}]}
    body = claim + ". What do you currently do to remind clients? I would like to learn about your own general workflow without any client names or private messages. This is a hypothesis, not a product result."
    assert lint(body, platform="reddit", brief=brief).ok
    honest = "I haven't interviewed a groomer yet. I am exploring a reminder-workflow hypothesis. If you use a paper diary, what happened at the last cancellation and what, if anything, was frustrating? No client names or private messages needed."
    assert lint(honest, platform="reddit", brief={**BRIEF, "claims": []}).ok


def test_negated_registered_research_does_not_support_a_positive_assertion():
    brief = {**BRIEF, "claims": [{"text": "I have not interviewed a groomer yet", "evidence": "No interviews"}]}
    body = "I interviewed a groomer. What do you currently do to remind clients? I would like to learn about your own general workflow without any client names or private messages. This is a hypothesis, not a product result."
    assert "unregistered_founder_research" in lint(body, platform="reddit", brief=brief).reasons


def test_invented_optional_profile_link_is_refused():
    body = "I am exploring a reminder-workflow hypothesis. What do you currently do to remind clients? If you are open to a short conversation, there is an optional link in my profile. I have no finished product and am not selling anything."
    assert any(reason.startswith("engagement_bait:") for reason in lint(body, platform="reddit", brief=BRIEF).reasons)


def test_warning_about_a_research_claim_is_not_registered_evidence():
    claim = "I keep hearing that no-shows eat into the day"
    brief = {**BRIEF, "claims": [{"text": "Do not claim " + claim, "evidence": "No interviews"}]}
    body = claim + ". What do you currently do to remind clients? I would like to learn about your own general workflow without client names or private messages. This is a hypothesis, not a product result."
    assert "unregistered_founder_research" in lint(body, platform="reddit", brief=brief).reasons


@pytest.mark.parametrize("sentence", [
    "After I have interviewed consenting adults, I will share what I learn.",
    "Have I talked to enough people to validate this hypothesis?",
    "I will talk to consenting adult providers about their general workflow.",
])
def test_explicit_contingent_future_and_research_questions_do_not_claim_completed_interviews(sentence):
    from app.content.safety.linter import unregistered_research_experience
    assert not unregistered_research_experience(sentence, {"claims": []})


def test_actual_past_research_with_future_sharing_still_requires_evidence():
    from app.content.safety.linter import unregistered_research_experience
    assert unregistered_research_experience("After I interviewed groomers, I will share what I learned.", {"claims": []})


def test_future_qualification_does_not_exempt_another_present_research_claim():
    from app.content.safety.linter import unregistered_research_experience
    assert unregistered_research_experience("After I have interviewed groomers, I will share more because I keep hearing about their cancellations.", {"claims": []})
