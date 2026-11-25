"""Discovery redaction — fail closed on unclassifiable blobs (38 §4)."""

from __future__ import annotations

import re
from typing import Any

from core.redact import redact, redact_string

_SECRET_NAME = re.compile(
    r"(?i)(\.env|\.pem|\.key|credentials|secret|passwd|password|token)"
)
_BINARY_HINT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def redact_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Strip secrets from a discovery inventory before it is stored or prompted."""
    return redact(inventory)


def may_prompt(path: str, content: str | None) -> tuple[bool, str | None]:
    """Return whether content may enter a prompt, and a drop reason if not."""
    name = path.rsplit("/", 1)[-1]
    if _SECRET_NAME.search(name):
        return False, "secret_path"
    if content is None:
        return False, "empty"
    if _BINARY_HINT.search(content[:2000] if content else ""):
        return False, "unclassifiable_binary"
    # Fail closed on very long opaque blobs.
    if len(content) > 200_000 and not content.isprintable():
        return False, "unclassifiable_blob"
    cleaned = redact_string(content)
    if "[REDACTED]" in cleaned and cleaned.count("[REDACTED]") > 5:
        return False, "too_many_secrets"
    return True, None


def safe_text(content: str) -> str:
    return redact_string(content)
