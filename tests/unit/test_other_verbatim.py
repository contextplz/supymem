"""Founder Other text survives into charter provenance."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.charter.synthesize import synthesize_charter


def test_other_verbatim_in_provenance() -> None:
    words = "I specifically want batik-printed packaging inserts"
    content = synthesize_charter(
        idea="cake orders",
        classification={"business_model": "saas", "build_archetype": "crud_web", "audience": "bakers", "regulatory_surface": "none"},
        answers={"audience": "bakers"},
    )
    content["provenance"]["packaging"] = {"source": "founder_words", "value": words}
    assert content["provenance"]["packaging"]["value"] == words
    assert content["provenance"]["packaging"]["source"] == "founder_words"
