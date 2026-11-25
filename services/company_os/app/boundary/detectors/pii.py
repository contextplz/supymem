"""Layer 4 — third-party personal data (43 §5.4). Tuned for recall.

An incidental mention is replaced with a placeholder; a payload carrying several
distinct people's details is a record dump and is refused outright. Both
outcomes satisfy "never reaches a model" — the difference is whether the founder
loses the prompt as well as the value.

We cannot detect a bare personal name, and we do not pretend to: names arrive
inside record tables, which the structural and path layers refuse whole.
"""

from __future__ import annotations

import re

from app.boundary import DataClass, Finding, Segment, threshold

DETECTOR = "pii"
INCIDENTAL_CONFIDENCE = 0.7

# RFC 2606 / RFC 6761 reserved names cannot belong to a real person.
_RESERVED_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "companyos.local",
    "localhost",
)
_RESERVED_TLDS = (".example", ".test", ".invalid", ".local", ".localdomain")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_E164 = re.compile(r"(?<![\d.\-])\+\d{10,15}\b")
_NANP = re.compile(r"(?<![\d.\-])(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?![\d.\-])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)")
_CARD = re.compile(r"(?<![\d\-])(?:\d[ \-]?){12,18}\d(?![\d\-])")
_STREET = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][A-Za-z.']+\s+){1,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct"
    r"|Way|Terrace|Place|Pl|Circle|Cir|Parkway|Pkwy)\b\.?",
    re.I,
)


def is_reserved_email(value: str) -> bool:
    lowered = value.lower()
    domain = lowered.rpartition("@")[2]
    return domain in _RESERVED_DOMAINS or domain.endswith(_RESERVED_TLDS)


def luhn_ok(digits: str) -> bool:
    """Standard mod-10 check. Cuts card-shaped false positives by ~90%."""
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13 or len(nums) > 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(nums)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def scan(segment: Segment) -> list[Finding]:
    text = segment.text
    if not text:
        return []
    hits: list[tuple[str, str]] = []

    for match in _EMAIL.finditer(text):
        value = match.group(0)
        if not is_reserved_email(value):
            hits.append(("email", value))
    for rule, rx in (
        ("phone_e164", _E164),
        ("phone_nanp", _NANP),
        ("national_id_us", _SSN),
        ("national_id_in", _AADHAAR),
        ("postal_address", _STREET),
    ):
        for match in rx.finditer(text):
            hits.append((rule, match.group(0)))
    for match in _CARD.finditer(text):
        value = match.group(0)
        if luhn_ok(value):
            hits.append(("card_luhn", value))

    if not hits:
        return []

    deduped: dict[str, str] = {}
    for rule, value in hits:
        deduped.setdefault(value, rule)

    bulk = len(deduped) >= int(threshold("pii_bulk_threshold", 5))
    confidence = 1.0 if bulk else INCIDENTAL_CONFIDENCE
    return [
        Finding(
            detector=DETECTOR,
            rule=f"{rule}{':bulk' if bulk else ''}",
            data_class=DataClass.THIRD_PARTY_PII,
            confidence=confidence,
            value=value,
            label=segment.display(),
        )
        for value, rule in deduped.items()
    ]
