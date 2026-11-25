"""ICP-fit lead discovery across the public web (Loop 1 supply engine).

Not accelerator-only. Searches the open web for companies matching the ICP,
enriches each site for published emails / LinkedIn URLs, and upserts into the
GTM relationship store. OpenLeads is studied for architecture only (PolyForm NC)
— this module does not import it.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from app.db.models import GtmContact
from app.gtm.relationships.dossier import attach_dossier, build_dossier_from_signal
from app.gtm.relationships.enrich import enrich_company_site
from app.gtm.relationships.store import ingest_signal, upsert_company, upsert_contact
from app.state import db
from core.clock import utcnow

log = logging.getLogger(__name__)

_URL = re.compile(r"https?://[^\s\"'<>)]+")


def _default_icp_queries(product_hint: str | None = None) -> list[str]:
    """Search queries that find real company sites matching the ICP."""
    hint = (product_hint or "AI agent platform for startups").strip()
    return [
        f'"{hint}" official site -reddit -guide -blog',
        "AI sales agent SaaS pricing contact",
        "outbound email automation software for startups about",
        "AI SDR platform company team page",
        "GTM automation software for founders contact@",
    ]


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).hostname
    if not host:
        return None
    return host.removeprefix("www.").lower()


def _search_web(query: str, *, limit: int = 8, workspace_id: str | None = None,
                venture_id: str | None = None) -> list[dict[str, str]]:
    """Use the shared transport; admission precedes every platform-paid search."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    import os

    from app.core.local_mode import is_local
    from app.credits.errors import CreditsUnavailable
    from app.credits.metered import call_sync, price_micro
    from app.signals.research import HttpResearchProvider, _provider_from_env
    from app.tenancy import current_or_none

    limit = max(1, min(limit, 20))
    scope = current_or_none()
    if scope and ((workspace_id and workspace_id != scope.workspace_id)
                  or (venture_id and venture_id != scope.venture_id)):
        raise PermissionError("Search does not belong to the active venture.")
    wid = workspace_id or (scope.workspace_id if scope else None)
    vid = venture_id or (scope.venture_id if scope else None)
    provider = _provider_from_env()
    if provider.name == "exa":
        limit = min(limit, 10)  # The configured Auto-search rate covers up to ten results.
    if isinstance(provider, HttpResearchProvider):
        provider._limit = limit

    async def search():
        if is_local():
            from app.research.local_providers import search as local_search
            return await local_search(provider, query, purpose="gtm.discovery.search",
                workspace_id=wid, venture_id=vid, limit=limit)
        return await provider.search(query)

    def fetch():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(search())
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(copy_context().run, asyncio.run, search()).result()

    name = provider.name
    if name == "openworker" and os.getenv("OPENWORKER_SEARCH_PROVIDER", "duckduckgo").lower() != "duckduckgo":
        raise CreditsUnavailable("search provider pricing needs confirmation")
    if is_local() or name in {"local_stub", "duckduckgo", "openworker"}:
        results = fetch()
    else:
        if name not in {"firecrawl", "exa", "tavily"}:
            raise CreditsUnavailable("search provider pricing is not available")
        quantity = max(1, (limit + 9) // 10) if name == "firecrawl" else 1
        results = call_sync(fetch, purpose="gtm.discovery.search", provider=name,
                            estimate_micro=price_micro(name + "_search", quantity),
                            workspace_id=wid, venture_id=vid)
    seen, unique = set(), []
    for row in results:
        if not isinstance(row, dict):
            continue
        domain = _domain_of(row.get("url"))
        if domain and domain not in seen:
            seen.add(domain)
            unique.append(row)
    return unique[:limit]


def _company_name_from_hit(title: str, domain: str) -> str:
    # Prefer clean brand from domain; titles are often listicle SEO.
    base = domain.split(".")[0] if domain else "Company"
    brand = base.replace("-", " ").title()
    name = (title or "").split("|")[0].split("-")[0].split("–")[0].strip()
    name = re.sub(r"\b(about|team|contact|home|official site|best|top|guide)\b", "", name, flags=re.I)
    name = name.strip(" -—–|:")
    if 2 <= len(name) <= 40 and name.lower() not in {"ai", "the", "how to"}:
        # If title looks like a brand (few words), keep it; else domain brand.
        if len(name.split()) <= 3 and not name.lower().startswith(("how ", "top ", "best ")):
            return name
    return brand


async def discover_leads(
    workspace_id: str,
    *,
    product_hint: str | None = None,
    queries: list[str] | None = None,
    limit: int = 15,
    venture_id: str | None = None,
) -> dict[str, Any]:
    """Search → enrich site → upsert company/contact/signal/dossier."""
    qlist = queries or _default_icp_queries(product_hint)
    seen_domains: set[str] = set()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for query in qlist:
        if len(created) >= limit:
            break
        hits = _search_web(query, limit=8, workspace_id=workspace_id, venture_id=venture_id)
        for hit in hits:
            if len(created) >= limit:
                break
            url = hit.get("url") or ""
            domain = _domain_of(url)
            if not domain or domain in seen_domains:
                continue
            # Skip obvious non-company hosts
            if any(
                domain.endswith(x) or domain == x or x in domain
                for x in (
                    "linkedin.com",
                    "twitter.com",
                    "x.com",
                    "facebook.com",
                    "youtube.com",
                    "wikipedia.org",
                    "github.com",
                    "medium.com",
                    "reddit.com",
                    "ycombinator.com",
                    "producthunt.com",
                    "bing.com",
                    "google.com",
                    "yahoo.com",
                    "baidu.com",
                    "origami.chat",
                    "mindstudio.ai",  # listicles / guides
                    "substack.com",
                    "forbes.com",
                    "techcrunch.com",
                    "crunchbase.com",
                )
            ):
                skipped.append({"domain": domain, "reason": "non_company_host"})
                continue
            seen_domains.add(domain)
            name = _company_name_from_hit(hit.get("title") or "", domain)

            contacts = enrich_company_site(domain, max_pages=4)
            email = contacts.emails[0] if contacts.emails else None
            linkedin = contacts.linkedin_urls[0] if contacts.linkedin_urls else None

            with db()() as session:
                company = upsert_company(
                    session,
                    workspace_id=workspace_id,
                    name=name,
                    domain=domain,
                    description=(hit.get("snippet") or "")[:500],
                    evidence_url=url,
                )
                contact = upsert_contact(
                    session,
                    workspace_id=workspace_id,
                    company_id=company.id,
                    email=email,
                    first_name=None,
                    role="Founder",
                    source="web_icp_search",
                    email_confidence="safe" if email else "none",
                    lawful_basis="legitimate_interest",
                    lawful_basis_note=(
                        f"Public company website contact for B2B outreach; "
                        f"ICP query={query!r}"
                    ),
                )
                if linkedin and not contact.linkedin_url:
                    contact.linkedin_url = linkedin
                ingest_signal(
                    session,
                    workspace_id=workspace_id,
                    kind="web_icp",
                    company_name=name,
                    domain=domain,
                    evidence_url=url,
                    payload={
                        "query": query,
                        "title": hit.get("title"),
                        "snippet": hit.get("snippet"),
                        "pages_fetched": list(contacts.pages_fetched),
                        "emails_found": list(contacts.emails),
                        "linkedin_found": list(contacts.linkedin_urls),
                    },
                    contact_email=email,
                    contact_role="Founder",
                    email_confidence="safe" if email else "none",
                )
                contact_id = contact.id
                session.commit()

            hook_bits = []
            if hit.get("snippet"):
                hook_bits.append(hit["snippet"][:80])
            hook_bits.append(f"fits ICP via “{query[:40]}”")
            dossier = build_dossier_from_signal(
                company_name=name,
                signal_kind="web_icp",
                evidence_url=url,
                description=hit.get("snippet") or name,
                person_name=None,
                role="Founder",
            )
            # Prefer a more specific hook from the search snippet
            if hit.get("snippet"):
                dossier["hook"] = f"building {name}: {(hit.get('snippet') or '')[:70]}"
                dossier["why_we_care"] = f"Matched ICP search: {query}"
            try:
                attach_dossier(contact_id, dossier, venture_id=venture_id or workspace_id)
            except Exception as exc:  # noqa: BLE001
                skipped.append({"domain": domain, "reason": f"dossier:{exc}"})
                continue

            created.append(
                {
                    "contact_id": contact_id,
                    "company": name,
                    "domain": domain,
                    "email": email,
                    "linkedin_url": linkedin,
                    "pages_fetched": list(contacts.pages_fetched),
                    "query": query,
                    "evidence_url": url,
                }
            )

    return {
        "workspace_id": workspace_id,
        "created": created,
        "created_count": len(created),
        "skipped": skipped[:50],
        "queries": qlist,
        "at": utcnow().isoformat(),
    }


def list_enriched_prospects(workspace_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from app.db.models import GtmCompany

    with db()() as session:
        rows = session.scalars(
            select(GtmContact)
            .where(GtmContact.workspace_id == workspace_id)
            .order_by(GtmContact.created_at.desc())
            .limit(limit)
        ).all()
        out = []
        for c in rows:
            company = session.get(GtmCompany, c.company_id) if c.company_id else None
            out.append(
                {
                    "id": c.id,
                    "email": c.email or "",
                    "first_name": c.first_name,
                    "company": company.canonical_name if company else None,
                    "domain": company.domain if company else None,
                    "linkedin_url": c.linkedin_url,
                    "status": c.relationship_stage,
                    "hook": c.dossier_hook,
                    "email_confidence": c.email_confidence,
                    "created_at": c.created_at.isoformat() if c.created_at else "",
                }
            )
        return out
