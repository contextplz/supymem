"""Labelled synthetic First Five Minutes examples (OT-27). Never real humans."""

from __future__ import annotations

from typing import Any, Final

EXAMPLE_PEOPLE: Final[tuple[str, ...]] = ("Alex Example", "Jordan Sample")
EXAMPLE_COMPANIES: Final[tuple[str, ...]] = ("Northwind Robotics",)
EXAMPLE_EMAIL_DOMAIN: Final = "example.invalid"


def labelled_examples() -> list[dict[str, Any]]:
    return [
        {
            "fake": True,
            "id": "example_alex",
            "name": "Alex Example",
            "company": "Northwind Robotics",
            "email": f"alex@{EXAMPLE_EMAIL_DOMAIN}",
            "role": "Head of Platform",
            "band": "pursue",
            "explain": "Example (fake): a pasted job post produced a quote and a draft.",
            "why_this_week": "Example only. Cannot mark sent.",
            "can_copy_as_sent": False,
        },
        {
            "fake": True,
            "id": "example_jordan",
            "name": "Jordan Sample",
            "company": "Northwind Robotics",
            "email": f"jordan@{EXAMPLE_EMAIL_DOMAIN}",
            "role": "Founder",
            "band": "watch",
            "explain": "Example (fake): refuse-to-personalize when there is no quote.",
            "why_this_week": "Example only. Cannot mark sent.",
            "can_copy_as_sent": False,
        },
    ]


def is_labelled_example_contact(*, email: str | None, first_name: str | None, last_name: str | None) -> bool:
    mail = (email or "").lower()
    if mail.endswith(f"@{EXAMPLE_EMAIL_DOMAIN}"):
        return True
    full = f"{(first_name or '').strip()} {(last_name or '').strip()}".strip()
    return full in EXAMPLE_PEOPLE
