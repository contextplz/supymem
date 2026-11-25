"""Bounded, source-backed deep research after explicit founder confirmation.

Only real returned sources are stored. Search snippets and GitHub metadata are
observations, not validated demand, pricing guarantees, or license clearance.
Every paid call has a durable receipt; plan compilation remains founder-driven.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from app.core.jobs import FatalError, RetryableError
from app.core.local_mode import is_local
from app.db.supymem_models import ResearchDossier, ResearchFinding
from app.research import execution as ex
from app.research.source_quality import plausible_review_lead, relevant_search_lead, research_topic
from app.research.metered import perform, synthesize
from app.research.presentation import NEXT_STEPS, REVIEW_CONTEXT_NEXT_STEP, clean_excerpt, gap, record_gaps, saved_gaps
from app.state import db
from core.clock import utcnow

MAX_PAGE_SCRAPES = 12
SAMPLE_RESEARCH_NOTE = "You are viewing sample research, not market evidence. Open Settings > Keys and connections > Your AI provider, save your model, then create a real venture for your own research."
PAGE_MAX_CHARS = 5900  # Below review.MAX_EXCERPT_LENGTH so a page is one valid excerpt.
CALL_BUDGET = 19 + MAX_PAGE_SCRAPES  # Ten discovery queries, two product follow-ups, GitHub, review, two automatic repairs, one zero-source fallback, one rejection, one boundary retry, page scrapes.
_SCRAPE_INTENTS = {"direct", "alternatives", "category", "job_tool", "services", "product_details", "pricing", "buyer_budget",
                   "customer_request", "workaround"}
_SCRAPE_HINT = re.compile(r"pric|plan|competitor|alternative|customer|workflow|feature|how it works", re.I)
_NO_SCRAPE_HOSTS = {"reddit.com", "linkedin.com", "g2.com", "capterra.com", "news.ycombinator.com"}
PHASES = {"all", "competitors", "services", "oss", "review", "pricing", "demand", "prior_art"}
log = logging.getLogger(__name__)
REVIEW_LEADS_PER_SEARCH = 2
REVIEW_LEADS_PER_DOSSIER = 10


# Grammar and generic framing are query scaffolding, not the job to investigate.
# No market/category/vendor-specific words belong in this list.
_QUERY_FILLER = set("a an the and or but of to for from in on at by with into that their they them this these those who which is are was were be been being have has had do does did no not cannot can will would could should before after about as it its just very little much more most way need needs want wants quick reliable actually getting ready time".split())


def _problem_query(value: object, customer_prefix: str) -> str:
    text = " ".join(str(value or "")[:8192].split())
    if len(text) <= 90:
        return text.rstrip(".,;:-")
    # Keep the actual job beyond introductory persona prose. Only the bounded
    # customer prefix is framing: the full customer can itself name the job.
    framing = set(re.findall(r"[^\W_]+", customer_prefix.lower()))
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", text)
    selected, seen = [], set()
    for word in words:
        key = word.lower()
        negative = key in {"not", "no", "cannot", "never", "without"}
        if not negative and (key in _QUERY_FILLER or set(re.findall(r"[^\W_]+", key)) <= framing or key in seen):
            continue
        seen.add(key)
        selected.append(word)
    if not selected:
        selected = words
    # Bounded input/output, and retain both ends when the problem is very long.
    # This is deterministic search preparation, never semantic validation.
    if len(" ".join(selected)) <= 240:
        return " ".join(selected) or text[:240]

    def edge_words(words: list[str], budget: int, *, reverse: bool = False) -> str:
        result: list[str] = []
        for word in reversed(words) if reverse else words:
            if len(" ".join([*result, word])) > budget:
                break
            result.append(word)
        if reverse:
            result.reverse()
        return " ".join(result)

    middle = len(selected) // 2
    head = edge_words(selected[:min(18, middle)], 120)
    tail = edge_words(selected[max(middle, len(selected) - 12):], 119, reverse=True)
    # Each end owns a separate character budget. Long introductory words cannot
    # consume the tail's budget and silently remove its problem terms.
    return " ".join(part for part in (head, tail) if part) or text[:240]



def queries_for(snapshot: dict, phase: str) -> list[tuple[str, str]]:
    # These are search intentions. Results do not automatically inherit the
    # requested competitor/funding category; the review labels inferences.
    def concise(value: object, limit: int) -> str:
        words = " ".join(str(value or "").split()).split()
        selected: list[str] = []
        for word in words:
            if selected and len(" ".join([*selected, word])) > limit:
                break
            selected.append(word)
        return " ".join(selected).rstrip(".,;:-")

    # Search the customer's problem rather than pasting the generated offer.
    # Long offer prose produced poor, mid-sentence queries and weak evidence.
    customer = concise(snapshot.get("customer"), 60)
    problem = _problem_query(snapshot.get("problem"), customer)
    theme = " ".join(
        part
        for part in (
            problem,
            customer,
        )
        if part
    )
    if not theme:
        theme = concise(snapshot.get("summary") or snapshot.get("offer"), 150)
    if not theme:
        site = snapshot.get("existing_site")
        theme = concise(site.get("url"), 150) if isinstance(site, dict) else ""
    if not theme:
        return []
    queries = [
        ("direct", f"{theme} software pricing paid plans features"),
        ("alternatives", f"{theme} alternatives existing workflow"),
        ("funded", f"{theme} startup funding"),
        ("accelerator", f"{theme} accelerator startup"),
        ("emerging", f"{theme} new product launch"),
        ("community", f'{theme} customer reviews 1-star 2-star 3-star complaints then 4-star "love it but"'),
        ("services", f"{theme} API service pricing free tier limits"),
    ]
    # Competitor discovery: vendors describe the category and the customer, not the founder's problem wording.
    if customer:
        queries += [("category", f"{problem or theme} software for {customer}"),
                    ("job_tool", f"{problem or theme} tool for {customer}")]
    voice = [("customer_voice", f"site:reddit.com OR site:news.ycombinator.com {theme} buying intent willingness to pay")]
    if phase == "pricing":
        return [
            ("pricing", f"{theme} competing product pricing paid plans"),
            ("buyer_budget", f"{theme} buyer budget procurement paid pilot"),
        ] + voice
    if phase == "demand":
        demand = [
            ("customer_request", f"{theme} customer request need problem forum"),
            ("workaround", f"{theme} manual workaround frustration interview"),
        ]
        risk = concise((snapshot.get("gstack_brief") or {}).get("biggest_risk"), 100)
        return demand + ([("gstack_risk", f"{theme} {risk}")] if risk else []) + voice
    return [(key, query) for key, query in queries if phase == "all" or (phase == "services" and key == "services") or (phase == "competitors" and key != "services")] + (voice if phase not in {"oss", "review"} else [])



def product_followups(sources: list[dict], *, topic: str) -> list[str]:
    """Bounded searches for product details discovered in actual sources.

    Excerpts remain untrusted; only a validated hostname enters the new query.
    This is follow-up coverage, not a claim that the domain or prices are verified.
    """
    domains = []
    community = {"reddit.com", "facebook.com", "quora.com", "medium.com", "youtube.com", "linkedin.com", "github.com"}
    for source in sources:
        evidence = source.get("evidence") or {}
        if evidence.get("founder_flag") == "exclude":
            continue
        # Older dossiers can contain leads admitted before the current topic
        # gate. A rerun must not use those domains to seed fresh searches.
        if not _saved_source_relevant(topic, source):
            continue
        if not set(evidence.get("search_intents") or []) & {"direct", "alternatives", "category", "job_tool", "services"}:
            continue
        text = str(source.get("title") or "") + " " + str(source.get("summary") or "")
        if not re.search(r"\b(?:software|tools?|platform|app|saas)\b", text, re.I):
            continue
        # An explicit price excerpt already gives synthesis a pricing candidate.
        if re.search(r"[$€£₹]\s*\d|\b(?:USD|EUR|GBP|INR)\s*\d", text):
            continue
        url = safe_source_url(str(source.get("url") or ""))
        host = (urlsplit(url).hostname or "").lower() if url else ""
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
            continue
        if any(host == blocked or host.endswith("." + blocked) for blocked in community):
            continue
        if host not in domains:
            domains.append(host)
        if len(domains) == 2:
            break
    return [f"site:{host} software product features pricing plans" for host in domains]


def _saved_source_relevant(topic: str, source: dict) -> bool:
    evidence = source.get("evidence") or {}
    return relevant_search_lead(topic, {"url": source.get("url"),
                                        "title": source.get("title"),
                                        "snippet": source.get("summary") or "",
                                        "repository": evidence.get("repository")})


def saved_followups(did: str, payload: dict, *, topic: str) -> list[str]:
    # Freeze before the first search; newly retrieved sources must not change
    # operation keys or trigger duplicate work after a worker reconnect.
    proposed = product_followups(sources_for(did), topic=topic)
    saved = []
    def update(row, document, session):
        runtime = document["_runtime"]
        saved.extend(runtime.setdefault("product_followups", proposed))
    ex.mutate(did, payload, update)
    return saved


def safe_source_url(raw: str) -> str | None:
    """Validate a display-only source URL. This function never fetches it."""
    try:
        u = urlsplit(raw)
        host = (u.hostname or "").lower().rstrip(".")
        if u.scheme not in {"https", "http"} or not host or u.username or u.password or len(raw) > 2048:
            return None
        if host in {"localhost", "metadata.google.internal", "instance-data"} or host.endswith((".localhost", ".internal", ".local")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        if u.port and u.port not in {80, 443}:
            return None
        query = [(k, v) for k, v in parse_qsl(u.query) if k.lower() not in {"token", "access_token", "api_key", "key", "code", "state", "email"} and not k.lower().startswith("utm_")]
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), ""))
    except (TypeError, ValueError):
        return None


def normalize_with_diagnostics(items: list, provider: str, *, topic: str) -> tuple[list[dict], dict[str, int]]:
    if not topic.strip():
        raise FatalError("research_topic_missing")
    result = []
    rejected = {"invalid_shape": 0, "unsafe_url": 0, "off_topic": 0, "over_limit": 0}
    for item in items:
        if not isinstance(item, dict):
            rejected["invalid_shape"] += 1
            continue
        url = safe_source_url(str(item.get("url") or ""))
        if not url:
            rejected["unsafe_url"] += 1
            continue
        if not relevant_search_lead(topic, {**item, "url": url}):
            rejected["off_topic"] += 1
            continue
        if len(result) == 6:
            rejected["over_limit"] += 1
            continue
        row = {"url": url, "title": str(item.get("title") or url)[:240],
               "snippet": str(item.get("snippet") or "")[:2000], "provider": provider}
        if isinstance(item.get("fetched_at"), str):
            row["fetched_at"] = item["fetched_at"]
        if provider == "github" and isinstance(item.get("repository"), dict):
            row["repository"] = item["repository"]
        result.append(row)
    return result, rejected


def normalize(items: list, provider: str, *, topic: str) -> list[dict]:
    return normalize_with_diagnostics(items, provider, topic=topic)[0]


def review_leads(items: list, provider: str, *, topic: str) -> list[dict]:
    """Bounded URLs for founder inspection, never model or market evidence."""
    leads: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = safe_source_url(str(item.get("url") or ""))
        if not url or url in seen:
            continue
        candidate = {**item, "url": url}
        if relevant_search_lead(topic, candidate) or not plausible_review_lead(topic, candidate):
            continue
        seen.add(url)
        leads.append({"url": url, "title": str(item.get("title") or url)[:240], "provider": provider})
        if len(leads) >= REVIEW_LEADS_PER_SEARCH:
            break
    return leads


def _store_sources(did: str, payload: dict, result: dict, query: str, intent: str):
    def update(row, document, session):
        for item in result.get("items", []):
            host = (urlsplit(str(item.get("url") or "")).hostname or "").lower()
            if host == "loot-drop.io" or host.endswith(".loot-drop.io"):
                continue
            metadata = item.get("prior_art") or {}
            if metadata.get("source_kind") in {"hn_item", "hn_comment", "yc_company", "repository_metadata", "github_issue"}:
                fields = {key: metadata[key] for key in ("status", "points", "comments", "stars", "reactions", "batch")
                          if isinstance(metadata.get(key), (str, int)) and not isinstance(metadata.get(key), bool)}
                day = _activity_date(metadata.get("date"))
                if day:
                    fields["source_date"] = day.date().isoformat()
                if fields:
                    item = {**item, "snippet": item["snippet"] + "\nObserved API fields: " + json.dumps(fields, ensure_ascii=False, sort_keys=True)}
            fid = "rsf_" + hashlib.sha256((did + item["url"]).encode()).hexdigest()[:32]
            finding = session.get(ResearchFinding, fid, with_for_update=True)
            if finding:
                evidence = json.loads(finding.evidence)
                if item.get("prior_art") and not evidence.get("prior_art"):
                    evidence["prior_art"] = item["prior_art"]
                if evidence.get("evidence_kind") == "search_lead_unverified":
                    # A later, stronger excerpt may qualify the same URL.
                    evidence["evidence_kind"] = "search_excerpt"
                    finding.category = "source"
                    finding.summary = item["snippet"]
                if query not in evidence.setdefault("search_queries", []):
                    evidence["search_queries"].append(query)
                if intent not in evidence.setdefault("search_intents", []):
                    evidence["search_intents"].append(intent)
                additions = evidence.setdefault("additional_excerpts", [])
                snippet = item["snippet"]
                if snippet and snippet != finding.summary and not any(entry["text"] == snippet for entry in additions) and len(additions) < 5:
                    additions.append({"text": snippet, "query": query, "observed_at": result["observed_at"], "provider": item["provider"]})
                finding.evidence = json.dumps(evidence)
                continue
            repo = item.get("repository", {})
            observed = result["observed_at"]
            evidence = {"observed_at": observed, "search_queries": [query], "search_intents": [intent],
                        "evidence_kind": "sample" if item["provider"] == "Sample" else "repository_metadata" if repo else "search_excerpt",
                        "page_verified": False, "repository": repo or None,
                        "credential_source": result.get("credential_source"),
                        "license_review": "Repository metadata only; dependency licenses and compatibility require review." if repo else None}
            if item.get("prior_art"):
                evidence["prior_art"] = item["prior_art"]
                evidence["evidence_kind"] = item["prior_art"].get("source_kind") or evidence["evidence_kind"]
            session.add(ResearchFinding(id=fid, dossier_id=did, workspace_id=row.workspace_id,
                venture_id=row.venture_id, category="oss_repo" if repo else "source", title=item["title"],
                url=item["url"], source=item["provider"], summary=item["snippet"], evidence=json.dumps(evidence),
                license=repo.get("license_spdx"), license_ok=None, stars=repo.get("stars"),
                reuse_recommendation="review" if repo else None, created_at=utcnow(),
                last_activity_at=_activity_date(item.get("prior_art", {}).get("date") or repo.get("pushed_at"))))
    ex.mutate(did, payload, update)


def sample_research(did: str, payload: dict, topic: str):
    from app.signals.research import LocalStubProvider

    items = asyncio.run(LocalStubProvider().search(topic))
    _store_sources(did, payload, {"items": items, "observed_at": utcnow().isoformat()}, topic, "Sample")
    return gap("system_scope", SAMPLE_RESEARCH_NOTE)


def _store_review_leads(did: str, payload: dict, result: dict, intent: str) -> None:
    """Persist a small separate lead pool without creating evidence claims."""
    candidates = result.get("review_leads") or []
    if not candidates:
        return

    def update(row, document, session):
        existing = session.scalars(select(ResearchFinding).where(
            ResearchFinding.dossier_id == did, ResearchFinding.category == "review_lead"
        )).all()
        total = len(existing)
        for item in candidates:
            if total >= REVIEW_LEADS_PER_DOSSIER:
                break
            fid = "rsf_" + hashlib.sha256((did + item["url"]).encode()).hexdigest()[:32]
            if session.get(ResearchFinding, fid):
                continue
            evidence = {"observed_at": result["observed_at"], "search_intents": [intent],
                        "evidence_kind": "search_lead_unverified", "page_verified": False,
                        "qualification": "Failed the idea-specific evidence gate; founder review only."}
            session.add(ResearchFinding(id=fid, dossier_id=did, workspace_id=row.workspace_id,
                venture_id=row.venture_id, category="review_lead", title=item["title"], url=item["url"],
                source=item["provider"], summary="Unverified search result; open and assess its relevance.",
                evidence=json.dumps(evidence), license=None, license_ok=None, stars=None,
                reuse_recommendation=None, created_at=utcnow()))
            total += 1
    ex.mutate(did, payload, update)


def inherit_feedback(did: str, payload: dict):
    previous_id = payload.get("previous_dossier_id")
    if not previous_id:
        return
    def update(row, doc, session):
        if doc["_runtime"].get("feedback_copied"):
            return
        previous = session.get(ResearchDossier, previous_id, with_for_update=True)
        if not previous or (previous.workspace_id, previous.venture_id, previous.revision_id) != (row.workspace_id, row.venture_id, row.revision_id):
            raise FatalError("research_scope_mismatch")
        review_only = payload.get("phase") == "review"
        if review_only:
            from app.research.service import review_inputs
            _, _, digest = review_inputs(previous.id)
            if digest != payload.get("source_digest"):
                raise FatalError("research_inputs_changed")
        old = session.scalars(select(ResearchFinding).where(ResearchFinding.dossier_id == previous_id)).all()
        old_runtime = json.loads(previous.summary).get("_runtime", {})
        old_plan = old_runtime.get("prior_art_plan") or old_runtime.get("prior_art_review_plan")
        if review_only and old_plan:
            doc["_runtime"]["prior_art_review_plan"] = old_plan
        topic = research_topic(doc["_runtime"]["snapshot"])
        for finding in old:
            evidence = json.loads(finding.evidence)
            if (not review_only and evidence.get("founder_flag") not in {"pin", "exclude"}) or not finding.url or not evidence.get("observed_at") or evidence.get("evidence_kind") not in {"search_excerpt", "repository_metadata", "hn_item", "hn_comment", "yc_company", "github_issue", "web"}:
                continue
            # An old dossier may predate the current relevance gate. Preserve
            # explicit founder pins, but do not silently reintroduce old broad
            # search leads into the new model review or product follow-ups.
            if evidence.get("founder_flag") != "pin" and not (review_only and old_plan and evidence.get("prior_art")) and not _saved_source_relevant(topic, {
                "url": finding.url, "title": finding.title, "summary": finding.summary,
                "evidence": evidence,
            }):
                continue
            fid = "rsf_" + hashlib.sha256((did + finding.url).encode()).hexdigest()[:32]
            if session.get(ResearchFinding, fid):
                continue
            evidence["inherited_from_dossier_id"] = previous_id
            session.add(ResearchFinding(id=fid, dossier_id=did, workspace_id=row.workspace_id,
                venture_id=row.venture_id, category=finding.category, title=finding.title, url=finding.url,
                source=finding.source, summary=finding.summary, evidence=json.dumps(evidence), license=finding.license,
                license_ok=None, stars=finding.stars, reuse_recommendation=finding.reuse_recommendation, created_at=utcnow()))
        doc["_runtime"]["feedback_copied"] = True
    ex.mutate(did, payload, update)


def _activity_date(value):
    from datetime import datetime
    from app.research.prior_art.entities import _date
    day = _date(value)
    return datetime.fromisoformat(day) if day else None


def finding_page(finding, evidence: dict) -> tuple[dict, dict]:
    source = {"url": finding.url, "title": finding.title, "summary": finding.summary}
    if not is_local() or not evidence.get("page_verified"):
        return source, evidence
    excerpts = evidence.get("additional_excerpts", [])
    verified = next((entry for entry in excerpts if entry.get("verified_page") is True and entry.get("url")), None)
    if not verified:
        # Older reads could be model output or could have followed an undocumented redirect.
        return source, {**evidence, "page_verified": False,
                        "additional_excerpts": [entry for entry in excerpts if not entry.get("verified_page")]}
    if verified["url"] != finding.url:
        source = {"url": verified["url"], "title": verified.get("title") or urlsplit(verified["url"]).hostname,
                  "summary": verified["text"][:2000], "search_lead": source}
    return source, {**evidence, "additional_excerpts": [
        entry for entry in excerpts if entry.get("url") == source["url"]
        or (source["url"] == finding.url and not entry.get("url") and not entry.get("verified_page"))]}


def sources_for(did: str) -> list[dict]:
    with db()() as session:
        rows = session.scalars(select(ResearchFinding).where(ResearchFinding.dossier_id == did).order_by(ResearchFinding.id))
        sources = []
        for finding in rows:
            evidence = json.loads(finding.evidence)
            if evidence.get("founder_flag") == "exclude" or evidence.get("evidence_kind") not in {
                    "search_excerpt", "repository_metadata", "hn_item", "hn_comment", "yc_company", "github_issue", "web"}:
                continue
            source, evidence = finding_page(finding, evidence)
            sources.append({"id": finding.id, "title": source["title"], "url": source["url"], "summary": source["summary"], "evidence": evidence})
        return sources


def _web_provider():
    if is_local():
        from app.research.local_providers import search_provider
        return search_provider()
    # Reuse the real existing HTTP/DDG adapters, not their heuristic recon report
    # or automatic retry wrapper. Fixed providers never fetch result URLs here.
    import os
    from app.signals.research import DuckDuckGoProvider, HttpResearchProvider, _self_hosted_firecrawl
    for name in ("firecrawl", "exa", "tavily"):
        key = os.getenv(name.upper() + "_API_KEY")
        if key or name == "firecrawl" and _self_hosted_firecrawl():
            return HttpResearchProvider(name, key or "")
    if os.getenv("RESEARCH_KEYLESS", "1").lower() not in {"0", "false", "no"}:
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return None
        return DuckDuckGoProvider()
    return None


def _scrape_provider(search_provider):
    """Local public reads work independently of a configured search upgrade."""
    if is_local():
        from app.research.local_providers import scrape_provider
        return scrape_provider()
    import os
    from app.signals.research import HttpResearchProvider, _self_hosted_firecrawl
    key = os.getenv("FIRECRAWL_API_KEY")
    if key or _self_hosted_firecrawl():
        return HttpResearchProvider("firecrawl", key or "")
    return search_provider if getattr(search_provider, "name", None) == "firecrawl" else None


def _company_provider(search_provider):
    """Exa discovers companies when configured; Firecrawl remains the scraper."""
    if is_local():
        return search_provider
    import os
    from app.signals.research import HttpResearchProvider, _self_hosted_firecrawl
    key = os.getenv("EXA_API_KEY")
    if key and isinstance(search_provider, HttpResearchProvider) and not _self_hosted_firecrawl():
        return HttpResearchProvider("exa", key, category="company")
    return search_provider


def _retrieve(did: str, key: str, payload: dict, **kwargs):
    """Retry rejected searches; skip definite optional-source failures, never uncertainty."""
    source_failures = {"research_provider_rejected",
        "research_invalid_output", "research_credentials_unavailable", "research_boundary_blocked"}
    if kwargs["purpose"] != "research.search":
        source_failures.add("research_provider_rate_limited")
    try:
        try:
            saved = ex.operation(did, key)
        except AttributeError:  # no saved dossier yet (direct provider paths)
            saved = {}
        if saved.get("error_code") in source_failures:
            raise FatalError(saved["error_code"])
        return perform(did, key, payload, **kwargs)
    except (FatalError, RetryableError) as exc:
        if str(exc) not in source_failures:
            raise
        category = ex.operation(did, key).get("failure", {}).get("category")
        reason = {"timeout": "source_timeout", "http": "source_http_error", "connection": "source_connection_error",
                  "boundary": "source_content_error"}.get(category, "source_content_error")
        log.warning("research_source_skipped", extra={"dossier_id": did, "operation_kind": kwargs["purpose"],
                    "reason": reason})
        return {"items": [], "text": "", "provider": kwargs["provider"],
                "observed_at": utcnow().replace(tzinfo=timezone.utc).isoformat(),
                "coverage_gap": gap("system_adapter", "One source could not be read; other sources were retained."),
                "coverage_gap_kind": "system_adapter"}


def _search(did: str, payload: dict, provider, key: str, query: str, topic: str,
            *, allowed_host: str | None = None, prior_plan: dict | None = None):
    from app.credits.policy import web_search_credits
    from app.credits.units import credits_to_micro
    import httpx
    from app.research.errors import SEARCH_KEY_FIELDS, SearchKeyRejected
    from app.research.local_providers import (SEARCH_LIMITED, SEARCH_UNAVAILABLE, BuiltinSearchUnavailable,
        SearchEngineLimited, collect_search_diagnostics, search_was_limited)

    async def fetch():
        coverage_gap = None
        search_outcome = None
        builtin = is_local() and provider.name == "duckduckgo"
        try:
            from ddgs.exceptions import DDGSException
        except ImportError:  # pragma: no cover - provider cannot be selected
            DDGSException = ()  # type: ignore[assignment,misc]
        try:
            from app.experiments import activity
            with (
                (collect_search_diagnostics() if builtin else nullcontext({})) as queries,
                activity.track(payload, action="search", tool=provider.name, evidence_level="desk_research", evidence_summary=query),
            ):
                rows = await asyncio.wait_for(provider.search(query), timeout=None if provider.name == "firecrawl" else 25)
            if search_was_limited(queries):
                coverage_gap = gap("system_request_limit", SEARCH_LIMITED)
            if builtin:
                search_outcome = "results" if rows else "no_matches"
        except httpx.HTTPStatusError as exc:
            if is_local() and provider.name in SEARCH_KEY_FIELDS and exc.response.status_code in {401, 403}:
                raise SearchKeyRejected(provider.name, exc.response.status_code) from None
            raise
        except (SearchEngineLimited, BuiltinSearchUnavailable, TimeoutError) as exc:
            if not builtin:
                raise
            rows = []
            search_outcome = "limited" if isinstance(exc, SearchEngineLimited) else "unavailable"
            coverage_gap = gap("system_request_limit", SEARCH_LIMITED if search_outcome == "limited" else SEARCH_UNAVAILABLE)
        except DDGSException:
            # ddgs fans a keyless query out to public engines. Rate limits or
            # blocks from those engines are a known read-coverage gap, not an
            # ambiguous external side effect. Paid provider failures continue
            # through the metered failure path below.
            if provider.name != "duckduckgo":
                raise
            rows = []
            coverage_gap = gap("system_request_limit", "Keyless web search was rate limited for one research query.")
        parsed_rows = len(rows)
        usage = getattr(rows, "usage", {})
        provider_rows = getattr(rows, "provider_rows", parsed_rows)
        provider_invalid_shape = getattr(rows, "rejected_shape", 0)
        provider_missing_url = getattr(rows, "rejected_missing_url", 0)
        rows = [item for item in rows if not isinstance(item, dict)
                or not ((host := (urlsplit(safe_source_url(str(item.get("url") or "")) or "").hostname or "").lower()) == "loot-drop.io"
                        or host.endswith(".loot-drop.io"))]
        scoped_out = 0
        if allowed_host:
            scoped = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                url = safe_source_url(str(item.get("url") or ""))
                host = (urlsplit(url).hostname or "").lower() if url else ""
                if host == allowed_host or host.endswith("." + allowed_host):
                    scoped.append(item)
            scoped_out = len(rows) - len(scoped)
            rows = scoped
        if prior_plan:
            from app.research.source_quality import relevant_company_lead, name_source
            admitted = [item for item in rows if isinstance(item, dict) and relevant_company_lead(prior_plan, item)]
            normalized = [{"url": safe_source_url(str(item.get("url") or "")), "title": str(item.get("title") or "")[:240],
                           "snippet": str(item.get("snippet") or "")[:2000], "provider": provider.name,
                           "prior_art": {"company_url": safe_source_url(str(item.get("url") or "")), "source_kind": "web"}}
                          for item in admitted[:6] if safe_source_url(str(item.get("url") or ""))]
            rejected = {"invalid_shape": 0, "unsafe_url": 0, "off_topic": len(rows) - len(admitted), "over_limit": max(0, len(admitted) - 6)}
            names = [{"url": safe_source_url(str(item.get("url") or "")), "title": str(item.get("title") or "")[:240],
                      "snippet": str(item.get("snippet") or "")[:2000], "provider": provider.name}
                     for item in rows if isinstance(item, dict) and name_source(item) and safe_source_url(str(item.get("url") or ""))][:2]
        else:
            normalized, rejected = normalize_with_diagnostics(rows, provider.name, topic=topic)
            names = []
        candidates = review_leads(rows, provider.name, topic=topic) if not allowed_host else []
        log.info("research_source_counts", extra={"adapter": provider.name, "intent": key,
            "rows": provider_rows, "admitted": len(normalized), "off_topic": rejected["off_topic"]})
        return {"items": normalized, "review_leads": candidates, "name_sources": names,
                "observed_at": utcnow().replace(tzinfo=timezone.utc).isoformat(),
                "provider": provider.name, "coverage_gap": coverage_gap,
                **({"search_outcome": search_outcome} if search_outcome else {}),
                **({"usage": {"firecrawl_self_hosted": True}} if usage.get("firecrawl_self_hosted") is True else {}),
                "coverage_gap_kind": "system_request_limit" if coverage_gap else None,
                "source_counts": {"provider_rows": provider_rows,
                                  "parsed_rows": parsed_rows, "scoped_out": scoped_out,
                                  "missing_url": provider_missing_url,
                                  "invalid_shape": provider_invalid_shape + rejected["invalid_shape"],
                                  "unsafe_url": rejected["unsafe_url"], "off_topic": rejected["off_topic"],
                                  "over_limit": rejected["over_limit"], "admitted": len(normalized),
                                  "review_leads": len(candidates)}}
    return _retrieve(did, key, payload, purpose="research.search", provider=provider.name,
                   estimate=0 if is_local() and provider.name == "duckduckgo" else credits_to_micro(web_search_credits()), callback=lambda: asyncio.run(fetch()),
                   **({"byok": True} if is_local() else {}))


def scrape_targets(sources: list[dict], limit: int = MAX_PAGE_SCRAPES) -> list[dict]:
    """Prefer top offering pages and domain diversity, then fill remaining slots."""
    ranked = []
    for source in sources:
        evidence = source.get("evidence") or {}
        if evidence.get("evidence_kind") not in {"search_excerpt", "web"} or evidence.get("page_verified"):
            continue
        url = safe_source_url(str(source.get("url") or ""))
        host = (urlsplit(url).hostname or "").lower() if url else ""
        if not host:
            continue
        if any(host == base or host.endswith("." + base) for base in _NO_SCRAPE_HOSTS):
            continue
        score = 2 * bool(set(evidence.get("search_intents") or []) & _SCRAPE_INTENTS)
        score += bool(_SCRAPE_HINT.search(f"{source.get('title')} {url}"))
        ranked.append((-score, source["id"], host, {**source, "url": url}))
    chosen, hosts, remaining = [], set(), []
    for _, _, host, source in sorted(ranked, key=lambda r: r[:2]):
        if host not in hosts and len(chosen) < limit:
            hosts.add(host)
            chosen.append(source)
        else:
            remaining.append(source)
    return [*chosen, *remaining][:limit]


class ScrapeFailed(Exception):
    status = 404  # metered._failure treats this as a definite, unbilled rejection


def _scrape(did: str, payload: dict, provider, source: dict):
    url = safe_source_url(str(source.get("url") or ""))
    host = (urlsplit(url).hostname or "") if url else ""
    if host == "loot-drop.io" or host.endswith(".loot-drop.io"):
        raise FatalError("research_provider_rejected")
    if not url or any(host == base or host.endswith("." + base) for base in _NO_SCRAPE_HOSTS):
        raise FatalError("research_provider_rejected")  # No reservation or I/O for prohibited hosts.
    from app.credits.policy import web_fetch_credits
    from app.credits.units import credits_to_micro
    async def fetch():
        try:
            from app.experiments import activity
            with activity.track(payload, action="fetch", tool=provider.name, evidence_level="desk_research", source_url=url) as receipt:
                text = await provider.scrape(source["url"])
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("research_source_content_invalid")
                receipt["evidence_summary"] = text.strip()[:500]
        except Exception as exc:
            import httpx
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else getattr(exc, "status", None)
            if status in {400, 401, 403, 404, 422}:
                raise ScrapeFailed() from exc  # definite rejection: unbilled, and the source is skipped
            raise  # transport/timeout may follow billed work: keep the protected receipt
        metadata = getattr(text, "metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        source_url = safe_source_url(str(metadata.get("url" if is_local() else "sourceURL") or ""))
        page_text = str(text)
        if page_text.lstrip().startswith("<"):
            from app.signals.local_reader import _Markdown
            from app.tools.reader import _clean
            parsed = _Markdown(source_url or source["url"])
            parsed.feed(page_text)
            page_text = _clean("".join(parsed.main or parsed.text))
        return {"text": page_text.strip()[:PAGE_MAX_CHARS], "provider": provider.name,
                **({"title": metadata["title"][:240]} if isinstance(metadata.get("title"), str) else {}),
                **({"url": source_url, **({"page_url": source_url} if is_local() else {})} if source_url else {}),
                **({"usage": {"firecrawl_self_hosted": True}} if getattr(text, "usage", {}).get("firecrawl_self_hosted") is True else {}),
                "observed_at": utcnow().replace(tzinfo=timezone.utc).isoformat()}
    return _retrieve(did, "scrape_" + source["id"][-12:], payload, purpose="research.fetch", provider=provider.name,
                   estimate=0 if is_local() and provider.name == "duckduckgo" else credits_to_micro(web_fetch_credits()), callback=lambda: asyncio.run(fetch()),
                   **({"byok": True} if is_local() else {}))


def _store_page(did: str, payload: dict, source_id: str, result: dict) -> None:
    def update(row, document, session):
        finding = session.get(ResearchFinding, source_id, with_for_update=True)
        if not finding:
            return
        final_url = result.get("page_url") if is_local() else result.get("url")
        if is_local() and (not final_url or urlsplit(final_url).hostname != urlsplit(finding.url).hostname):
            return
        evidence = json.loads(finding.evidence)
        entry = {"text": result["text"], "query": "page_scrape", "observed_at": result["observed_at"],
                 "provider": result["provider"], "verified_page": True,
                 **({"url": final_url, "title": result.get("title")} if final_url else {})}
        evidence["additional_excerpts"] = [entry, *evidence.get("additional_excerpts", [])][:5]
        evidence["page_verified"] = True
        finding.evidence = json.dumps(evidence)
    ex.mutate(did, payload, update)


def verify_top_sources(did: str, payload: dict, provider, *, limit: int = MAX_PAGE_SCRAPES) -> None:
    """Verify ranked leads in four-call batches, draining every admitted receipt."""
    if not callable(getattr(provider, "scrape", None)):
        return
    sources = sources_for(did)
    targets = scrape_targets(sources[:50], limit=limit)
    frozen = []
    def save(row, doc, session):
        frozen.extend(doc["_runtime"].setdefault("scrape_targets", [source["id"] for source in targets]))
    ex.mutate(did, payload, save)
    by_id = {source["id"]: source for source in sources}
    targets = [by_id[sid] for sid in frozen if sid in by_id][:limit]
    deadline = time.monotonic() + 90
    with ThreadPoolExecutor(max_workers=4) as pool:
        for offset in range(0, len(targets), 4):
            if ex.cancel_requested(payload):
                raise FatalError("research_cancelled")
            if deadline - time.monotonic() < 25:
                break
            row = ex.read(did)
            runtime = json.loads(row.summary)["_runtime"]
            slots = row.budget_calls - row.calls_used - 1
            if runtime.get("cost_ceiling_micro"):
                slots = min(slots, 59 - runtime.get("raw_calls_reserved", 0))
            if slots <= 0:
                break
            batch = [(source, pool.submit(_scrape, did, payload, provider, source))
                     for source in targets[offset:offset + min(4, slots)]]
            exhausted, fatal = False, None
            for source, future in batch:
                try:
                    result = future.result()
                except (FatalError, RetryableError) as exc:
                    if str(exc) in {"research_operation_in_progress", "research_lease_lost", "research_scope_mismatch", "research_outcome_uncertain", "research_cancelled"}:
                        fatal = exc
                    exhausted |= str(exc) in {"research_cost_budget_exhausted", "research_call_budget_exhausted", "research_credits_unavailable"}
                    continue
                if result.get("text"):
                    _store_page(did, payload, source["id"], result)
            if fatal:
                raise fatal
            if exhausted:
                break


_NONPUBLIC_QUALIFIER = re.compile(r"-?\b(?:is|visibility):\s*\S*", re.IGNORECASE)
# Lease/scope/in-flight failures must stop the run; anything else from the optional GitHub lookup is a gap.
_RUN_FATAL = {"research_operation_in_progress", "research_lease_lost", "research_scope_mismatch"}


def github_query(snapshot: dict, topic: str) -> str:
    """One-line public search text. Founder prose may hold newlines or be empty after vague
    answers ("not sure", "skip"); unnormalized, GitHub's query guard raised before any request
    and the free lookup was journaled as an uncertain outcome that failed the whole run
    (prod job_sq2fn9dyw7dt: research.github, category internal, 0 ms)."""
    text = " ".join(str(snapshot.get(k) or "") for k in ("offer", "customer")).strip() or str(topic or "")
    text = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(_NONPUBLIC_QUALIFIER.sub(" ", text).split())[:180].strip()


def _github(did: str, payload: dict, query: str, topic: str):
    from app.research.github_search import search_public_repositories
    async def fetch():
        result = await asyncio.wait_for(search_public_repositories(query, payload["workspace_id"], limit=5), timeout=25)
        normalized, rejected = normalize_with_diagnostics(result["items"], "github", topic=topic)
        return {**result, "items": normalized,
                "source_counts": {"provider_rows": len(result["items"]), "parsed_rows": len(result["items"]),
                                  "scoped_out": 0, "missing_url": 0, **rejected, "admitted": len(normalized)},
                "observed_at": utcnow().replace(tzinfo=timezone.utc).isoformat()}
    return _retrieve(did, "github", payload, purpose="research.github", provider="github", estimate=0,
                   callback=lambda: asyncio.run(fetch()))


def _finish(did, payload, review, gaps):
    def update(row, doc, session):
        if review:
            record_gaps(review)
        doc.update(review=review, coverage_gaps=list(dict.fromkeys(gaps)), review_kind="model_assessment" if review else "retrieved_sources_only")
        record_gaps(doc)
        row.state = "partial" if gaps else "complete"
        row.completed_at = utcnow()
        system_kinds = {"system_adapter", "system_assessment", "system_license_metadata", "system_model_context", "system_request_limit",
                        "system_scope", "system_stopped", "system_unsent_material", "system_verification"}
        return sorted({item["kind"] if item["kind"] in NEXT_STEPS or item["kind"] in system_kinds else "system_scope"
                       for item in doc["gap_items"]})
    gap_kinds = ex.mutate(did, payload, update)
    if gap_kinds:
        log.info("research_partial dossier_id=%s gap_kinds=%s", did, ",".join(gap_kinds))


def _boundary_safe_sources(sources: list[dict], workspace_id: str, *, text_budget: int = 9000) -> list[dict]:
    """Drop blocked sources before trimming; the gateway still screens the prompt."""
    from app.boundary import Segment
    from app.boundary.classify import classify
    kept = []
    blocked = 0
    for source in sources:
        evidence = dict(source.get("evidence") or {})
        metadata = [source.get("title") or "", source.get("url") or "",
                    json.dumps({key: evidence.get(key) for key in ("prior_art", "repository")}, ensure_ascii=False)]
        additions = sorted(evidence.get("additional_excerpts") or [], key=lambda entry: not entry.get("verified_page"))
        entries = [{"text": source.get("summary") or "", "summary": True}, *additions]
        if evidence.get("snippet"):
            entries.append({"text": evidence["snippet"]})
        if not classify([Segment(text=text, origin="third_party") for text in
                         [*metadata, *[entry["text"] for entry in entries]]], workspace_id=workspace_id).sendable:
            blocked += 1
            continue
        remaining, summary, selected = text_budget, "", []
        for entry in entries:
            text = entry["text"]
            text = clean_excerpt(text)[:min(2000 if entry.get("summary") else PAGE_MAX_CHARS, remaining)]
            while text and len(json.dumps(text, ensure_ascii=False)) > remaining:
                text = text[:max(0, len(text) - (len(json.dumps(text, ensure_ascii=False)) - remaining))]
            if not text:
                continue
            remaining -= len(json.dumps(text, ensure_ascii=False))
            if entry.get("summary"):
                summary = text
            elif len(selected) < 5:
                selected.append({**entry, "text": text})
        if summary or selected:
            evidence.pop("snippet", None)
            evidence["additional_excerpts"] = selected
            evidence["page_verified"] = any(entry.get("verified_page") for entry in selected)
            kept.append({**source, "summary": summary, "evidence": evidence})
    if blocked:
        log.warning("research_source_material_blocked", extra={"blocked_passages": blocked})
    return kept


def _review_sources(did, payload, snapshot, sources, *, key="review", repair=True):
    from app.research.selection import messages_for, parse_selection, response_schema, review_prompt
    from app.research.citations import project, restore
    from llm_gateway.local_transport import loopback_input_budget

    provider_sources, _ = project(sources)
    row = ex.read(did)
    runtime = json.loads(row.summary)["_runtime"]
    selected_plan = runtime.get("prior_art_plan") or runtime.get("prior_art_review_plan")
    review_snapshot = {**snapshot, **({"prior_art_query_plan": selected_plan} if selected_plan else {})}
    from app.research.review import MAX_INPUT_LENGTH
    overhead = len(json.dumps({"snapshot": review_snapshot, "sources": [
        {"id": source["id"], "page_kind": "editorial_or_unknown", "excerpts": [],
         "structured_observations": {key: (source.get("evidence") or {}).get(key) for key in ("prior_art", "repository")}}
        for source in provider_sources]}, ensure_ascii=False, allow_nan=False))
    # ponytail: 9k text per source and equal prompt shares; relevance-weighted allocation if coverage suffers.
    budget = min(9000, max(0, (MAX_INPUT_LENGTH - overhead) // max(1, len(sources)) - 200))
    bounded_sources = _boundary_safe_sources(sources, row.workspace_id, text_budget=budget)
    all_sources_safe = len(bounded_sources) == len(sources)
    if all_sources_safe:
        sources = bounded_sources
    if not sources:
        raise FatalError("research_boundary_blocked")
    provider_sources, source_map = project(sources)

    def prepare_loopback(context_window, tokens):
        if not all_sources_safe:
            raise FatalError("research_boundary_blocked")
        # Leave room for the server clock and the format fallback instruction.
        budget = loopback_input_budget(context_window, tokens) - 512
        messages, options = review_prompt(review_snapshot, provider_sources, input_budget=budget)
        sent_map = {label: source_map[label] for label in options}
        def parse(raw):
            return record_gaps(restore(parse_selection(raw, sources=provider_sources, context=review_snapshot, options=options).model_dump(), sent_map))
        return messages, parse, sent_map

    return synthesize(did, payload, key=key, messages=messages_for(review_snapshot, provider_sources), schema=response_schema(),
                      parse=lambda raw: record_gaps(restore(parse_selection(raw, sources=provider_sources, context=review_snapshot).model_dump(), source_map)),
                      max_tokens=8192, provider_timeout_seconds=240, pin_provider=True, repair=repair, source_map=source_map,
                      prepare_loopback=prepare_loopback,
                      **({"cap_at_reservation": True} if runtime.get("cost_ceiling_micro") else {}))


def run_deep(payload: dict) -> None:
    from app.research.prior_art import runner as prior
    from app.research.prior_art import graveyard
    prior_enabled = prior.enabled() and (payload.get("phase") or "all") in {"all", "competitors", "prior_art"}
    did = ex.claim(payload, kind="deep", budget=prior.CALL_BUDGET if prior_enabled else CALL_BUDGET, prior_art_enabled=prior_enabled)
    row = ex.read(did)
    doc = json.loads(row.summary)
    prior_enabled = doc["_runtime"].get("prior_art_enabled", False)
    if row.state in {"complete", "partial"}:
        if doc.get("error_code") and doc["error_code"] != "research_cancelled":  # a stopped run kept as partial stays a failed job on redelivery
            raise FatalError(doc["error_code"])
        return
    snapshot = doc["_runtime"]["snapshot"]
    # The search query may include customer prose, but relevance is anchored
    # in the founder's stated problem when one is available.
    topic = research_topic(snapshot)
    phase = payload.get("phase") or "all"
    if phase not in PHASES:
        raise FatalError("research_phase_invalid")
    gaps = [gap("system_verification", "Search excerpts have not been verified against full source pages."),
            gap("willingness_to_pay", "Demand and willingness to pay require founder interviews or observed customer behavior.")]
    gaps.extend(snapshot.get("limitations") or [])
    if phase != "all":
        gaps.append(gap("system_scope", "This run covers only " + phase + "; prior dossiers retain other evidence."))
    graveyard_task = None
    def check_cancelled():
        if ex.cancel_requested(payload):
            raise FatalError("research_cancelled")
    try:
        check_cancelled()
        if not topic:
            _finish(did, payload, None, [*gaps, gap("system_scope", "No idea summary or audience is saved; add a topic or website description to research. No evidence was invented.")])
            return
        from app.core.local_mode import is_test_mode
        if is_test_mode():
            _finish(did, payload, None, [sample_research(did, payload, topic)])
            return
        inherit_feedback(did, payload)
        provider = _web_provider() if phase != "review" else None
        if prior_enabled:
            provider = _company_provider(provider)
        if not is_local() and prior_enabled and getattr(provider, "name", None) == "duckduckgo":
            # ddgs fans out to an unknown number of engines. Bounded prior-art
            # runs use configured web providers or the free capped APIs.
            provider = None
        check_cancelled()
        prior_plan, prior_gaps = prior.prepare(did, payload, snapshot) if prior_enabled else (None, [])
        check_cancelled()
        if not is_local() and phase in {"all", "competitors", "prior_art"}:
            graveyard_task = graveyard.start(did, payload, snapshot, prior_plan)
        searches, missing = 0, 0
        builtin_failed = False
        name_sources = []
        from app.research.prior_art.plan import queries as prior_queries
        planned_queries = queries_for(snapshot, phase) if not prior_plan else prior_queries(prior_plan)
        if prior_plan and phase == "all":
            planned_queries += [(key, query) for key, query in queries_for(snapshot, "all") if key in {"services", "customer_voice"}]
        def save_progress_plan(row, document, session):
            document["_runtime"].setdefault("progress_plan", [key for key, _ in planned_queries]
                + (["github"] if phase in {"all", "oss"} and not prior_plan else []) + ["review"])
        ex.mutate(did, payload, save_progress_plan)
        if prior_plan:
            check_cancelled()
            if is_local():
                prior_gaps.append(gap("system_adapter", "You are researching through your configured search provider. Dedicated public-directory lookups are not included; add original sources to check them."))
            else:
                prior_gaps.extend(prior.discover(did, payload, prior_plan))
        for key, query in planned_queries:
            check_cancelled()
            if provider is None:
                if is_local():
                    from app.research.local_providers import SEARCH_UNAVAILABLE
                    gaps.append(gap("system_adapter", SEARCH_UNAVAILABLE))
                else:
                    gaps.append(gap("system_adapter", "Web search provider is unavailable; no web evidence was invented."))
                break
            try:
                result = _search(did, payload, provider, key, query, topic, prior_plan=prior_plan if key.startswith("prior_") else None)
            except FatalError as exc:
                if str(exc) not in {"research_cost_budget_exhausted", "research_call_budget_exhausted"}:
                    raise
                prior_gaps.append(gap("system_request_limit", "The research cost limit was reached; retained evidence is available without further paid calls."))
                break
            searches += 1
            _store_sources(did, payload, result, query, key)
            _store_review_leads(did, payload, result, key)
            name_sources.extend(result.get("name_sources", []))
            if result.get("coverage_gap"):
                gaps.append(gap(result.get("coverage_gap_kind") or "system_request_limit", result["coverage_gap"]))
            if result.get("search_outcome") in {"limited", "unavailable"}:
                builtin_failed = True
            elif not result["items"]:
                missing += 1
        if missing:
            gaps.append(gap("system_scope", f"{missing} of {searches} searches found no pages specific to your idea."))
        check_cancelled()
        if prior_plan and provider and (name_sources or prior_plan.get("seed_competitors")):
            prior_gaps.extend(prior.name_followups(did, payload, prior_plan, name_sources, provider, _scrape_provider(provider), topic))
        check_cancelled()
        if provider is not None and not prior_plan and phase == "all" and not builtin_failed and not sources_for(did) and not any("rate limited" in g for g in gaps):
            # Zero qualified sources from a long, niche query (a rate-limited engine would only refuse again): one
            # broader, shorter retry before reporting that research found nothing.
            short = " ".join(" ".join(str(snapshot.get(k) or "") for k in ("offer", "summary", "problem")).split()[:8])
            if short:
                result = _search(did, payload, provider, "fallback", short + " alternatives", topic)
                _store_sources(did, payload, result, short, "direct")
                if result.get("coverage_gap"):
                    gaps.append(gap(result.get("coverage_gap_kind") or "system_request_limit", result["coverage_gap"]))
        if provider is not None and not prior_plan and phase in {"all", "competitors", "services"}:
            for index, query in enumerate(saved_followups(did, payload, topic=topic)):
                check_cancelled()
                # Domain scoping alone does not establish topicality: a broad
                # vendor's general pricing or blog page may be unrelated to
                # the founder's actual problem.
                host = query.split(" ", 1)[0].removeprefix("site:")
                result = _search(did, payload, provider, f"product_followup_{index}", query, topic, allowed_host=host)
                _store_sources(did, payload, result, query, "product_details")
                if result.get("coverage_gap"):
                    gaps.append(gap(result.get("coverage_gap_kind") or "system_request_limit", result["coverage_gap"]))
                if not result["items"]:
                    gaps.append(gap("competitor_offering", "No product/pricing details returned for a discovered source domain."))
        check_cancelled()
        if phase in {"all", "oss"} and not prior_plan:
            query = github_query(snapshot, topic)
            try:
                if not query:
                    raise FatalError("research_github_query_empty")
                result = _github(did, payload, query, topic)
            except (FatalError, RetryableError) as exc:
                if str(exc) in _RUN_FATAL or str(exc) == "research_cancelled":
                    raise
                # Repository search is free, optional evidence: record the gap and keep the run.
                message = ("You could not check public repositories. Configure your repository search token, then retry this research."
                           if is_local() else "GitHub repository search could not run for this idea; reusable open-source options were not checked.")
                gaps.append(gap("system_adapter", message))
            else:
                _store_sources(did, payload, result, query, "oss")
                if not result["items"] or result.get("incomplete"):
                    gaps.append(gap("system_adapter", "GitHub search returned no matching public repository or incomplete results."))
        check_cancelled()
        if provider is not None and phase != "review":
            if prior_plan:
                prior_gaps.extend(prior.enrich(did, payload, prior_plan, _scrape_provider(provider)))
            check_cancelled()
            reader = _scrape_provider(provider) if prior_plan or is_local() else provider
            if is_local() and reader is None:
                from app.research.local_providers import READER_UNAVAILABLE
                gaps.append(gap("system_adapter", READER_UNAVAILABLE))
            verify_top_sources(did, payload, reader)
        sources = sources_for(did)
        if prior_plan:
            safe = _boundary_safe_sources(sources, row.workspace_id)
            sources = safe
        verified = sum(bool(s["evidence"].get("page_verified")) for s in sources)
        if not sources:
            gaps.pop(0)  # nothing to verify; the missing-sources gaps already say so
        elif verified:
            gaps[0] = gap("system_verification", f"{verified} of {len(sources)} sources were verified against full pages; "
                          "the remaining sources are unverified search excerpts.")
        if len(sources) > 50:
            gaps.append(gap("system_request_limit", "Model review used a bounded subset of 50 sources; all retrieved sources remain in the dossier."))
            sources = sources[:50]
        review = None
        check_cancelled()
        if sources:
            try:
                try:
                    review = _review_sources(did, payload, snapshot, sources)
                except FatalError as exc:
                    if str(exc) != "research_boundary_blocked":
                        raise
                    safe = _boundary_safe_sources(sources, row.workspace_id)
                    gaps.append(gap("system_unsent_material", "Some source material was kept out of the AI assessment because it contained data we do not send. Original sources remain saved for your review."))
                    log.warning("research_assessment_boundary_blocked", extra={"dossier_id": did})
                    if not safe:
                        raise
                    # Separate durable receipt prevents a worker replay from
                    # resending either the blocked prompt or the cleaned retry.
                    check_cancelled()
                    review = _review_sources(did, payload, snapshot, safe, key="review-boundary-retry", repair=False)
            except FatalError as exc:
                if str(exc) not in {"research_invalid_output", "research_output_truncated", "research_boundary_blocked", "research_cost_budget_exhausted", "research_call_budget_exhausted", "research_context_length_exceeded"}:
                    raise
                # Retrieval is already durable and paid for. An invalid model
                # citation must not erase the sources or become an invented
                # assessment. Complete a source-only partial dossier instead;
                # the rejected review receipt remains available for diagnosis.
                if str(exc) == "research_boundary_blocked":
                    log.warning("research_assessment_boundary_blocked", extra={"dossier_id": did})
                elif str(exc) == "research_context_length_exceeded":
                    gaps.append(gap("system_model_context", REVIEW_CONTEXT_NEXT_STEP))
                else:
                    gaps.append(gap("system_assessment", "The research limit was reached; no additional assessment was charged." if str(exc) in {"research_cost_budget_exhausted", "research_call_budget_exhausted"} else
                                "The model assessment failed evidence validation. Retrieved sources remain available, but no assessment was accepted; review or rerun this phase."))
            if review:
                gaps.extend(saved_gaps(review))
        else:
            gaps.append(gap("system_scope", "No usable public source qualified as idea-specific evidence. A model assessment was not charged; review any separately labeled search leads yourself."))
        if prior_plan:
            prior_result = prior.finish(did, payload, review, prior_plan, prior_gaps)
            gaps.extend(saved_gaps(prior_result))
        elif phase == "review":
            reviewed_plan = json.loads(ex.read(did).summary)["_runtime"].get("prior_art_review_plan")
            if reviewed_plan:
                prior.finish(did, payload, review, reviewed_plan, [])
        check_cancelled()
        graveyard.collect(did, payload, graveyard_task)
        check_cancelled()
        _finish(did, payload, review, gaps)
    except RetryableError:
        raise
    except FatalError as exc:
        if str(exc) == "research_operation_in_progress":
            raise
        error_code = str(exc)
        # Retrieved sources are durable and already paid for. A later stop (e.g. an uncertain
        # provider outcome, whose credit hold stays protected) must not erase them or block
        # the journey: keep a source-only partial dossier the founder can plan from or rerun.
        usable = error_code not in _RUN_FATAL and bool(sources_for(did))
        def failed(row, document, session):
            row.state = "partial" if usable else "failed"
            if usable:
                document.update(review=None, review_kind="retrieved_sources_only")
                row.completed_at = utcnow()
            document["error_code"] = error_code
            document["coverage_gaps"] = list(dict.fromkeys(gaps + [gap("system_stopped", "Research stopped before completion; retained sources remain available.")]))
            record_gaps(document)
        try:
            ex.mutate(did, payload, failed)
        except FatalError:
            pass  # A reclaimed worker cannot overwrite the current owner's state.
        if error_code == "research_cancelled":
            return
        raise
    finally:
        if graveyard_task:
            graveyard_task[1].set()
