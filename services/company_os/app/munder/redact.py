"""Strip secret-shaped substrings before text leaves the process.

Ported from Munder Difflin ``src/main/hive.ts`` ``redactSecrets``.
MIT: Chaitanya Giri. Do not run vendor/munder-difflin in-process.
"""

from __future__ import annotations

import re
from typing import Any

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
_PREFIX = re.compile(
    r"(?:sk-(?:ant-)?[A-Za-z0-9_-]{16,}|xox[bpaors]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}"
    r"|gh[posru]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}"
    r"|AIza[A-Za-z0-9_-]{20,})"
)
_BEARER = re.compile(r"\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
_KV = re.compile(
    r"\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|secret[_-]?access[_-]?key|secret|token|"
    r"password|passwd|pwd|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"signing[_-]?secret|webhook[_-]?secret|auth[_-]?token|bot[_-]?token|"
    r"private[_-]?key))([\"']?\s*[:=]\s*)([\"']?)[^\s\"',}]{6,}\3",
    re.I,
)


def redact_secrets(text: Any) -> str:
    """Copy of Munder ``redactSecrets`` — conservative shape matching, not entropy."""
    if not isinstance(text, str):
        return ""
    if not text:
        return text
    s = _PEM.sub("[redacted]", text)
    s = _JWT.sub("[redacted]", s)
    s = _PREFIX.sub("[redacted]", s)
    s = _BEARER.sub(r"\1 [redacted]", s)
    s = _KV.sub(lambda m: f"{m.group(1)}=[redacted]", s)
    return s


def redact_tree(value: Any) -> Any:
    """Walk JSON-like structures and redact string leaves."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: redact_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_tree(v) for v in value]
    return value
