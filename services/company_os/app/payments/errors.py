"""Payment-layer errors. ``code`` is stable for HTTP mapping."""

from __future__ import annotations


class PaymentError(Exception):
    """Base for every payment-layer failure."""

    code: str = "payment_error"


class WebhookVerificationError(PaymentError):
    """Bad signature or stale timestamp. Map to HTTP 401 with an empty body."""

    code = "webhook_verification_failed"


class UnknownOrder(PaymentError):
    """Webhook or refund referenced an order we do not have."""

    code = "unknown_order"


class AmountMismatch(PaymentError):
    """Payload amount/currency disagrees with the stored order. Do not grant."""

    code = "amount_mismatch"


class ProviderUnavailable(PaymentError):
    """Named provider is not registered or cannot be reached."""

    code = "provider_unavailable"
