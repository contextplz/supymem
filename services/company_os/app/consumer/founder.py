"""Founder admin allowlist. Emails come from env, never from a public catalogue."""

from __future__ import annotations

import os


def founder_emails() -> frozenset[str]:
    raw = (
        os.environ.get("FOUNDER_ADMIN_EMAILS")
        or os.environ.get("ADMIN_BOOTSTRAP_EMAILS")
        or ""
    )
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_founder_email(email: str | None) -> bool:
    addr = (email or "").strip().lower()
    if not addr:
        return False
    return addr in founder_emails()
