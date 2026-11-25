"""Public-page contact enrichment (clean-room; OpenLeads-inspired, not imported).

Extracts **published** emails and LinkedIn profile URLs from a company website.
Never scrapes LinkedIn itself. Never invents emails — only addresses that appear
on the page (mailto: or plain text). Guessed permutations are out of scope for
the relationship track (doc 115).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from app.research.web_fetch import fetch_page_markdown

_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9._%+-])"
)
_LINKEDIN = re.compile(
    r"https?://(?:www\.)?linkedin\.com/(?:in|company)/[A-Za-z0-9_-]+/?",
    re.I,
)
_DISPOSABLE_LOCALS = {
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "abuse",
}
_ROLE_PREFERRED = ("founder", "ceo", "hello", "team", "contact", "hi", "info")


@dataclass(frozen=True)
class PageContacts:
    emails: tuple[str, ...]
    linkedin_urls: tuple[str, ...]
    pages_fetched: tuple[str, ...]


def _normalize_site(domain_or_url: str) -> str:
    raw = domain_or_url.strip()
    if not raw.startswith("http"):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").removeprefix("www.")
    return f"https://{host}"


def _email_ok(email: str, *, site_host: str) -> bool:
    email = email.lower().strip().rstrip(".,;)>")
    if "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if local.split("+")[0] in _DISPOSABLE_LOCALS:
        return False
    # Prefer same-domain addresses; allow common public contact domains weakly.
    if domain == site_host or domain.endswith("." + site_host):
        return True
    # Still keep cross-domain if it's clearly a person email on the page.
    return local not in {"example", "email", "you", "name"}


def _rank_emails(emails: list[str]) -> list[str]:
    def key(e: str) -> tuple[int, str]:
        local = e.split("@", 1)[0]
        for i, pref in enumerate(_ROLE_PREFERRED):
            if local == pref or local.startswith(pref):
                return (i, e)
        if "." in local:
            return (50, e)
        return (80, e)

    return sorted(dict.fromkeys(emails), key=key)


def extract_from_text(text: str, *, site_host: str) -> tuple[list[str], list[str]]:
    emails = [
        e.lower().rstrip(".,;)>")
        for e in _EMAIL.findall(text or "")
        if _email_ok(e, site_host=site_host)
    ]
    linkedin = list(dict.fromkeys(_LINKEDIN.findall(text or "")))
    return _rank_emails(emails), linkedin


def enrich_company_site(domain_or_url: str, *, max_pages: int = 4) -> PageContacts:
    """Fetch homepage + common contact paths; return published contacts."""
    base = _normalize_site(domain_or_url)
    host = urlparse(base).hostname or ""
    host = host.removeprefix("www.")
    paths = ["", "/about", "/team", "/contact", "/about-us", "/company"]
    pages: list[str] = []
    emails: list[str] = []
    linkedin: list[str] = []

    for path in paths[:max_pages]:
        url = urljoin(base + "/", path.lstrip("/")) if path else base
        if url in pages:
            continue
        try:
            text = fetch_page_markdown(url, timeout_s=20.0)
        except Exception:
            continue
        if not text or len(text) < 40:
            continue
        pages.append(url)
        e, li = extract_from_text(text, site_host=host)
        emails.extend(e)
        linkedin.extend(li)
        if emails and linkedin:
            break

    return PageContacts(
        emails=tuple(_rank_emails(emails)[:5]),
        linkedin_urls=tuple(list(dict.fromkeys(linkedin))[:5]),
        pages_fetched=tuple(pages),
    )
