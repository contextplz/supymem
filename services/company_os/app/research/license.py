"""Licence gate for OSS reuse recommendations."""

from __future__ import annotations

OK = {"mit", "apache-2.0", "apache 2.0", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense"}
AVOID = {"agpl-3.0", "agpl", "sspl", "commons-clause"}
REVIEW = {"gpl-3.0", "gpl-2.0", "lgpl-3.0", "lgpl"}


def evaluate_license(name: str | None) -> dict[str, object]:
    raw = (name or "unknown").strip().lower()
    if raw in OK:
        return {"ok": True, "recommendation": "adopt", "license": name}
    if raw in AVOID:
        return {"ok": False, "recommendation": "avoid", "license": name}
    if raw in REVIEW:
        return {"ok": True, "recommendation": "fork", "license": name}
    return {"ok": None, "recommendation": "reference", "license": name or "unknown"}
