"""Crockford base32 IDs: ``{prefix}_{id}``, 60 random bits, lowercase."""

from __future__ import annotations

import secrets

# Crockford base32 alphabet — excludes i, l, o, u (ambiguous with 1/0).
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
_BITS = 60
_CHARS = _BITS // 5  # 12


def encode_crockford(n: int, *, length: int = _CHARS) -> str:
    """Encode a non-negative integer as Crockford base32 (lowercase)."""
    if n < 0:
        raise ValueError("n must be non-negative")
    chars: list[str] = []
    for _ in range(length):
        chars.append(_CROCKFORD[n & 0x1F])
        n >>= 5
    if n:
        raise ValueError("value does not fit in length")
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """Return ``{prefix}_{crockford(60 random bits)}``."""
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError(f"invalid prefix: {prefix!r}")
    # Strip trailing underscore if caller passed ``ven_``.
    p = prefix.rstrip("_")
    body = encode_crockford(secrets.randbits(_BITS))
    return f"{p}_{body}"


def parse_prefix(identifier: str) -> str:
    """Return the prefix portion of an id (text before the first ``_``)."""
    if not identifier or "_" not in identifier:
        raise ValueError(f"invalid identifier: {identifier!r}")
    prefix, _, body = identifier.partition("_")
    if not prefix or not body:
        raise ValueError(f"invalid identifier: {identifier!r}")
    return prefix
