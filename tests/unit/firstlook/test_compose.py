"""Composed first-look pages are unique per idea and self-contained."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_two_ideas_get_different_pages() -> None:
    from app.firstlook.brandkit import build_brandkit
    from app.firstlook.compose import compose_html, lint_html, plan_page
    from app.firstlook.copy import fallback_copy

    idea_a = "A marketplace where people buy and sell restored vintage synthesizers"
    idea_b = "Same-evening vet house-calls for anxious city dog owners"
    html_a, name_a, plan_a = compose_html(
        brand=build_brandkit(idea_a, venture_name="SynthRest"),
        copy=fallback_copy(idea_a, audience="synth owners"),
        idea_text=idea_a,
        version=1,
        interest_action="/w/wsp_a/interest",
    )
    html_b, name_b, plan_b = compose_html(
        brand=build_brandkit(idea_b, venture_name="NightVet"),
        copy=fallback_copy(idea_b, audience="dog owners"),
        idea_text=idea_b,
        version=1,
        interest_action="/w/wsp_b/interest",
    )
    assert html_a != html_b
    assert name_a.startswith("composed:")
    assert name_b.startswith("composed:")
    assert plan_a["layout"] in {plan_page(idea_a)["layout"]}
    assert "data-layout=" in html_a
    assert "SynthRest" in html_a
    assert "NightVet" in html_b
    assert "I assumed:" in html_a
    assert not lint_html(html_a)
    assert not lint_html(html_b)
    assert "prefers-reduced-motion" in html_a
    assert "https://" not in html_a
    assert 'rel="stylesheet"' not in html_a.lower()


def test_variant_index_changes_the_look() -> None:
    from app.firstlook.brandkit import build_brandkit
    from app.firstlook.compose import compose_html, plan_page
    from app.firstlook.copy import fallback_copy

    idea = "A weekly digest of local farm produce for city cooks"
    copy = fallback_copy(idea, audience="city cooks")
    brand0 = build_brandkit(idea, venture_name="FarmNote", variant_index=0)
    brand1 = build_brandkit(idea, venture_name="FarmNote", variant_index=1)
    html0, _, plan0 = compose_html(
        brand=brand0, copy=copy, idea_text=idea, version=1, variant_index=0
    )
    html1, _, plan1 = compose_html(
        brand=brand1, copy=copy, idea_text=idea, version=2, variant_index=1
    )
    assert html0 != html1
    assert plan0 != plan_page(idea, variant_index=1)
    assert plan1["variant_index"] == 1
