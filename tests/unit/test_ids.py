"""IDs: Crockford base32, 60 bits, prefix round-trip, lowercase, no ambiguous chars."""

from __future__ import annotations

import re

from core.ids import new_id, parse_prefix

AMBIGUOUS = set("ilouILOU")
BODY_RE = re.compile(r"^[0-9a-hjkmnp-tv-z]+$")


def test_100k_ids_zero_collisions() -> None:
    seen: set[str] = set()
    for _ in range(100_000):
        i = new_id("ven")
        assert i not in seen
        seen.add(i)
    assert len(seen) == 100_000


def test_prefixes_round_trip() -> None:
    for prefix in ("ven", "idea", "chr", "ag", "wo", "prop", "job", "evt"):
        ident = new_id(prefix)
        assert parse_prefix(ident) == prefix
        assert ident.startswith(prefix + "_")


def test_lowercase_and_no_ambiguous() -> None:
    for _ in range(1000):
        ident = new_id("ven")
        assert ident == ident.lower()
        prefix, body = ident.split("_", 1)
        assert prefix == "ven"
        assert len(body) == 12
        assert BODY_RE.match(body)
        assert not (set(body) & AMBIGUOUS)
