"""Founder-facing credit UI must not mention $ or USD (doc 112 P1)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = [
    ROOT / "apps/console/src/components/settings/TokenMeter.tsx",
    ROOT / "apps/console/src/components/credits/TopUpCard.tsx",
]


def test_founder_credit_surfaces_have_no_dollar_copy() -> None:
    import re

    currency = re.compile(r"\$\d|credits = \$|USD|creditsToUsd|formatUsd|credits_per_usd")
    for path in FILES:
        text = path.read_text()
        match = currency.search(text)
        assert match is None, f"dollar copy in {path}: {match.group(0) if match else ''}"
