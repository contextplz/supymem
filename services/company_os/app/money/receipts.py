"""Evidence required before a provider event can establish venture revenue."""
from __future__ import annotations

from typing import Any

# Stripe charge API units (ISK and UGX retain two-decimal compatibility).
# https://docs.stripe.com/currencies#zero-decimal
_ZERO_DECIMAL_CHARGES = frozenset({"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "VND", "VUV", "XAF", "XOF", "XPF"})


def charge_minor_unit_exponent(currency: str) -> int:
    return 0 if currency.upper() in _ZERO_DECIMAL_CHARGES else 2


def live_customer_payment(body: dict[str, Any], workspace_id: str, venture_id: str) -> tuple[dict[str, Any] | None, str]:
    """Validate a signed event's content; callers must verify and persist its signature first."""
    data = body.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if body.get("type") != "payment_intent.succeeded" or not isinstance(obj, dict):
        return None, "not_a_customer_payment_receipt"
    meta = obj.get("metadata")
    if not isinstance(meta, dict) or not meta.get("venture_id"):
        return None, "venture_attribution_missing"
    if meta.get("workspace_id") != workspace_id or meta.get("venture_id") != venture_id:
        return None, "venture_attribution_mismatch"
    if meta.get("kind") in {"topup", "subscription"} or meta.get("order_id") or meta.get("tier"):
        return None, "supymem_billing_is_not_venture_revenue"
    if body.get("livemode") is False or obj.get("livemode") is False:
        return None, "test_mode"
    if body.get("livemode") is not True or obj.get("livemode") is not True:
        return None, "live_mode_unconfirmed"
    amount, currency = obj.get("amount_received"), obj.get("currency")
    if (obj.get("status") != "succeeded" or not isinstance(amount, int) or isinstance(amount, bool)
            or amount <= 0 or not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha()
            or not str(obj.get("id") or "").startswith("pi_") or not body.get("id")):
        return None, "payment_result_unconfirmed"
    return {"amount_minor": amount, "currency": currency.upper(), "payment_intent_id": obj["id"]}, "confirmed_live_payment"
