"""Venture titles are a short product name, not a clipped sentence."""

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.intake.naming import derive_name, title_from_idea


def test_quoted_product_name_wins():
    idea = (
        'An AI-powered “Form Operator” SaaS that turns messy intake into a '
        "clean operating system for a team."
    )
    assert title_from_idea(idea) == "Form Operator"
    assert derive_name(None, idea) == "Form Operator"


def test_marketplace_becomes_a_name_not_an_ellipsis():
    idea = "A marketplace for restoring vintage synthesizers and analog tape machines"
    name = title_from_idea(idea)
    assert "…" not in name
    assert name == "Vintage Synthesizers"
    assert derive_name(None, idea) == name


def test_ramble_does_not_become_first_clause():
    idea = (
        "I was thinking of building a platform where you kind of sing the song "
        "back and friends can join in"
    )
    name = title_from_idea(idea)
    assert "I was thinking" not in name
    assert "…" not in name
    assert name == "Song Back"


def test_explicit_name_wins():
    assert derive_name("Chorus", "I was thinking of building a platform") == "Chorus"


def test_short_founder_category_remains_a_descriptive_working_name():
    assert derive_name(None, "ai for knowledge workers") == "AI for Knowledge Workers"
    assert derive_name(None, "AI security monitoring") == "AI Security Monitoring"
    assert derive_name(None, "pet care") == "Pet Care"
    assert derive_name(None, "job application assistant") == "Job Application Assistant"


def test_clipped_stored_name_is_replaced():
    idea = "I was thinking of building a platform where you kind of sing the song back"
    assert derive_name("I was thinking of building a platform…", idea) == "Song Back"


def test_existing_product_description_does_not_name_the_helper_verb():
    assert derive_name(None, "It helps rehearsal studios manage bookings") == "Rehearsal Studios"
    assert derive_name(None, "An app that helps rehearsal studios manage bookings") == "Rehearsal Studios"
    assert derive_name(None, "I have a product for rehearsal studios to manage bookings") == "Rehearsal Studios"
    assert derive_name("StudioLedger", "It helps rehearsal studios") == "StudioLedger"


@pytest.mark.parametrize(
    "idea,expected",
    [
        (
            "I want to build a startup: a tool that helps independent physiotherapy clinics "
            "fill last-minute cancelled appointment slots by texting waitlisted patients. "
            "Target customers are small clinics with 2-10 therapists.",
            "Physiotherapy Clinics",
        ),
        ("Helps rehearsal organizers schedule sessions and collect RSVPs", "Rehearsal Organizers"),
        ("A startup that lets local bakeries sell surplus bread", "Bakeries Surplus"),
    ],
)
def test_generic_and_vague_words_do_not_become_the_name(idea, expected):
    name = derive_name(None, idea)
    assert name == expected
    assert not name.lower().startswith(("helps", "startup"))
