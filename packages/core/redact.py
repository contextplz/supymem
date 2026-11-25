"""Secret redaction — every pattern from docs/company-os/16_MIDDLEWARE_GATEWAY.md §5."""

from __future__ import annotations

import os
import re
from numbers import Number
from typing import Any

REDACT_PATTERNS: list[str] = [
    r"sk_(live|test)_[A-Za-z0-9]{16,}",
    r"pk_(live|test)_[A-Za-z0-9]{16,}",
    r"sk-(?:proj|ant|svcacct|admin)-[A-Za-z0-9_-]{20,}",  # OpenAI project / Anthropic keys
    r"sk-[A-Za-z0-9]{20,}",
    r"AIza[0-9A-Za-z_-]{35}",  # Google API key
    r"fc-[A-Fa-f0-9]{32}",  # Firecrawl
    r"github_pat_[A-Za-z0-9_]{22,}",
    r"gh[opsur]_[A-Za-z0-9]{36,}",
    r"(?<=://)[^\s:/@]+:[^\s@/]+(?=@)",  # user:password in any URL (postgres://u:p@host)
    r"(?:sk_|zn_)[A-Fa-f0-9]{16,}",
    r"lin_api_[A-Za-z0-9]{16,}",
    r"rzp_(?:live|test)_[A-Za-z0-9]{10,}",
    r"ghp_[A-Za-z0-9]{36}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[abpsr]-[A-Za-z0-9-]{10,}",
    r"Bearer\s+[A-Za-z0-9._\-]{20,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT
]

_COMPILED = [re.compile(p) for p in REDACT_PATTERNS]
_REPLACEMENT = "[REDACTED]"
MIN_SECRET_LENGTH = 12


def is_secret_value(value: str) -> bool:
    # ponytail: token-shape heuristic excludes prose, extend for new credential formats.
    return len(value) >= MIN_SECRET_LENGTH and (
        any(pattern.search(value) for pattern in _COMPILED)
        or (re.fullmatch(r"[A-Za-z0-9_+./=-]+", value) is not None
            and len(set(value)) >= 4
            and re.search(r"[0-9]", value) is not None)
    )


def redact_string(text: str, *, exact_values: list[str] | None = None) -> str:
    """Apply all redaction patterns (and optional explicit exact matches) to a string."""
    out = text
    for rx in _COMPILED:
        out = rx.sub(_REPLACEMENT, out)
    if exact_values:
        for v in exact_values:
            if v and v in out:
                out = out.replace(v, _REPLACEMENT)
    return out


def redact(value: Any, *, exact_values: list[str] | None = None) -> Any:
    """Recursively redact secrets in dict / list / str structures."""
    if isinstance(value, str):
        return redact_string(value, exact_values=exact_values)
    if isinstance(value, dict):
        return {k: redact(v, exact_values=exact_values) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, exact_values=exact_values) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, exact_values=exact_values) for v in value)
    if (exact_values is not None and value is not None and not isinstance(value, Number)
            and os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local"):
        return redact_string(str(value), exact_values=exact_values)
    return value
