"""RFC 6238 TOTP (SHA-1, 6 digits, 30s step) with stdlib only.

Single-use replay protection: an accepted ``(secret_id, code, time_step)``
cannot be accepted twice. No third-party OTP library — an auth primitive is
the last place to widen the supply chain.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections import OrderedDict
from datetime import datetime
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
WINDOW = 1
_REPLAY_CAPACITY = 10_000
_replay: OrderedDict[tuple[str, str, int], None] = OrderedDict()


def generate_secret() -> str:
    """20 random bytes, base32 (no padding), as used by authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    label = quote(f"{issuer}:{email}")
    secret_n = secret.replace(" ", "").rstrip("=")
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret_n}"
        f"&issuer={quote(issuer)}"
        f"&algorithm=SHA1"
        f"&digits={DIGITS}"
        f"&period={STEP_SECONDS}"
    )


def _secret_bytes(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    pad = "=" * ((8 - len(cleaned) % 8) % 8)
    return base64.b32decode(cleaned + pad, casefold=True)


def hotp(key: bytes, counter: int, *, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP. ``digits=8`` is used only to assert RFC 6238 vectors."""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = (
        ((digest[offset] & 0x7F) << 24)
        | ((digest[offset + 1] & 0xFF) << 16)
        | ((digest[offset + 2] & 0xFF) << 8)
        | (digest[offset + 3] & 0xFF)
    )
    return str(truncated % (10**digits)).zfill(digits)


def totp_code(
    secret: str,
    *,
    now: datetime | float | None = None,
    digits: int = DIGITS,
    step: int = STEP_SECONDS,
) -> str:
    return hotp(_secret_bytes(secret), _time_step(now, step), digits=digits)


def _time_step(now: datetime | float | None, step: int) -> int:
    if now is None:
        ts = time.time()
    elif isinstance(now, datetime):
        ts = now.timestamp()
    else:
        ts = float(now)
    return int(ts) // step


def _secret_id(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _replay_key(secret_id: str, code: str, time_step: int) -> tuple[str, str, int]:
    return (secret_id, code, time_step)


def _remember(key: tuple[str, str, int]) -> bool:
    """Return True if this is a replay."""
    if key in _replay:
        return True
    _replay[key] = None
    _replay.move_to_end(key)
    while len(_replay) > _REPLAY_CAPACITY:
        _replay.popitem(last=False)
    return False


def reset_replay_cache() -> None:
    _replay.clear()


def verify(
    secret: str,
    code: str,
    *,
    now: datetime | float | None = None,
    window: int = WINDOW,
    secret_id: str | None = None,
    digits: int = DIGITS,
    step: int = STEP_SECONDS,
) -> bool:
    if not isinstance(code, str):
        return False
    candidate = code.strip().replace(" ", "")
    if len(candidate) != digits or not candidate.isdigit():
        return False
    try:
        key = _secret_bytes(secret)
    except Exception:
        return False
    t = _time_step(now, step)
    sid = secret_id or _secret_id(secret)
    matched_step: int | None = None
    # Check every offset so a miss on offset -1 cannot skip offset +1, and
    # compare_digest keeps each comparison constant-time.
    for offset in range(-window, window + 1):
        expected = hotp(key, t + offset, digits=digits)
        if hmac.compare_digest(expected, candidate) and matched_step is None:
            matched_step = t + offset
    if matched_step is None:
        return False
    if _remember(_replay_key(sid, candidate, matched_step)):
        return False
    return True
