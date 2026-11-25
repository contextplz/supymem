"""Founder-targeted, keyless discovery of published company contacts.

Search results and pages are untrusted evidence, never instructions. No fixed
industry, accelerator share, geography, paid-provider fallback, or outreach.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import time
from typing import Any

from core.research_web.providers import build_provider
from app.gtm import openleads_bridge as ol
from app.gtm import scrapy_leads as scrapy_bridge

log = logging.getLogger(__name__)
_BLOCKED_HOSTS = {"ycombinator.com", "linkedin.com", "twitter.com", "x.com", "facebook.com", "reddit.com", "wikipedia.org", "crunchbase.com", "producthunt.com", "github.com", "medium.com", "youtube.com", "yelp.com", "clutch.co"}
_STOP = {"find", "me", "get", "please", "lead", "leads", "contact", "contacts", "company", "companies", "founder", "founders", "ceo", "cto", "owner", "owners", "decision", "maker", "makers", "in", "the", "a", "an", "and", "or", "for", "with", "of", "b2b", "b2c", "startup", "startups", "based", "located"}
_EXPLICIT_WEB_SOURCES = {"auto", "web", "public_web", "scrapy", "multi"}
_UNAVAILABLE = {
    "opengtm": "OpenGTM discovery requires a founder-scoped provider and credit receipt; its global Gemini credential is not a hosted execution path.",
    "openleads": "OpenLeads is a noncommercial research reference. Commercial license and scoped resource authorization are required before execution.",
}


def _target(query: str, industry: str, region: str | None) -> tuple[str, str, list[str]]:
    query, industry = " ".join(str(query or "").split()), " ".join(str(industry or "").split())
    if not query or len(query) > 500 or len(industry) > 120:
        raise ValueError("lead_target_required_or_too_long")
    # Preserve explicit founder location; only derive it when the caller omitted one.
    split = re.split(r"\s+in\s+", query, maxsplit=1, flags=re.I)
    location = " ".join(str(region or (split[1] if len(split) == 2 else "")).split())
    if len(location) > 120:
        raise ValueError("lead_region_too_long")
    base = split[0] if len(split) == 2 else query
    if location:
        base = re.sub(re.escape(location), " ", base, flags=re.I)
    terms = []
    # Broad B2B is not a software-industry alias. All concrete target words must
    # appear on the company site; a missing match produces a shortfall, not filler.
    # Keep alphanumeric labels whole before removing broad categories. Splitting
    # B2B into two letters incorrectly required a standalone "b" on every site.
    for word in re.findall(r"[^\W_]+", base + " " + industry, re.UNICODE):
        word = word.casefold()
        if not word.isdecimal() and word not in _STOP and word not in terms:
            terms.append(word)
    if not terms:
        raise ValueError("lead_sector_required")
    if region and len(split) == 2:
        query = base.strip() + " in " + location
    return query, location, terms[:20]


def _search_web(query: str, *, limit: int) -> list[dict]:
    # Explicit keyless provider: no global API keys, paid fallback, or model call.
    rows = build_provider("duckduckgo").search(query, max_results=max(1, min(limit, 10)))
    return [r.to_dict() for r in rows[:10]]


def _web_directory_seeds(query: str, *, industry: str, region: str, terms: list[str], limit: int, deadline: float) -> tuple[list[dict], list[str]]:
    target = query if not region or region.casefold() in query.casefold() else f"{query} in {region}"
    if industry and industry.casefold() not in target.casefold() and industry.casefold() not in _STOP:
        target += " " + industry
    searches = [target + " official company contact", target + " company about contact email"]
    seen, seeds, errors = set(), [], []
    for text in searches:
        if len(seeds) >= limit or time.monotonic() >= deadline:
            break
        try:
            hits = _search_web(text, limit=min(10, limit - len(seeds)))
        except Exception:
            log.warning("gtm_lead_search_failed", extra={"provider": "duckduckgo"})
            errors.append("search_unavailable")
            break  # no repeated same failing provider or paid fallback
        for hit in hits:
            url = scrapy_bridge._seed_url(hit.get("url") or "")
            host = scrapy_bridge._host(url)
            if not url or host in seen or any(host == h or host.endswith("." + h) for h in _BLOCKED_HOSTS):
                continue
            seen.add(host)
            # A search title is not a verified organization name or location.
            seeds.append({"website": url, "organization": host, "industry": industry,
                          "target_terms": terms, "target_region": region,
                          "search_evidence": {"url": str(hit.get("url") or "")[:2048],
                                              "title": str(hit.get("title") or "")[:300],
                                              "snippet": str(hit.get("snippet") or "")[:600]}})
            if len(seeds) >= limit:
                break
    return seeds, errors


def status() -> dict[str, Any]:
    crawler = scrapy_bridge.status()
    search_ready = importlib.util.find_spec("ddgs") is not None
    ready = search_ready and crawler["ready"]
    return {"ready": ready, "provider": "public_web", "network_verified": False,
            "hint": "Search and guarded crawler are installed; discovery runs only on an authorized request." if ready else "Search or guarded crawler dependency is unavailable.",
            "search": {"ready": search_ready, "provider": "duckduckgo", "paid": False, "network_verified": False},
            "scrapy": crawler, "openleads": ol.status(),
            "opengtm": {"ready": False, "code": "scoped_provider_required", "hint": _UNAVAILABLE["opengtm"]},
            "email_verification": {"ready": False, "hint": "Published addresses are unverified; no mailbox verification is performed."}}


def find_leads_opengtm(**kwargs) -> dict[str, Any]:
    """Compatibility entry: do not run an unmetered global provider subprocess."""
    raise RuntimeError(_UNAVAILABLE["opengtm"])


def find_leads_combined(*, query: str = "", count: int = 50, industry: str = "", source: str = "auto", deep: bool = True,
                        verified_only: bool = False, use_scrapy: bool = True, region: str | None = None,
                        icp_profile: str = "saas") -> dict[str, Any]:
    del icp_profile  # An old provider preset must not replace the founder's target.
    query, region, terms = _target(query, industry, region)
    count = max(1, min(int(count), 200))
    source = str(source or "auto").lower()
    if source in {"opengtm", "open-gtm", "open_gtm"}:
        raise RuntimeError(_UNAVAILABLE["opengtm"])
    if source not in _EXPLICIT_WEB_SOURCES:
        raise RuntimeError(_UNAVAILABLE["openleads"] if source in {"openleads", "yc", "hn", "producthunt", "companies", "local", "github", "edgar"} else "lead_source_unsupported")
    if not use_scrapy or not status()["ready"]:
        raise RuntimeError("public_contact_discovery_unavailable")
    if verified_only:
        raise RuntimeError("Mailbox verification is not configured; published addresses cannot satisfy verified-only discovery.")
    deadline = time.monotonic() + scrapy_bridge.MAX_SECONDS
    seeds, errors = _web_directory_seeds(query, industry=industry, region=region, terms=terms,
                                        limit=min(scrapy_bridge.MAX_SITES, count * 2), deadline=deadline)
    remaining = deadline - time.monotonic()
    rows = []
    if seeds and remaining > 0:
        try:
            rows = scrapy_bridge.crawl_sites_subprocess(seeds, max_per_site=3 if deep else 1, timeout_sec=remaining)
        except Exception:
            log.warning("gtm_lead_crawl_failed")
            errors.append("crawler_unavailable")
    leads = rows[:count]
    for row in leads:
        row["target_match"] = {"basis": "company_page_text", "terms": terms, "requested_region": region}
    state = "partial" if leads and len(leads) < count else "completed" if leads else "unavailable" if errors else "no_matches"
    if state == "unavailable":
        message = "Public contact discovery could not finish because search or page retrieval was unavailable. This does not establish that no matching companies exist."
    else:
        message = f"Found {len(leads)} of {count} requested contacts matching the market and location in published company pages."
        if leads:
            message += " All addresses are unverified."
    if errors and leads:
        message += " Search or page retrieval was incomplete, so these results may be incomplete."
    if len(leads) < count and not errors:
        message += " The bounded search returned no additional supported matches; no unrelated contacts were added."
    return {"leads": leads, "count": len(leads), "requested_count": count, "shortfall": max(0, count - len(leads)),
            "state": state, "safe": 0, "risky": 0, "unverified": len(leads), "query": query, "industry": industry, "region": region,
            "source": "public_web", "deep": deep, "message": message, "openleads_count": 0, "scrapy_count": len(leads),
            "source_counts": {"public_web": len(leads)}, "slice_counts": {"public_web": len(leads)}, "web_seeds": len(seeds),
            "errors": sorted(set(errors)), "log_tail": None}


leads_to_json_bytes = ol.leads_to_json_bytes
leads_to_xlsx_bytes = ol.leads_to_xlsx_bytes
leads_to_csv_bytes = ol.leads_to_csv_bytes
