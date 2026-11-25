"""Charter fields carry provenance."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.charter.synthesize import synthesize_charter


def test_charter_provenance_present() -> None:
    content = synthesize_charter(
        idea="an app for bakers",
        classification={
            "business_model": "saas",
            "build_archetype": "crud_web",
            "audience": "bakers",
            "regulatory_surface": "none",
        },
        answers={"gtm_order": "market_first", "audience": "home bakers"},
    )
    prov = content["provenance"]
    assert "audience" in prov
    assert prov["audience"]["source"] in {"answered", "inferred", "default", "founder_words"}
    assert "gtm_order" in prov
