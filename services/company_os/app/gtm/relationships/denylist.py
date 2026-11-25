"""Hosts work orders must not drive (doc 130 OT-10)."""

from __future__ import annotations

from urllib.parse import urlparse


def linkedin_host_blocked(url: str) -> bool:
    """True when `url` targets linkedin.com (including subdomains)."""
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return host == "linkedin.com" or host.endswith(".linkedin.com")
