"""PII detection — regex plus core.redact. Presidio only when GUARDRAILS_MODE=full."""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.redact import redact_string

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b")
_CC = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_AADHAAR = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", _EMAIL),
    ("phone", _PHONE),
    ("credit_card", _CC),
    ("aadhaar", _AADHAAR),
    ("ssn", _SSN),
)

BLOCK_ON_OUTPUT = frozenset({"credit_card", "aadhaar", "ssn"})


@dataclass(frozen=True)
class PiiHit:
    kind: str
    excerpt: str


def detect_pii(text: str) -> list[PiiHit]:
    raw = text or ""
    hits: list[PiiHit] = []
    for kind, rx in KIND_PATTERNS:
        for m in rx.finditer(raw):
            hits.append(PiiHit(kind=kind, excerpt=m.group(0)[:32]))
    return hits


def redact_pii(text: str) -> str:
    out = redact_string(text or "")
    out = _EMAIL.sub("[REDACTED_EMAIL]", out)
    out = _SSN.sub("[REDACTED_SSN]", out)
    out = _AADHAAR.sub("[REDACTED_AADHAAR]", out)
    out = _CC.sub("[REDACTED_CARD]", out)
    return out
