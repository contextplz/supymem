"""Unit conversions for the credit meter. No database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.credits.units import (
    CREDITS_PER_USD,
    MAX_GRANT_MICRO,
    MICRO_PER_CREDIT,
    MICRO_PER_USD,
    cents_to_micro,
    credits_to_micro,
    format_credits,
    format_usd,
    micro_to_credits,
    usd_to_micro,
    validate_amount_micro,
)


def test_one_dollar_round_trip() -> None:
    assert CREDITS_PER_USD == 100
    assert MICRO_PER_CREDIT == 10_000
    assert MICRO_PER_USD == 1_000_000
    assert usd_to_micro(Decimal("1")) == 1_000_000
    assert usd_to_micro("1.00") == 1_000_000
    assert usd_to_micro(1) == 1_000_000
    assert credits_to_micro(100) == 1_000_000
    assert micro_to_credits(1_000_000) == Decimal("100")
    assert cents_to_micro(100) == 1_000_000


def test_one_cent_is_one_credit() -> None:
    assert cents_to_micro(1) == MICRO_PER_CREDIT
    assert credits_to_micro(1) == MICRO_PER_CREDIT
    assert usd_to_micro(Decimal("0.01")) == MICRO_PER_CREDIT


def test_usd_to_micro_rounds_half_up() -> None:
    # 0.4 micro → 0; 0.5 micro → 1.
    assert usd_to_micro(Decimal("0.0000004")) == 0
    assert usd_to_micro("0.0000005") == 1
    assert usd_to_micro(Decimal("0.0000015")) == 2


def test_usd_to_micro_rejects_float() -> None:
    with pytest.raises(TypeError):
        usd_to_micro(1.0)  # type: ignore[arg-type]


def test_format_helpers() -> None:
    assert format_credits(5_000_000) == "500.00"
    assert format_usd(5_000_000) == "$5.00"


def test_validate_amount_micro_accepts_range() -> None:
    assert validate_amount_micro(1) == 1
    assert validate_amount_micro(MAX_GRANT_MICRO) == MAX_GRANT_MICRO


@pytest.mark.parametrize(
    "value",
    [0, -1, -500, 2**63, MAX_GRANT_MICRO + 1, 1.5, "1", True, None],
)
def test_validate_amount_micro_rejects_invalid(value: object) -> None:
    with pytest.raises(ValueError):
        validate_amount_micro(value)
