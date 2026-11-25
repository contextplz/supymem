"""Webhook verification primitives (39 §6).

Verification is mandatory. An unverified payload is counted and dropped without
being parsed, because parsing attacker-controlled JSON before knowing who sent
it is the whole vulnerability.
"""

from __future__ import annotations

import hashlib
import hmac

MAX_AGE_S = 300


class WebhookRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_DROPS: dict[str, int] = {}


def note_drop(provider: str, reason: str) -> int:
    key = f"{provider}:{reason}"
    _DROPS[key] = _DROPS.get(key, 0) + 1
    return _DROPS[key]


def drops(provider: str | None = None) -> int:
    if provider is None:
        return sum(_DROPS.values())
    return sum(v for k, v in _DROPS.items() if k.startswith(f"{provider}:"))


def reset_drops() -> None:
    _DROPS.clear()


def sign_hex(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_hex(secret: str, raw: bytes, signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign_hex(secret, raw), signature.strip())


def require_fresh(sent_ms: int | float | None, *, now_ms: float, max_age_s: int = MAX_AGE_S) -> None:
    """Reject a payload older than the window; replays age out even if signed."""
    if sent_ms is None:
        raise WebhookRejected("missing timestamp")
    if abs(now_ms - float(sent_ms)) > max_age_s * 1000:
        raise WebhookRejected("stale timestamp")


def header(headers: dict[str, str], name: str) -> str:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower(), "")
