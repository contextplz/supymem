"""Credit pack catalogue. Prices are policy rows, never client-supplied."""

from __future__ import annotations

from dataclasses import dataclass

from app.credits.policy import get_policy

# Doc 100 §3.2. Money fields are minor units (cents / paise) as ints.
_FALLBACK: list[dict[str, object]] = [
    {"sku": "cr_500", "credits": 500, "usd": 500, "inr": 44900, "label": "Starter"},
    {"sku": "cr_1000", "credits": 1000, "usd": 1000, "inr": 89900, "label": "Builder"},
    {
        "sku": "cr_2000",
        "credits": 2000,
        "usd": 2000,
        "inr": 179900,
        "label": "Studio",
        "badge": "Popular",
    },
    {
        "sku": "cr_5000",
        "credits": 5000,
        "usd": 5000,
        "inr": 439900,
        "label": "Scale",
        "bonus_credits": 250,
    },
]


@dataclass(frozen=True)
class Pack:
    sku: str
    credits: int
    bonus_credits: int
    label: str
    badge: str | None
    usd: int
    inr: int | None

    def price_minor(self, currency: str) -> int:
        code = (currency or "").strip().upper()
        if code == "USD":
            return self.usd
        if code == "INR":
            if self.inr is None:
                raise ValueError(f"unknown currency {currency!r}")
            return self.inr
        raise ValueError(f"unknown currency {currency!r}")


def _as_int(value: object, *, name: str) -> int | None:
    if type(value) is int:
        return value
    if value is None:
        return None
    raise TypeError(f"{name} must be int, not {type(value).__name__}")


def _parse_pack(raw: object) -> Pack | None:
    if not isinstance(raw, dict):
        return None
    try:
        sku = raw.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            return None
        credits = _as_int(raw.get("credits"), name="credits")
        usd = _as_int(raw.get("usd"), name="usd")
        if credits is None or usd is None or credits < 1 or usd < 1:
            return None
        bonus = raw.get("bonus_credits", 0)
        bonus_credits = _as_int(bonus, name="bonus_credits")
        if bonus_credits is None or bonus_credits < 0:
            return None
        inr_raw = raw.get("inr")
        inr = _as_int(inr_raw, name="inr") if inr_raw is not None else None
        if inr is not None and inr < 1:
            return None
        label = raw.get("label")
        badge = raw.get("badge")
        return Pack(
            sku=sku.strip(),
            credits=credits,
            bonus_credits=bonus_credits,
            label=label.strip() if isinstance(label, str) and label.strip() else sku.strip(),
            badge=badge.strip() if isinstance(badge, str) and badge.strip() else None,
            usd=usd,
            inr=inr,
        )
    except TypeError:
        return None


def credit_packs() -> list[Pack]:
    raw = get_policy("credit_packs")
    rows = raw if isinstance(raw, list) else _FALLBACK
    packs = [p for p in (_parse_pack(row) for row in rows) if p is not None]
    if packs:
        return packs
    return [p for p in (_parse_pack(row) for row in _FALLBACK) if p is not None]


def find_pack(sku: str) -> Pack | None:
    wanted = (sku or "").strip()
    if not wanted:
        return None
    for pack in credit_packs():
        if pack.sku == wanted:
            return pack
    return None


def price_for(sku: str, currency: str) -> tuple[int, str]:
    pack = find_pack(sku)
    if pack is None:
        raise ValueError(f"unknown sku {sku!r}")
    code = (currency or "").strip().upper()
    if not code:
        raise ValueError(f"unknown currency {currency!r}")
    return pack.price_minor(code), code


__all__ = [
    "Pack",
    "credit_packs",
    "find_pack",
    "price_for",
]
