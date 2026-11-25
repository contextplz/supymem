"""Hunter.io REST client (direct API, not Composio).

Used to resolve a *published or finder-scored* email for a person/company.
Never fabricates an address — only returns what Hunter returned.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

HUNTER_BASE = "https://api.hunter.io/v2"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ROLE_PREFERRED = (
    "founder",
    "co-founder",
    "ceo",
    "cto",
    "coo",
    "owner",
    "partner",
)


def configured() -> bool:
    return bool(os.environ.get("HUNTER_API_KEY", "").strip())


def _key() -> str:
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("HUNTER_API_KEY is not configured.")
    return key


def _get(path: str, params: dict[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
    q = {k: v for k, v in params.items() if v not in (None, "")}
    q["api_key"] = _key()
    with httpx.Client(timeout=timeout) as client:
        res = client.get(f"{HUNTER_BASE}{path}", params=q)
    if res.status_code == 401:
        raise RuntimeError("Hunter API key was rejected.")
    if res.status_code == 429:
        raise RuntimeError("Hunter rate limit hit. Try again shortly.")
    if res.status_code >= 400:
        raise RuntimeError(f"Hunter {path} returned HTTP {res.status_code}.")
    data = res.json()
    return data if isinstance(data, dict) else {}


def _str(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _confidence_from_score(score: Any, verification: str | None) -> str:
    ver = (verification or "").lower()
    if ver == "valid":
        return "safe"
    if ver in {"accept_all", "webmail"}:
        return "risky"
    if ver in {"invalid", "disposable", "unknown"}:
        return "guess" if ver == "unknown" else "none"
    try:
        n = int(score)
    except (TypeError, ValueError):
        return "risky"
    if n >= 80:
        return "safe"
    if n >= 50:
        return "risky"
    return "guess"


def normalize_person(payload: dict[str, Any], *, input_email: str | None = None) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        data = {}
    name_obj = data.get("name") if isinstance(data.get("name"), dict) else {}
    emp = data.get("employment") if isinstance(data.get("employment"), dict) else {}
    linkedin = data.get("linkedin") if isinstance(data.get("linkedin"), dict) else {}
    twitter = data.get("twitter") if isinstance(data.get("twitter"), dict) else {}

    full = _str(name_obj.get("fullName")) or _str(data.get("name"))
    first = _str(name_obj.get("givenName")) or _str(data.get("first_name"))
    last = _str(name_obj.get("familyName")) or _str(data.get("last_name"))
    if full and not first:
        parts = full.split()
        first, last = parts[0], " ".join(parts[1:]) or last

    handle = _str(linkedin.get("handle")) or _str(data.get("linkedin"))
    linkedin_url = None
    if handle:
        linkedin_url = (
            handle if handle.startswith("http") else f"https://www.linkedin.com/in/{handle.lstrip('/')}/"
        )

    email = _str(data.get("email")) or input_email
    score = data.get("score") or (data.get("verification") or {}).get("score") if isinstance(data.get("verification"), dict) else data.get("score")
    ver = None
    if isinstance(data.get("verification"), dict):
        ver = _str(data["verification"].get("status"))

    return {
        "first_name": first,
        "last_name": last,
        "name": full or " ".join(p for p in (first, last) if p) or None,
        "title": _str(emp.get("title")) or _str(data.get("position")) or _str(data.get("title")),
        "company": _str(emp.get("name")) or _str(data.get("company")) or _str(data.get("organization")),
        "domain": _str(emp.get("domain")) or _str(data.get("domain")),
        "location": _str(data.get("location")),
        "linkedin_url": linkedin_url,
        "x_handle": _str(twitter.get("handle")) or _str(data.get("twitter")),
        "email": (email or "").lower() if email else None,
        "email_confidence": _confidence_from_score(score, ver) if email else "none",
        "source": "hunter",
    }


def people_find(email: str) -> dict[str, Any] | None:
    email_n = email.strip().lower()
    if not _EMAIL_RE.match(email_n):
        return None
    try:
        raw = _get("/people/find", {"email": email_n})
    except Exception as exc:  # noqa: BLE001
        log.info("hunter people/find failed: %s", exc)
        return None
    person = normalize_person(raw, input_email=email_n)
    if not any(person.get(k) for k in ("first_name", "title", "company", "linkedin_url")):
        person["email"] = email_n
        return person
    person["email"] = email_n
    return person


def email_finder(
    *,
    domain: str,
    first_name: str | None = None,
    last_name: str | None = None,
    full_name: str | None = None,
) -> dict[str, Any] | None:
    domain = domain.strip().lower().removeprefix("www.")
    if not domain:
        return None
    params: dict[str, Any] = {"domain": domain}
    if first_name:
        params["first_name"] = first_name
    if last_name:
        params["last_name"] = last_name
    if full_name and not first_name:
        params["full_name"] = full_name
    if not (first_name or full_name):
        return None
    try:
        raw = _get("/email-finder", params)
    except Exception as exc:  # noqa: BLE001
        log.info("hunter email-finder failed: %s", exc)
        return None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    email = _str(data.get("email")) if isinstance(data, dict) else None
    if not email or not _EMAIL_RE.match(email):
        return None
    person = normalize_person(raw, input_email=email)
    person["email"] = email.lower()
    return person


def domain_search(domain: str, *, limit: int = 10) -> dict[str, Any] | None:
    domain = domain.strip().lower().removeprefix("www.")
    if not domain:
        return None
    try:
        raw = _get("/domain-search", {"domain": domain, "limit": limit})
    except Exception as exc:  # noqa: BLE001
        log.info("hunter domain-search failed: %s", exc)
        return None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    emails = data.get("emails") if isinstance(data, dict) else None
    if not isinstance(emails, list) or not emails:
        return None

    def rank(item: Any) -> tuple[int, int]:
        if not isinstance(item, dict):
            return (99, 0)
        pos = (_str(item.get("position")) or "").lower()
        for i, pref in enumerate(_ROLE_PREFERRED):
            if pref in pos:
                return (i, -int(item.get("confidence") or 0))
        return (50, -int(item.get("confidence") or 0))

    picked = min((e for e in emails if isinstance(e, dict) and _str(e.get("value"))), key=rank, default=None)
    if not isinstance(picked, dict):
        return None
    email = _str(picked.get("value"))
    if not email:
        return None
    ver = picked.get("verification") if isinstance(picked.get("verification"), dict) else {}
    return {
        "first_name": _str(picked.get("first_name")),
        "last_name": _str(picked.get("last_name")),
        "name": " ".join(
            p for p in (_str(picked.get("first_name")), _str(picked.get("last_name"))) if p
        ) or None,
        "title": _str(picked.get("position")),
        "company": _str(data.get("organization")) if isinstance(data, dict) else None,
        "domain": domain,
        "linkedin_url": _str(picked.get("linkedin")),
        "email": email.lower(),
        "email_confidence": _confidence_from_score(
            picked.get("confidence"), _str(ver.get("status")) if isinstance(ver, dict) else None
        ),
        "source": "hunter",
    }


def enrich_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Fill missing email / LinkedIn on a lead dict. Mutates and returns it."""
    email = (lead.get("email") or "").strip().lower()
    domain = (lead.get("website") or lead.get("domain") or "").strip()
    if domain.startswith("http"):
        from urllib.parse import urlparse

        domain = (urlparse(domain).hostname or "").removeprefix("www.")
    else:
        domain = domain.removeprefix("www.").split("/")[0]
    first = (lead.get("first_name") or "").strip() or None
    last = (lead.get("last_name") or "").strip() or None
    full = " ".join(p for p in (first, last) if p) or None

    found: dict[str, Any] | None = None
    if email:
        found = people_find(email)
    if found is None and domain and (first or full):
        found = email_finder(domain=domain, first_name=first, last_name=last, full_name=full)
    if found is None and domain and not email:
        found = domain_search(domain)

    if not found:
        return lead

    for key in (
        "first_name",
        "last_name",
        "title",
        "linkedin_url",
        "email",
        "email_confidence",
    ):
        if found.get(key) and not lead.get(key):
            lead[key] = found[key]
    if found.get("company") and not lead.get("organization"):
        lead["organization"] = found["company"]
    if found.get("email"):
        lead["email"] = found["email"]
        lead.setdefault("email_tier", "safe" if found.get("email_confidence") == "safe" else "risky")
        lead.setdefault("source", f"{lead.get('source') or 'openleads'}+hunter")
    return lead
