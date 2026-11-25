"""Integer micro-credit conversion. The only module allowed to scale these units."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

CREDITS_PER_USD = 100
MICRO_PER_CREDIT = 10_000
MICRO_PER_USD = CREDITS_PER_USD * MICRO_PER_CREDIT  # 1_000_000
MIN_RESERVE_MICRO = 100
MAX_GRANT_MICRO = 10_000_000_000  # $10,000 — sanity ceiling per single grant


def _as_decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    """Coerce to Decimal. Floats are rejected so rounding bugs cannot creep in."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(
            f"{name} does not accept {type(value).__name__}; pass Decimal, str, or int"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(
        f"{name} requires Decimal, str, or int, not {type(value).__name__}"
    )


def usd_to_micro(usd: Decimal | str | int) -> int:
    """Convert dollars to micro-credits, rounding half-up. Never accepts float."""
    amount = _as_decimal(usd, name="usd_to_micro")
    scaled = (amount * MICRO_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(scaled)


def micro_to_credits(micro: int) -> Decimal:
    """Display helper: micro-credits → credits. Not for ledger arithmetic."""
    if type(micro) is not int:
        raise TypeError("micro_to_credits requires int")
    return Decimal(micro) / Decimal(MICRO_PER_CREDIT)


def credits_to_micro(credits: int) -> int:
    if type(credits) is not int:
        raise TypeError("credits_to_micro requires int")
    return credits * MICRO_PER_CREDIT


def cents_to_micro(cents: int) -> int:
    """Payments: one cent is one credit, no rounding."""
    if type(cents) is not int:
        raise TypeError("cents_to_micro requires int")
    return cents * MICRO_PER_CREDIT


def format_credits(micro: int) -> str:
    """Whole-and-fractional credits, two decimal places (e.g. ``500.00``)."""
    credits = micro_to_credits(micro).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{credits:.2f}"


def format_usd(micro: int) -> str:
    """Dollar display with two decimal places (e.g. ``$5.00``)."""
    usd = (Decimal(micro) / Decimal(MICRO_PER_USD)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return f"${usd:.2f}"


def validate_amount_micro(value: object) -> int:
    """Reject non-ints, zero, negatives, and amounts above ``MAX_GRANT_MICRO``.

    Used by the admin API so a ``-500`` or ``2**63`` grant cannot wrap (attack #15).
    """
    if type(value) is not int:
        raise ValueError("amount must be a positive int of micro-credits")
    if value < 1 or value > MAX_GRANT_MICRO:
        raise ValueError(
            f"amount must be in [1, {MAX_GRANT_MICRO}], got {value}"
        )
    return value


def credits_text(value: Any) -> str:
    """Founder-facing credit amount (already in credits, not micro), at most two decimals."""
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
