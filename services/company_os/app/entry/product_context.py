"""Founder-facing product context for the existing-product entry path."""

from __future__ import annotations

import re
from typing import Any


_LEGACY_SUFFIX = re.compile(
    r"(?:\.\s*)?(?:website|app|both|internal)\s*·\s*"
    r"(?:github|local|url|unsure|[^·]+)\s*·\s*[^·]+\s*·\s*[^·]+\s*$",
    re.IGNORECASE,
)
_PRODUCT_KIND = {
    "website": "website",
    "app": "app",
    "both": "app and website",
    "internal": "internal tool",
}
_FOUNDER_HAS_PRODUCT = re.compile(
    r"^(?:i|we)\s+(?:already\s+)?(?:have|built|launched|run)\s+"
    r"(?:an?\s+|our\s+|my\s+)?(?:existing\s+)?(?:(?:saas|software)\s+)?(?:product|app|mvp|saas|website)\b",
    re.IGNORECASE,
)


def product_context(text: str, answers: dict[str, Any]) -> tuple[str, str]:
    """Return (description, naming source) without questionnaire codes.

    Older clients sent the questionnaire as `Existing product: app · unsure ...`.
    Keep accepting those requests while new clients send plain founder prose.
    """
    summary = str(answers.get("product_summary") or "").strip()
    original = text.strip()
    raw = original
    legacy_codes = raw.lower().startswith("existing product:")
    if legacy_codes:
        raw = raw.split(":", 1)[1].strip()
        raw = _LEGACY_SUFFIX.sub("", raw).strip(" .")
        if " · " in raw:
            raw = ""
    else:
        lead = _FOUNDER_HAS_PRODUCT.match(raw)
        if lead:
            rest = raw[lead.end():].strip()
            if rest.startswith((".", "!", "?")):
                followup = rest[1:].strip()
                raw = followup if re.match(r"^(?:it|this\s+(?:product|app))\s+(?:helps?|lets?|enables?)\b", followup, re.IGNORECASE) else ""
            elif not re.match(r"^(?:that|which|for|to)\b", rest, re.IGNORECASE):
                raw = ""
    description = summary or raw
    if description and description.lower() not in {"existing product", "app", "website", "both", "internal"}:
        return description[:2000], description
    kind = _PRODUCT_KIND.get(str(answers.get("what") or "").lower(), "product")
    fallback = f"Existing {kind}"
    return (fallback if legacy_codes else original[:2000] or fallback), fallback
