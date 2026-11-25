"""Content linter hard-fails fabricated social proof (45 §6, LD-60)."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_lint_rejects_trusted_by_ten_thousand_teams() -> None:
    from app.firstlook.lint import lint_copy

    report = lint_copy(
        {
            "headline": "Ship faster with less noise",
            "subhead": "Trusted by 10,000 teams worldwide",
            "points": [
                {"title": "One", "body": "Do the first thing clearly."},
                {"title": "Two", "body": "Do the second thing clearly."},
                {"title": "Three", "body": "Do the third thing clearly."},
            ],
            "cta": {"label": "Join waitlist", "kind": "waitlist"},
            "assumptions": ["Founders care about speed"],
        },
        idea_text="A tool that helps teams ship code",
    )
    assert not report.ok
    assert any(f.rule == "social_proof" for f in report.failures)


def test_lint_rejects_market_size_and_missing_assumptions() -> None:
    from app.firstlook.lint import lint_copy

    report = lint_copy(
        {
            "headline": "Own the $4.2B market",
            "subhead": "For operators who need a better queue",
            "points": [
                {"title": "One", "body": "First point without fluff."},
                {"title": "Two", "body": "Second point without fluff."},
                {"title": "Three", "body": "Third point without fluff."},
            ],
            "cta": {"label": "Notify me", "kind": "notify"},
        },
        idea_text="A queue for neighbourhood bakers",
    )
    assert not report.ok
    rules = {f.rule for f in report.failures}
    assert "schema" in rules  # missing assumptions
    assert "numeric_claim" in rules or "social_proof" in rules or "superlatives" in rules


def test_lint_accepts_plain_copy() -> None:
    from app.firstlook.lint import lint_copy

    report = lint_copy(
        {
            "headline": "A weekly digest of local farm produce",
            "subhead": "For people who want to know what's in season nearby",
            "points": [
                {"title": "See this week", "body": "A short list of what farms have nearby."},
                {"title": "Your tastes", "body": "Tell us what you cook so notes stay useful."},
                {"title": "A short note", "body": "Get a note when something matches."},
            ],
            "cta": {"label": "Leave your email", "kind": "waitlist"},
            "assumptions": ["City cooks want local produce"],
        },
        idea_text="A weekly digest of local farm produce for city cooks",
    )
    assert report.ok, report.reasons
