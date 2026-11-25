"""Near-duplicate detection over everything we have ever published.

Reddit and X both name "identical or substantially similar content" as the
prohibited thing, which means both detect it. SimHash over word shingles is
enough here and needs no embedding dependency.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[a-z0-9']+")
_SHINGLE = 3
_BITS = 64
#: Hamming distance at or below this counts as the same post.
DEFAULT_THRESHOLD = 12


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _shingles(text: str) -> list[str]:
    words = _tokens(text)
    if len(words) <= _SHINGLE:
        return [" ".join(words)] if words else []
    return [
        " ".join(words[i : i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)
    ]


def simhash(text: str) -> str:
    """Return a 64-bit SimHash as a hex string, stable across processes."""
    vector = [0] * _BITS
    grams = _shingles(text)
    if not grams:
        return "0" * 16
    for gram in grams:
        digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(_BITS):
            if value >> bit & 1:
                vector[bit] += 1
            else:
                vector[bit] -= 1
    out = 0
    for bit in range(_BITS):
        if vector[bit] > 0:
            out |= 1 << bit
    return f"{out:016x}"


def distance(left: str, right: str) -> int:
    try:
        a = int(left, 16)
        b = int(right, 16)
    except (TypeError, ValueError):
        return _BITS
    return (a ^ b).bit_count()


def is_duplicate(
    candidate: str,
    previous: list[str],
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> tuple[bool, int]:
    """Compare a body against stored hashes. Returns (duplicate, best distance)."""
    if not previous:
        return False, _BITS
    fingerprint = simhash(candidate)
    best = min((distance(fingerprint, prior) for prior in previous), default=_BITS)
    return best <= threshold, best
