"""Rendered first-look HTML is self-contained (45 §6, §10)."""

from __future__ import annotations

import re

from tests.company_os_harness import load_company_os_app

load_company_os_app()

_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["']https?://|@import\s+|url\(\s*["']?https?://""",
    re.I,
)


def test_render_is_self_contained_and_has_assumptions() -> None:
    from app.firstlook.brandkit import build_brandkit
    from app.firstlook.copy import fallback_copy
    from app.firstlook.render import render_html

    idea = "A marketplace for restoring vintage synthesizers"
    brand = build_brandkit(idea, venture_name="SynthRest")
    copy = fallback_copy(idea, audience="synth owners")
    html, template = render_html(
        brand=brand,
        copy=copy,
        archetype="marketplace",
        idea_text=idea,
        version=1,
        stage="first_look",
        interest_action="/w/wsp_test/interest",
    )
    assert template == "marketplace"
    assert "<!DOCTYPE html>" in html
    assert "SynthRest" in html
    assert "I assumed:" in html
    assert idea in html
    assert "only works on your machine" in html.lower() or "Your words:" in html
    assert not _EXTERNAL.search(html)
    assert "<img" not in html.lower() or "http" not in html.lower()
    # No stylesheet / webfont links
    assert 'rel="stylesheet"' not in html.lower()
    assert "@font-face" not in html.lower()


def test_render_generic_fallback_template() -> None:
    from app.firstlook.brandkit import build_brandkit
    from app.firstlook.copy import fallback_copy
    from app.firstlook.render import render_html

    idea = "Something unclear and new"
    html, template = render_html(
        brand=build_brandkit(idea),
        copy=fallback_copy(idea),
        archetype="unknown_thing",
        idea_text=idea,
        version=2,
    )
    assert template == "generic"
    assert "Something unclear" in html or idea in html


def test_fallback_copy_names_competitors_not_dicts() -> None:
    from app.firstlook.copy import fallback_copy

    copy = fallback_copy(
        "An AI-powered Form Operator SaaS that files visa forms",
        audience="consumers",
        research={
            "competitors": [
                {"name": "Formstack", "website": "https://formstack.example"},
                {"name": "licl", "website": "http://example.com"},
            ]
        },
    )
    market = copy["points"][2]["body"]
    assert "{" not in market
    assert "Formstack" in market
    assert "who care about" not in copy["subhead"]
