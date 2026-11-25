"""OT-27: labelled examples are fake and use @example.invalid."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.gtm.relationships.examples import EXAMPLE_PEOPLE, labelled_examples


def test_every_card_is_fake():
    cards = labelled_examples()
    assert cards
    for card in cards:
        assert card["fake"] is True
        assert card["can_copy_as_sent"] is False
        assert str(card["email"]).endswith("@example.invalid")
    names = {c["name"] for c in cards}
    assert names <= set(EXAMPLE_PEOPLE)
    assert "Alex Example" in names
    assert any(c["company"] == "Northwind Robotics" for c in cards)


def test_no_overlap_with_production_fixture_names():
    cards = labelled_examples()
    productionish = {"Ada", "Analytical"}
    for card in cards:
        assert card["name"].split()[0] not in productionish
