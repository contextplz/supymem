"""Layer 2 — known-secret patterns (43 §5.2).

Provider prefixes are near-zero-ambiguity, so they match at full confidence and
block. The generic assignment rule is one step down but still blocks: a
sixteen-character value behind the word `password` is not worth a maybe.
"""

from __future__ import annotations

import re

from app.boundary import DataClass, Finding, Segment, policy
from core.redact import REDACT_PATTERNS

DETECTOR = "patterns"

# (rule, pattern, group holding the value)
_PROVIDER_RULES: tuple[tuple[str, str, int], ...] = (
    ("stripe", r"\b[sprk]k_(?:live|test)_[A-Za-z0-9]{16,}", 0),
    ("openai", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}", 0),
    ("zernio", r"\b(?:sk_|zn_)[A-Fa-f0-9]{16,}", 0),
    ("linear", r"\blin_api_[A-Za-z0-9]{16,}", 0),
    ("razorpay", r"\brzp_(?:live|test)_[A-Za-z0-9]{10,}", 0),
    ("github_pat", r"\bgh[pousr]_[A-Za-z0-9]{36,}", 0),
    ("github_fine_grained", r"\bgithub_pat_[A-Za-z0-9_]{22,}", 0),
    ("gitlab", r"\bglpat-[A-Za-z0-9_-]{20,}", 0),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}", 0),
    ("slack_webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/+_-]{20,}", 0),
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", 0),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b", 0),
    ("google_oauth", r"\bya29\.[0-9A-Za-z_-]{20,}", 0),
    ("npm", r"\bnpm_[A-Za-z0-9]{36}\b", 0),
    ("docker", r"\bdckr_pat_[A-Za-z0-9_-]{20,}", 0),
    ("sendgrid", r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", 0),
    ("twilio", r"\bSK[0-9a-f]{32}\b", 0),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", 0),
    (
        "pem_block",
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
        0,
    ),
    ("pem_header", r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", 0),
    ("bearer", r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}", 0),
    ("basic_auth_url", r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/:@]{6,})@", 1),
)

# 43 §5.2: `KEY|TOKEN|SECRET|PASSWORD\s*[:=]\s*\S{16,}`, widened to the spellings
# that actually appear in config files.
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:[a-z0-9_.-]*(?:api[_-]?key|apikey|secret|token|password|passwd"
    r"|pwd|access[_-]?key|private[_-]?key|client[_-]?secret|auth))\b"
    r"\s*[:=]\s*"
    r"[\"']?([^\s\"',;#]{16,})[\"']?"
)

_COMPILED = tuple((rule, re.compile(pat), group) for rule, pat, group in _PROVIDER_RULES)
# Whatever the shared redactor already knows how to strip is also worth
# detecting, so the two never drift apart.
_SHARED = tuple(re.compile(p) for p in REDACT_PATTERNS)

_PLACEHOLDER_SHAPES = re.compile(r"^(?:<.*>|\{\{.*\}\}|\$\{.*\}|%[a-z_]+%|your[-_].*)$", re.I)


def is_placeholder(value: str) -> bool:
    """A stand-in, not a credential. Narrow by design: real keys are not these."""
    stripped = value.strip().strip("\"'`")
    if not stripped:
        return True
    lowered = stripped.lower()
    if lowered in {str(v).lower() for v in (policy().get("placeholder_values") or [])}:
        return True
    if _PLACEHOLDER_SHAPES.match(stripped):
        return True
    # `xxxxxxxxxxxxxxxx`, `################`, `0000...`: too few distinct symbols
    # to carry a key's worth of entropy.
    return len(set(lowered)) <= 4


def scan(segment: Segment) -> list[Finding]:
    text = segment.text
    if not text:
        return []
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(rule: str, value: str, confidence: float) -> None:
        if not value or is_placeholder(value):
            return
        key = (rule, value)
        if key in seen:
            return
        seen.add(key)
        found.append(
            Finding(
                detector=DETECTOR,
                rule=rule,
                data_class=DataClass.SECRET,
                confidence=confidence,
                value=value,
                label=segment.display(),
            )
        )

    for rule, rx, group in _COMPILED:
        for match in rx.finditer(text):
            add(rule, match.group(group), 1.0)
    for rx in _SHARED:
        for match in rx.finditer(text):
            add("shared_redactor", match.group(0), 1.0)
    for match in _ASSIGNMENT.finditer(text):
        add("assignment", match.group(1), 0.9)
    return found
