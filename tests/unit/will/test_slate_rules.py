from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.will.slate import generate_slate  # noqa: E402
from app.will.validate import SlateValidationError, validate_slate  # noqa: E402


def test_generated_slate_obeys_all_composition_rules() -> None:
    slate = generate_slate("ven_rules", because="test", idea="Scheduling software for clinics")
    assert len(slate.items) == 5
    assert all(item.evidence for item in slate.items)
    assert any(item.origin == "probe" for item in slate.items)
    validate_slate(slate)


def test_rank_one_cannot_be_blocked() -> None:
    slate = generate_slate("ven_blocked", because="test", idea="Payroll tools for freelancers")
    blocked = slate.model_copy(
        update={"items": [slate.items[0].model_copy(update={"blocked_by": ["human_1"]}), *slate.items[1:]]}
    )
    with pytest.raises(SlateValidationError, match="rank 1"):
        validate_slate(blocked)


def test_named_product_idea_keeps_the_product_name_and_drops_the_command() -> None:
    slate = generate_slate(
        "ven_named_product",
        because="test",
        idea="Build ScopeSignal, a scope-change approval assistant for small agencies.",
    )

    assert all("build scopesignal" not in item.title.lower() for item in slate.items)
    assert all("ScopeSignal" in item.title for item in slate.items)
