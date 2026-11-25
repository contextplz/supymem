"""Prior-attempt discovery inside the existing journaled founder research run."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import timezone
from urllib.parse import urlsplit

from app.core.jobs import FatalError, RetryableError
from app.research import execution as ex
from app.research.metered import synthesize
from app.research.prior_art import adapters, plan
from app.research.prior_art.entities import compile_prior_art, entity_key
from app.research.source_quality import relevant_company_lead
from app.research.presentation import gap, record_gaps, saved_gaps
from core.clock import utcnow

CALL_BUDGET = 60  # Includes all actual free-adapter child requests, not only journal entries.
FATAL = {"research_operation_in_progress", "research_lease_lost", "research_scope_mismatch", "research_outcome_uncertain", "research_cancelled"}
BUDGET = {"research_cost_budget_exhausted", "research_call_budget_exhausted"}


def enabled() -> bool:
    return os.getenv("RESEARCH_PRIOR_ART_ENABLED", "1").lower() not in {"0", "false", "no"}


def prepare(did: str, payload: dict, snapshot: dict) -> tuple[dict, list[str]]:
    row = ex.read(did)
    runtime = json.loads(row.summary)["_runtime"]
    if runtime.get("prior_art_plan") is not None:
        return runtime["prior_art_plan"], saved_gaps({"gap_items": runtime.get("prior_art_gap_items"), "gaps": runtime.get("prior_art_gaps", [])}, "gaps")
    def ceiling(row, doc, session):
        doc["_runtime"]["cost_ceiling_micro"] = ex.COST_CEILING_MICRO
    ex.mutate(did, payload, ceiling)
    gaps = []
    messages, schema = plan.messages_for(snapshot)
    try:
        selected = synthesize(did, payload, purpose="research.prior_art.plan", key="prior-art-plan", messages=messages, schema=schema,
            parse=plan.parse_plan, max_tokens=1200, repair=False, cap_at_reservation=True)
    except (FatalError, RetryableError) as exc:
        if str(exc) in FATAL or str(exc) == "research_settlement_pending":
            raise
        selected = plan.fallback(snapshot)
        gaps.append(gap("system_adapter", "Category planning was unavailable; short founder job phrases were used instead."))
    def save(row, doc, session):
        doc["_runtime"].update(prior_art_plan=selected, prior_art_gaps=gaps)
        doc["_runtime"]["prior_art_gap_items"] = record_gaps({"coverage_gaps": gaps})["gap_items"]
    ex.mutate(did, payload, save)
    return selected, gaps


def discover(did: str, payload: dict, selected: dict) -> list[str]:
    from app.research.deep import _retrieve, _store_sources, safe_source_url
    gaps = []
    category = selected["category_terms"][0]
    job = (selected.get("job_phrases") or [category])[0]
    lookups = [("hn-launches", lambda: adapters.search_hn(category, max_calls=1)),
               ("hn-questions", lambda: adapters.search_hn(job, tags="ask_hn", max_calls=1)),
               ("yc", lambda: adapters.search_yc(selected["category_terms"] + selected["job_phrases"], max_calls=1)),
               ("github-repos", lambda: adapters.search_github(category, payload["workspace_id"], max_calls=4))]
    stories = []
    child_calls = 0
    def lookup(key, callback):
        result = _retrieve(did, "prior-" + key, payload, purpose="research.prior_art." + key,
                         provider=key.split("-", 1)[0], estimate=0, callback=lambda: asyncio.run(callback()))
        return result
    for key, callback in lookups:
        try:
            result = lookup(key, callback)
        except FatalError as exc:
            if str(exc) not in BUDGET:
                raise
            gaps.append(gap("system_request_limit", "The research request limit was reached; retained sources remain available."))
            break
        child_calls += result.get("calls", 0)
        gaps.extend(saved_gaps(result, "gaps"))
        admitted = []
        for item in result.get("items", []):
            if safe_source_url(str(item.get("url") or "")) and relevant_company_lead(selected, item):
                meta = {**item.get("prior_art", {}), "source_kind": item.get("source_kind")}
                admitted.append({**item, "prior_art": meta})
                if key == "hn-launches" and meta.get("company_url") and meta.get("hn_id"):
                    stories.append(meta)
        _store_sources(did, payload, {**result, "items": admitted,
            "observed_at": utcnow().replace(tzinfo=timezone.utc).isoformat()}, category, "prior_art")
    for index, story in enumerate(stories[:2]):
        try:
            result = lookup("hn-comments-" + str(index), lambda: adapters.hn_comments(story["hn_id"],
                parent_company_url=story["company_url"], parent_company_name=story.get("company_name"), max_calls=3))
        except FatalError as exc:
            if str(exc) not in BUDGET:
                raise
            gaps.append(gap("system_request_limit", "The research request limit was reached before every discussion could be read."))
            break
        child_calls += result.get("calls", 0)
        gaps.extend(saved_gaps(result, "gaps"))
        # Comments must explicitly concern the planned job; praise and unrelated
        # reactions are kept out of the complaint/demand evidence pool.
        items = [{**item, "prior_art": {**item.get("prior_art", {}), "source_kind": item.get("source_kind")}}
                 for item in result.get("items", []) if relevant_company_lead(selected, item)]
        _store_sources(did, payload, {**result, "items": items,
            "observed_at": utcnow().replace(tzinfo=timezone.utc).isoformat()}, job, "prior_art")
    assert child_calls <= 13  # HN eight total, GitHub four, YC one.
    return list(dict.fromkeys(gaps))


def roundup_names(text: str, *, source_url: str = "") -> list[dict]:
    """Verbatim linked names are query hints, never evidence or company records."""
    from app.research.deep import safe_source_url, _NO_SCRAPE_HOSTS
    source_key = entity_key(source_url) if source_url else None
    rows = []
    for match in re.finditer(r"\[([^\]\n]{1,80})\]\((https?://[^)\s]+)\)", text[:5900]):
        name, url = match.groups()
        safe = safe_source_url(url)
        key = entity_key(safe) if safe else None
        host = (urlsplit(safe).hostname or "") if safe else ""
        prohibited = any(host == base or host.endswith("." + base) for base in _NO_SCRAPE_HOSTS)
        if key and key != source_key and key not in {entity_key(row["url"]) for row in rows} and not prohibited and name.strip() and not re.search(r"@|(?:\+?\d[\s().-]*){8,}", name):
            rows.append({"name": name.strip(), "url": safe})
        if len(rows) == 8:
            break
    return rows


def name_followups(did: str, payload: dict, selected: dict, name_sources: list[dict], provider, scraper, topic: str) -> list[str]:
    """Linked URLs and recalled domains are hints until their real pages qualify."""
    from app.research.deep import _scrape, _store_sources, _store_page, safe_source_url, _NO_SCRAPE_HOSTS
    if scraper is None:
        return []
    names, gaps, seen = [], [], set()
    for seed in selected.get("seed_competitors", [])[:3]:
        domain = str(seed.get("domain") or "").strip()
        url = safe_source_url(domain if "://" in domain else "https://" + domain) if domain else None
        host = (urlsplit(url).hostname or "") if url else ""
        if url and entity_key(url) and not any(host == base or host.endswith("." + base) for base in _NO_SCRAPE_HOSTS):
            names.append({"url": url})
    for source in name_sources:
        if source["url"] in seen or len(seen) >= 2:
            continue
        seen.add(source["url"])
        try:
            identifier = "names_" + hashlib.sha256(source["url"].encode()).hexdigest()[:32]
            fetched = _scrape(did, payload, scraper, {"id": identifier, "url": source["url"]})
        except (FatalError, RetryableError) as exc:
            if str(exc) in FATAL:
                raise
            gaps.append(gap("system_adapter", "Some lists of related products could not be read; their names were not used as evidence."))
            continue
        names.extend(roundup_names(fetched.get("text", ""), source_url=source["url"]))
    # Freeze linked candidates before the first read; saved scrape receipts
    # reconstruct exactly the same list after a worker replay.
    candidates = {}
    for candidate in names:
        candidates.setdefault(entity_key(candidate["url"]), candidate)
    verified = set()
    for candidate in list(candidates.values())[:8]:
        if len(verified) >= 3:
            break
        try:
            identifier = "linked_" + hashlib.sha256(candidate["url"].encode()).hexdigest()[:32]
            fetched = _scrape(did, payload, scraper, {"id": identifier, "url": candidate["url"]})
        except (FatalError, RetryableError) as exc:
            if str(exc) in BUDGET:
                gaps.append(gap("system_request_limit", "Your research reached its limit before every linked page could be checked. Open Research and retry the remaining public product URLs."))
                break
            raise
        if not fetched.get("text"):
            gaps.append(gap("system_adapter", "Your linked product page could not be read. Open Research and add another public product URL, then retry."))
            continue
        url = safe_source_url(str(fetched.get("url") or candidate["url"]))
        item = {"url": url, "title": fetched.get("title") or url, "snippet": fetched.get("text", ""),
                "provider": fetched["provider"]}
        key = entity_key(url or "")
        if not key or key in verified or not relevant_company_lead(selected, item):
            continue
        item["snippet"] = item["snippet"][:2000]
        item["prior_art"] = {"company_url": url, "source_kind": "web"}
        _store_sources(did, payload, {"items": [item], "observed_at": fetched["observed_at"]}, url, "prior_art")
        identifier = "rsf_" + hashlib.sha256((did + url).encode()).hexdigest()[:32]
        _store_page(did, payload, identifier, fetched)
        verified.add(key)
    return gaps


def enrich(did: str, payload: dict, selected: dict, provider) -> list[str]:
    """Budgeted homepage/pricing observations. Failed fetches never imply death."""
    from app.research.deep import sources_for, _scrape, _store_sources, _store_page
    sources = sources_for(did)
    companies = compile_prior_art(sources, None, plan=selected, include_unconfirmed=True)["companies"]
    gaps = []
    if provider is None or not callable(getattr(provider, "scrape", None)):
        return [gap("system_verification", "Full company pages could not be checked; current prices and availability may be unknown.")]
    frozen = []
    def save(row, doc, session):
        frozen.extend(doc["_runtime"].setdefault("prior_company_targets", companies[:2]))
    ex.mutate(did, payload, save)
    for company in frozen:
        if company["kind"] == "oss":
            continue
        website = company["website"]
        parsed = urlsplit(website)
        root = f"{parsed.scheme}://{parsed.netloc}"
        # A fixed common /pricing path is a lookup candidate, never a pricing claim.
        for suffix in ("/", "/pricing"):
            url = root + suffix
            try:
                result = _scrape(did, payload, provider, {"id": "prior_" + hashlib.sha256(url.encode()).hexdigest()[:32], "url": url})
            except (FatalError, RetryableError) as exc:
                if str(exc) in FATAL:
                    raise
                gaps.append(gap("system_verification", "Some company pages could not be checked; no shutdown or price was inferred from a failed fetch."))
                continue
            if not result.get("text"):
                continue
            # Confirm the description on the homepage; a pricing page belongs
            # only to an already confirmed company from the same exact host.
            if suffix == "/" and not relevant_company_lead(selected, {"title": company["name"], "url": url, "snippet": result["text"]}):
                continue
            item = {"url": url, "title": company["name"], "snippet": result["text"][:2000], "provider": provider.name,
                    "prior_art": {"company_name": company["name"], "company_url": website, "source_kind": "web"}}
            _store_sources(did, payload, {"items": [item], "observed_at": result["observed_at"]}, url, "prior_art")
            sid = "rsf_" + hashlib.sha256((did + url).encode()).hexdigest()[:32]
            _store_page(did, payload, sid, result)
    return list(dict.fromkeys(gaps))


def finish(did: str, payload: dict, review: dict | None, selected: dict, gaps: list[str]) -> dict:
    from app.research.deep import sources_for
    runtime = json.loads(ex.read(did).summary)["_runtime"]
    operations = list(runtime["operations"].values())
    usage = [op.get("usage", {}) for op in operations]
    complete = all(op.get("state") in {"completed", "failed", "release_pending", "reserving"}
                   for op in operations) and all("actual_micro" in u for u in usage if u)
    cost = {"usd_estimate": sum(u.get("estimate_micro", 0) for u in usage) / 1_000_000,
            "usd_recorded": sum(u.get("provider_actual_micro", u.get("actual_micro", 0)) for u in usage) / 1_000_000 if complete else None,
            "measurement_complete": complete}
    result = compile_prior_art(sources_for(did), review, plan=selected, gaps=gaps, cost=cost)
    if not result["companies"]:
        result["coverage_gaps"].append(gap("competitor_offering", "No prior attempt was confirmed in this bounded search; this does not establish an empty market."))
    if not any(c["status"] == "inactive" for c in result["companies"]):
        result["coverage_gaps"].append(gap("competitor_offering", "No inactive attempt was confirmed; failure causes remain unknown."))
    record_gaps(result)
    def save(row, doc, session):
        doc["prior_art"] = result
    ex.mutate(did, payload, save)
    return result
