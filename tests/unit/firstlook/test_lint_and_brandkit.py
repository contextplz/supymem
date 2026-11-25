"""First-look lint + brandkit hermetic tests."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)


def test_lint_rejects_fabricated_social_proof() -> None:
    from app.firstlook.lint import lint_copy

    report = lint_copy(
        {
            "headline": "Ship faster",
            "subhead": "Trusted by 10,000 teams worldwide",
            "points": [
                {"title": "One", "body": "Do the first thing clearly."},
                {"title": "Two", "body": "Do the second thing clearly."},
                {"title": "Three", "body": "Do the third thing clearly."},
            ],
            "cta": {"label": "Join waitlist"},
            "assumptions": ["Founders care about speed"],
        },
        idea_text="A tool that helps teams ship code",
    )
    assert not report.ok
    assert any(f.rule == "social_proof" for f in report.failures)


def test_lint_accepts_plain_honest_copy() -> None:
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
            "cta": {"label": "Leave your email"},
            "assumptions": ["City cooks want local produce"],
        },
        idea_text="A weekly digest of local farm produce for city cooks",
    )
    assert report.ok, report.reasons


def test_brandkit_is_deterministic() -> None:
    from app.firstlook.brandkit import build_brandkit

    idea = "A tool that helps freelancers invoice clients in India"
    a = build_brandkit(idea, venture_name="InvoiceDesk")
    b = build_brandkit(idea, venture_name="InvoiceDesk")
    assert a.to_dict() == b.to_dict()
    assert a.hash == b.hash
    assert a.colors.ink
    assert a.wordmark == "InvoiceDesk"
