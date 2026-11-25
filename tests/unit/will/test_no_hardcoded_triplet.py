from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.will.slate import generate_slate  # noqa: E402


def test_different_ideas_produce_different_slate_titles() -> None:
    clinic = generate_slate(
        "ven_clinic",
        because="test",
        idea="Automated appointment reminders for dental clinics",
    )
    kitchen = generate_slate(
        "ven_kitchen",
        because="test",
        idea="Inventory forecasting for independent restaurants",
    )
    assert [item.title for item in clinic.items] != [item.title for item in kitchen.items]
    assert {"Add user sign-up", "Add login", "Add pricing section"} != {
        item.title for item in clinic.items
    }
