"""Secrets leakage — core.redact patterns plus a conservative entropy check."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from core.redact import REDACT_PATTERNS, redact_string

_COMPILED = tuple(re.compile(p) for p in REDACT_PATTERNS)
_TOKENISH = re.compile(r"\b[A-Za-z0-9/_+=.-]{32,}\b")


@dataclass(frozen=True)
class SecretHit:
    kind: str
    excerpt: str


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def detect_secrets(text: str) -> list[SecretHit]:
    raw = text or ""
    hits: list[SecretHit] = []
    for rx in _COMPILED:
        m = rx.search(raw)
        if m:
            hits.append(SecretHit(kind="pattern", excerpt=m.group(0)[:24]))
    for m in _TOKENISH.finditer(raw):
        token = m.group(0)
        if _entropy(token) >= 4.2 and not token.startswith("http"):
            hits.append(SecretHit(kind="entropy", excerpt=token[:24]))
    return hits


def redact_secrets(text: str) -> str:
    return redact_string(text or "")
