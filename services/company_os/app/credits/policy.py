"""Read/write ``credit_policies``. In-process cache with a 5s TTL."""

from __future__ import annotations

import json
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.credits._txn import naive_utc, session_factory
from app.credits.units import MAX_GRANT_MICRO, credits_to_micro
from app.db.models import CreditAccount, CreditPolicy

WALLET_PHASE_KEY = "founder_wallet_phase"


def wallet_phase(session: Session | None = None) -> str:
    """Financial schema authority, read without the editable-policy cache.

    Old databases without the expansion marker remain legacy. A fresh local
    create_all database may use person mode; ensuring its first wallet records
    that decision. Only the drained transfer migration changes an existing marker.
    """
    if session is None:
        with session_factory()() as owned:
            return wallet_phase(owned)
    raw = session.scalar(select(CreditPolicy.value_json).where(CreditPolicy.key == WALLET_PHASE_KEY))
    if raw is None:
        legacy = session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "venture").limit(1))
        return "legacy" if legacy is not None else "person"
    try:
        phase = json.loads(raw)
    except (TypeError, ValueError):
        phase = None
    if phase not in ("legacy", "person"):
        from app.credits.errors import CreditsUnavailable
        raise CreditsUnavailable("wallet phase needs reconciliation")
    return phase


def person_wallet_enabled(session: Session | None = None) -> bool:
    return wallet_phase(session) == "person"

_TTL_SECONDS = 5.0
_lock = threading.Lock()
_cache: dict[str, object] | None = None
_cache_at: float = 0.0

# Spec §4.1 seeds. Used when a row is missing (create_all test DBs).
_FALLBACKS: dict[str, object] = {
    "default_venture_credits": 5000,
    "max_ventures_per_account": 2,
    "max_grant_credits_support": 2000,
    "max_grant_credits_finance": 5000,
    "dual_control_threshold": 50000,
    "reservation_ttl_seconds": 900,
    "web_search_credits": 2,
    "web_fetch_credits": 1,
    "trial_days": 14,
    "enforcement_enabled": True,
    # Money fields are minor units (cents / paise). Doc 100 §3.2.
    "credit_packs": [
        {
            "sku": "cr_500",
            "credits": 500,
            "usd": 500,
            "inr": 44900,
            "label": "Starter",
        },
        {
            "sku": "cr_1000",
            "credits": 1000,
            "usd": 1000,
            "inr": 89900,
            "label": "Builder",
        },
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
    ],
    "topup_provider_by_currency": {
        "USD": "stripe",
        "EUR": "stripe",
        "GBP": "stripe",
        "INR": "stripe",
        "*": "stripe",
    },
}


def invalidate() -> None:
    """Drop the cache so the next read hits the DB. ``set_policy`` always calls this."""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0


def _load_table() -> dict[str, object]:
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _cache_at) < _TTL_SECONDS:
            return _cache
    rows: dict[str, object] = {}
    factory = session_factory()
    with factory() as session:
        for pol in session.scalars(select(CreditPolicy).where(CreditPolicy.key != WALLET_PHASE_KEY)).all():
            try:
                rows[pol.key] = json.loads(pol.value_json)
            except json.JSONDecodeError:
                rows[pol.key] = pol.value_json
    with _lock:
        _cache = rows
        _cache_at = time.monotonic()
        return rows


def get_policy(key: str, default: object | None = None) -> object | None:
    table = _load_table()
    if key in table:
        return table[key]
    if default is not None:
        return default
    return _FALLBACKS.get(key)


def get_int_policy(key: str, default: int) -> int:
    value = get_policy(key, default)
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def get_bool_policy(key: str, default: bool) -> bool:
    value = get_policy(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, int):
        return value != 0
    return default


def set_policy(key: str, value: object, updated_by: str) -> None:
    if key == WALLET_PHASE_KEY:
        raise ValueError("Wallet phase changes require the drained financial migration.")
    payload = json.dumps(value)
    now = naive_utc()
    factory = session_factory()
    with factory() as session:
        row = session.get(CreditPolicy, key)
        if row is None:
            session.add(
                CreditPolicy(
                    key=key,
                    value_json=payload,
                    updated_at=now,
                    updated_by=updated_by,
                )
            )
        else:
            row.value_json = payload
            row.updated_at = now
            row.updated_by = updated_by
        session.commit()
    invalidate()


def default_venture_credits() -> int:
    return get_int_policy("default_venture_credits", 5000)


def reservation_ttl_seconds() -> int:
    return get_int_policy("reservation_ttl_seconds", 900)


def enforcement_enabled() -> bool:
    return get_bool_policy("enforcement_enabled", True)


def web_search_credits() -> int:
    return get_int_policy("web_search_credits", 2)


def web_fetch_credits() -> int:
    return get_int_policy("web_fetch_credits", 1)


def dual_control_threshold_micro() -> int:
    credits = get_int_policy("dual_control_threshold", 50_000)
    return credits_to_micro(credits)


def grant_cap_micro(role: str) -> int:
    """All admin grants retain the shared safety ceiling, not per-role caps."""
    normalised = (role or "").strip().lower().replace("-", "_")
    if normalised in {"super_admin", "superadmin", "finance", "support", "read_only"}:
        return MAX_GRANT_MICRO
    return 0


__all__ = [
    "default_venture_credits",
    "dual_control_threshold_micro",
    "enforcement_enabled",
    "get_bool_policy",
    "get_int_policy",
    "get_policy",
    "grant_cap_micro",
    "invalidate",
    "person_wallet_enabled",
    "reservation_ttl_seconds",
    "set_policy",
    "web_fetch_credits",
    "web_search_credits",
    "wallet_phase",
]
