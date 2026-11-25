from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.will.override import parse_override  # noqa: E402
from app.will.slate import generate_slate  # noqa: E402


def test_day_one_override_phrasings_parse() -> None:
    slate = generate_slate("ven_override", because="test", idea="A workflow tool for therapists")
    phrases = (
        "do the first two only",
        "drop 4 and 5, add a pricing experiment and a LinkedIn push",
        "the third one should be reminders, not intake",
        "do all of them but pricing first",
        "stop the cold email, it feels spammy",
        "why is 2 there?",
        "just handle it",
    )
    results = [parse_override(phrase, slate) for phrase in phrases]
    assert results[0].edits[0].op == "keep"
    assert {edit.op for edit in results[1].edits} == {"drop", "insert"}
    assert results[2].edits[0].op == "modify"
    assert results[3].edits[0].op in {"reorder"} or results[3].unclear
    assert results[4].interpretation.startswith("Stop cold email")
    assert not results[5].edits
    assert results[6].autopilot_on
