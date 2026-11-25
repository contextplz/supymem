"""Hermes-shaped web explore: search, then extract readable pages.

Contract matches Nous Hermes `web_search` + `web_extract` (MIT) — search
returns titles/URLs, extract pulls markdown with a hard character budget.
Backends stay ours: Firecrawl/OpenWorker search + OpenWorker/Jina fetch,
plus Agent-Reach specialist platforms when the query names them or contains
their URLs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.local_mode import is_local
from app.credits.errors import CreditError
from app.credits.metered import search as metered_search
from app.research.web_fetch import fetch_page_markdown
from app.signals.research import _provider_from_env

_EXCERPT = 1800
_MAX_EXTRACT = 3


async def explore_web(
    query: str,
    *,
    max_results: int = 5,
    extract: int = _MAX_EXTRACT,
    excerpt_chars: int = _EXCERPT,
    public_only: bool = False,
) -> dict[str, Any]:
    """Search the live web, then read the top pages. Never invent sources."""
    q = (query or "").strip()
    if not q:
        return {"query": "", "mode": "empty", "hits": [], "pages": [], "summary": "No query."}

    from app.chat.link_context import site_read_url

    if url := site_read_url(q):
        from httpx import HTTPError

        from app.chat.recovery import log_founder_exception
        from app.tools import reader

        try:
            if is_local():
                from app.research.local_providers import (
                    READER_UNAVAILABLE,
                    fetch_page,
                    scrape_provider,
                )
                if scrape_provider() is None:
                    return {"query": q, "mode": "error", "hits": [], "pages": [],
                            "error_code": "page_read_unavailable", "summary": READER_UNAVAILABLE}
                text = await fetch_page(url)
                if text.strip():
                    hit = {"title": url, "url": url, "snippet": text[:excerpt_chars]}
                    return {"query": q, "mode": "ok", "hits": [hit],
                            "pages": [{**hit, "excerpt": text[:excerpt_chars]}], "platforms": []}
                return {"query": q, "mode": "error", "hits": [], "pages": [], "error_code": "page_read_unavailable"}
            page = await asyncio.to_thread(reader.read_page, url)
            if page.text.strip():
                hit = {"title": page.title or page.final_url, "url": page.final_url, "snippet": page.text[:excerpt_chars]}
                return {"query": q, "mode": "ok", "hits": [hit],
                        "pages": [{**hit, "excerpt": page.text[:excerpt_chars]}], "platforms": []}
        except CreditError:
            raise
        except (OSError, ValueError, HTTPError) as exc:
            log_founder_exception(exc, None, "research.page_read")
        return {"query": q, "mode": "error", "hits": [], "pages": [], "error_code": "page_read_unavailable"}

    search = metered_search
    if is_local():
        from app.research.local_providers import SEARCH_UNAVAILABLE, _blocked_message, search_provider
        from app.research.local_providers import search as local_search
        search = local_search
        provider = search_provider()
        if provider is None:
            return {"query": q, "mode": "error", "hits": [], "pages": [], "platforms": [],
                    "error_code": "search_unavailable", "summary": _blocked_message()}
    else:
        provider = _provider_from_env()
    hits: list[dict[str, Any]] = []
    search_error = ""
    from app.tenancy import current_or_none
    scope = current_or_none()
    configured = getattr(provider, "name", "") not in {"local_stub", "duckduckgo", "openworker"}
    if public_only and not configured and not is_local():
        return {"query": q, "mode": "error", "hits": [], "pages": [], "error_code": "search_unavailable"}
    try:
        hits = await search(provider, q, purpose="research.explore.search",
                                    workspace_id=scope.workspace_id if scope else None,
                                    venture_id=scope.venture_id if scope else None)
    except CreditError:
        raise
    except Exception as exc:  # noqa: BLE001
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if configured and (type(status) is not int or status >= 500):
            raise  # The paid outcome needs reconciliation; never replace it with free results.
        search_error = "search_rate_limited" if status == 429 else "search_unavailable"
        from app.analytics.error_events import actor_person_id, log_error_event
        log_error_event("research", feature="research", code=search_error,
                        venture_id=scope.venture_id if scope else None,
                        person_id=actor_person_id(scope.actor) if scope else None,
                        status=status if type(status) is int else None,
                        provider=getattr(provider, "name", "unknown"))

    hits = [h for h in (hits or []) if isinstance(h, dict) and h.get("url")][:max_results]
    platforms = [] if is_local() or public_only or search_error and configured else await _platform_lookups(q, max_results=max_results)
    platforms = [{**block, "error": "platform_lookup_unavailable" if block.get("error") else None}
                 for block in platforms if isinstance(block, dict)]
    for block in platforms:
        for hit in block.get("hits") or []:
            if isinstance(hit, dict) and hit.get("url"):
                hits.append(hit)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for hit in hits:
        url = str(hit.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(hit)
    hits = deduped[: max(max_results + 5, max_results)]

    if not hits:
        return {
            "query": q,
            "mode": "error" if search_error else (
                "empty" if getattr(provider, "name", "") == "local_stub" else "no_hits"
            ),
            "provider": getattr(provider, "name", "unknown"),
            "hits": [],
            "pages": [],
            "platforms": platforms,
            "error_code": search_error or None,
            "summary": (SEARCH_UNAVAILABLE if is_local() and search_error else
                        "Live search is temporarily rate-limited." if search_error == "search_rate_limited" else
                        "Live search is unavailable right now." if search_error else "No live sources were found."),
        }

    reader_gap = ""
    if is_local() and extract > 0:
        from app.research.local_providers import READER_UNAVAILABLE, scrape_provider
        if scrape_provider() is None:
            reader_gap = " " + READER_UNAVAILABLE
    pages = await _extract_pages(hits[: max(0, extract)], excerpt_chars=excerpt_chars) if extract > 0 and not reader_gap else []
    read_n = sum(1 for p in pages if p.get("excerpt"))
    platform_names = [p.get("platform") for p in platforms if p.get("hits")]
    extra = f" + Agent-Reach {', '.join(str(n) for n in platform_names)}" if platform_names else ""
    return {
        "query": q,
        "mode": "live",
        "provider": getattr(provider, "name", "unknown"),
        "hits": hits,
        "pages": pages,
        "platforms": platforms,
        "summary": (
            f"The search provider was unavailable; found {len(hits)} links via platform lookups and read {read_n} pages."
            if search_error else
            f"Searched “{q[:80]}” via {getattr(provider, 'name', 'web')}{extra}: "
            f"{len(hits)} hits, read {read_n} page(s).{reader_gap}"
        ),
    }


async def _platform_lookups(query: str, *, max_results: int) -> list[dict[str, Any]]:
    if is_local():
        return []
    try:
        from app.research.agent_reach_bridge import (
            detect_platforms,
            search_platform,
            urls_in,
            vendored,
        )
    except Exception:
        return []
    if not vendored():
        return []

    jobs: list[tuple[str, str]] = []
    for url in urls_in(query):
        jobs.append(("url", url))
    for name in detect_platforms(query):
        jobs.append((name, query))
    if not jobs:
        return []

    async def one(kind: str, value: str) -> dict[str, Any]:
        if kind == "url":
            try:
                from app.research.agent_reach_bridge import channel_for_url, read_url

                ch = channel_for_url(value)
                body = await asyncio.to_thread(read_url, value, specialist_only=True)
                if not body:
                    body = await asyncio.to_thread(fetch_page_markdown, value)
                return {
                    "platform": getattr(ch, "name", None) or "web",
                    "backend": "agent-reach",
                    "hits": [
                        {
                            "title": value,
                            "url": value,
                            "snippet": (body or "")[:240],
                        }
                    ],
                    "error": None,
                }
            except Exception:  # noqa: BLE001
                return {"platform": "url", "hits": [], "error": "platform_lookup_unavailable"}
        return await asyncio.to_thread(search_platform, kind, value, limit=max_results)

    return list(await asyncio.gather(*(one(kind, value) for kind, value in jobs[:4])))


async def _extract_pages(hits: list[dict[str, str]], *, excerpt_chars: int) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(3)

    async def one(hit: dict[str, str]) -> dict[str, Any]:
        url = str(hit.get("url") or "")
        row: dict[str, Any] = {
            "url": url,
            "title": hit.get("title") or url,
            "snippet": (hit.get("snippet") or "")[:400],
        }
        if not url:
            return row
        async with sem:
            try:
                body = await asyncio.to_thread(fetch_page_markdown, url)
                text = (body or "").strip()
                row["excerpt"] = text[:excerpt_chars]
                row["chars"] = len(text)
            except Exception:  # noqa: BLE001
                row["error"] = "page_read_unavailable"
        return row

    return list(await asyncio.gather(*(one(h) for h in hits)))


def render_explore(result: dict[str, Any], *, pages: bool = True) -> str:
    if is_local() and result.get("error_code") in {"search_unavailable", "page_read_unavailable"} and result.get("summary"):
        return str(result["summary"])
    if result.get("mode") in {"error", "empty", "no_hits"}:
        from app.chat.search_recovery import search_failure_reply
        return search_failure_reply(str(result.get("query") or ""), str(result.get("error_code") or ""))["body_md"]
    lines = [f"**{result.get('summary') or 'Web explore'}**", ""]
    for i, hit in enumerate(result.get("hits") or [], 1):
        title = hit.get("title") or hit.get("url")
        url = hit.get("url") or ""
        lines.append(f"{i}. [{title}]({url})")
        if hit.get("snippet"):
            lines.append(f"   {hit['snippet'][:220]}")
    for block in result.get("platforms") or []:
        if block.get("error") and not block.get("hits"):
            lines.append("- One specialist platform lookup was unavailable; its results were not verified.")
    if is_local() and any(page.get("error") for page in result.get("pages") or []):
        lines.append("Some pages are unavailable. You can check your page reader in Settings or try another public source.")
    pages = [p for p in (result.get("pages") or []) if p.get("excerpt")] if pages else []
    if pages:
        lines.append("")
        lines.append("**What the pages actually say**")
        for page in pages[:3]:
            lines.append(f"- **{page.get('title')}**{page.get('url')}")
            excerpt = " ".join(str(page.get("excerpt") or "").split())
            lines.append(f"  {excerpt[:500]}{'…' if len(excerpt) > 500 else ''}")
    return "\n".join(lines).strip()
