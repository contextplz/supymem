"""Published and labelled business guesses; social profiles are never fetch targets."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections import Counter
from contextlib import nullcontext
from urllib.parse import parse_qs, urljoin, urlsplit

from sqlalchemy import func, select

from app.chat.recovery import log_founder_exception as _log_founder_exception
from app.gtm import enrich, playbooks
from app.gtm.discovery import Judgement, ReachDrafts

log = logging.getLogger(__name__)


def log_founder_exception(error, venture_id, action):
    from app.core.jobs import FatalError, RetryableError
    if isinstance(error, (FatalError, RetryableError)):
        from app.gtm.journey import _log_search_failure
        _log_search_failure(error, venture_id, action)
    else:
        _log_founder_exception(error, venture_id, action)

ROUTE_PRIORITY = {"booking": -1, "email": 1, "contact_form": 2, "linkedin": 3, "x": 4, "forum": 5, "phone": 6, "website": 7}
PUBLISH_BATCH_SIZE = 10
_CHAINS = re.compile(r"\b(?:Aspen Dental|Heartland Dental|Pacific Dental|Smile Brands|Castle Dental|Jefferson Dental|Bupa|MyDentist"
    r"|Cult\.?\s?fit|Gold[’']?s Gym|Anytime Fitness|Snap Fitness|Planet Fitness|Talwalkars|Crunch Fitness|F45 Training|Orangetheory|PureGym)\b"
    # Multi-site operators state their footprint ("30+ clubs in Maharashtra") or sell franchises.
    r"|\b\d{2,}\+?\s+(?:clubs|centres|centers|locations|branches|outlets|gyms|studios)\b|\bfranchise (?:opportunit|enquir|inquir|partner)", re.I)


class PublicPages:
    async def allowed(self, url: str):
        from urllib.robotparser import RobotFileParser
        from app.gtm.scrapy_leads import _fetch
        from app.gtm import email_learning
        from app.gtm.journey import _signal_url
        if not _signal_url(url):
            return False
        robots_url = urljoin(url, "/robots.txt")
        key = email_learning.cache_key("public_pages", "robots", robots_url)
        policy = email_learning.cached(key)
        if policy is None:
            policy = await asyncio.to_thread(_fetch, robots_url, allow_missing=True)
            if policy is None:
                return False
            email_learning.cache_save(key, policy)
        robots = RobotFileParser()
        robots.parse(policy.splitlines())
        return robots.can_fetch("SupymemResearch", url)

    async def scrape(self, url: str, *, main_only: bool = True):
        from app.gtm.scrapy_leads import _fetch
        from app.gtm import email_learning
        if not await self.allowed(url):
            return ""
        key = email_learning.cache_key("public_pages", "scrape", url)
        saved = email_learning.cached(key)
        if saved is not None:
            return saved
        result = await asyncio.to_thread(_fetch, url) or ""
        email_learning.cache_save(key, result)
        return result


async def _finished(tasks):
    try:
        for task in asyncio.as_completed(tasks):
            yield await task
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def published_route(lead: dict) -> dict | None:
    return next((route for route in lead.get("routes", []) if route.get("kind") == "email" and route.get("email")
        and route.get("route_type", "published") == "published"), None)


def record_judged_live_leads(workspace_id: str, venture_id: str, leads: list[dict]) -> None:
    """The one call point for live results that passed the quality judge; the Lead Library lane implements the write."""


def _profile_can_fit_without_need(candidate: dict, context: dict) -> bool:
    if context.get("target_type") == "person" and candidate["kind"] == "person":
        return True
    if candidate["kind"] != "person" or candidate.get("expert_role") or candidate["routes"][0]["kind"] != "linkedin":
        return False
    if context.get("mode") != "B2C":
        return True
    # A private person with no stated need stays only when their own profile names the audience's place.
    place = re.search(r"\b(?:in|near|around)\s+([A-Z][\w.-]+(?:\s+[A-Z][\w.-]+)*)", context.get("audience") or "")
    return bool(place and re.search(r"\b" + re.escape(place[1]) + r"\b", candidate["text"], re.I))


def contact_seed(lead: dict) -> dict | None:
    """A judged lead's contact route as a journey._ingest row."""
    from app.core.local_mode import is_local

    # A published address becomes a contact too, so its draft joins the Gmail review batch, not only "Copy email".
    route = published_route(lead) or next((route for route in lead.get("routes", []) if route.get("route_type") in {"guess", "pattern"}), None)
    if route is None and is_local():
        route = next((route for route in lead.get("routes", []) if route.get("source_url")), None)
    if not route or lead.get("judging_status") != "judged":
        return None
    guessed = route.get("route_type") in {"guess", "pattern"}
    person_name = route["name"] if guessed else lead["name"] if lead.get("kind") == "person" else None
    name = person_name.split() if person_name else []
    company = lead.get("company") or (lead["name"] if lead.get("kind") == "business" else "")
    if person_name and company.casefold() == person_name.casefold():
        company = ""
    return {"website": lead.get("business_url") or route["source_url"], "source_url": lead["source_url"], "organization": company,
        **({"business_url": lead.get("business_url")} if lead.get("kind") == "person" else {}),
        "enriched": True, "email": route.get("email"), "first_name": name[0] if name else None, "last_name": " ".join(name[1:]),
        **({"email_route": route} if guessed else {"email_confidence": "published"}),
        "person_name": person_name, "person_role": lead.get("person_role") or lead.get("role"), "person_source_url": route["source_url"], "person_quote": route.get("quote") or lead.get("quote", ""),
        "contact_source_url": route["source_url"], "contact_quote": route.get("quote") or lead.get("quote", ""), "country": lead.get("country"),
        "why": lead.get("why"), "why_now": lead.get("value_exchange"), "evidence_label": "fit_only" if lead.get("evidence_label") == "Public source" else lead.get("evidence_label"), "score": lead.get("fit", 0), "signals": lead.get("signals", [])}


def _generic_experts(context):
    from app.core.local_mode import is_local
    return is_local() and not context.get("requested_role") and not context.get("organisation_audience")


def _publish(journal, payload, leads, *, final=False):
    from app.gtm.quality import dedupe, limit_fits
    ingested = 0
    decisions = []
    def update(row, doc, session):
        nonlocal ingested
        incoming = {lead["id"] for lead in leads}
        incoming.update(hashlib.sha256(candidate["url"].encode()).hexdigest()[:24]
            for key in ("reachable_candidates_quality_v1", "public_reachable_candidates_quality_v1")
            for candidate in doc.get("_runtime", {}).get(key, []))
        for lead in leads:
            lead["display_quote"] = enrich.fit_excerpt(lead.get("quote", ""))
            for route in lead.get("routes", []):
                route["display_quote"] = enrich.contact_excerpt(route.get("display_quote") or route.get("quote", ""), route.get("email") or route.get("value") or "")
        saved = {lead["id"]: lead for lead in (doc.get("reachable_leads") or []) + (doc.get("industry_experts") or [])}
        previously_judged = {lead["id"] for lead in saved.values() if lead.get("judging_status") == "judged"}
        saved.update({lead["id"]: lead for lead in leads if lead.get("judging_status") != "pending" or saved.get(lead["id"], {}).get("judging_status") != "judged"})
        from app.gtm import journey
        icp = doc.get("icp") or {}
        target = doc.get("search_target") or {}
        who = target.get("audience") if target.get("target_type") == "person" else icp.get("who")
        audience = " ".join((who or "", journey._audience_location(who or "", icp)))
        geographic = [lead for lead in saved.values() if not journey._geography_mismatch(audience,
            lead.get("business_url") or lead.get("source_url") or "", lead.get("location") or "",
            lead.get("country") or "", require_match=True)]
        geographic_ids = {lead["id"] for lead in geographic}
        unique = dedupe(sorted(geographic, key=rank))
        deduplicated_ids = {lead["id"] for lead in unique}
        if target.get("target_type") == "person":
            unique = [lead for lead in prefer_exact_roles(unique) if lead.get("judging_status") == "judged"
                and (lead.get("kind") == "person" and lead.get("role") and playbooks.real_person(lead)
                    or _generic_experts(target) and lead.get("kind") == "business"
                    and (lead.get("expert_role") or lead.get("lead_type") == "insight")
                    or lead.get("company_contact_fallback"))]
        targeted_ids = {lead["id"] for lead in unique}
        if target.get("target_type") == "person":
            unique = unique[:journey._keep_limit(len(doc.get("discovery_plan") or []), target.get("requested_count"))]
        for field, expert in (("reachable_leads", False), ("industry_experts", True)):
            matching = [lead for lead in unique if bool(lead.get("expert_role") or lead.get("lead_type") == "insight") == expert]
            kept = limit_fits(sorted((lead for lead in matching if lead.get("judging_status") != "pending"),
                key=lambda lead: (lead["id"] not in incoming, rank(lead))),
                journey._keep_limit(len(doc.get("discovery_plan") or []) + len(doc.get("expert_discovery_plan") or []), target.get("requested_count")))
            pending = [] if final else [lead for lead in matching if lead.get("judging_status") == "pending"]
            doc[field] = limit_fits(kept + pending, journey.MAX_CANDIDATES)
        # Insight leads (industry_experts) are listed under "People you can reach" too, so both lists become contacts.
        listed = {lead["id"] for field in ("reachable_leads", "industry_experts") for lead in doc[field]}
        for lead in leads:
            if session is not None and lead["id"] in listed and (seed := contact_seed(lead)):
                ingested += journey._ingest(payload["workspace_id"], payload["venture_id"], [seed], session=session)
            if lead.get("judging_status") == "judged" and lead["id"] not in previously_judged:
                reason = ("" if lead["id"] in listed else "geography_mismatch" if lead["id"] not in geographic_ids
                    else "duplicate_source" if lead["id"] not in deduplicated_ids
                    else "person_target_filter" if lead["id"] not in targeted_ids else "publication_limit")
                decisions.append((lead, reason))
        if final and session is not None and payload.get("_source_yield"):
            from copy import deepcopy

            visible([deepcopy(lead) for lead in leads if lead["id"] in listed], payload["workspace_id"],
                b2b=not journey._consumer(icp), venture_id=payload["venture_id"], session=session, trace_payload=payload)
    journal.mutate(payload["journey_id"], payload, update)
    for lead, reason in decisions:
        possible_fit = not reason and lead.get("evidence_label") == "possible_fit"
        trace(payload, lead["source_url"], "dropped" if reason else "kept", reason or ("possible_fit" if possible_fit else ""),
            **({"track": "reachable"} if possible_fit else {}))
    return ingested


def _call_draft(lead, problem):
    from app.gtm.discovery import ReachDraft
    greeting = recipient_name(lead)
    topic = " ".join(problem.split()).rstrip(".!?")
    if re.match(r"^(?:a|an|the)\s", topic, re.I):
        topic = topic[:1].lower() + topic[1:]
    else:
        topic = "this workflow: " + topic if topic else "this workflow"
    lead.update(draft=f"Hi {greeting}, I'm researching {topic}. I came across your published work and would value your perspective. "
        "Would you be open to a 15-minute call about your experience? Happy to share what I find out. Reply with a time.",
        connection_note=f"Hi {greeting}, I'm researching {topic}. Open to a 15-minute call about your experience? Happy to share what I find out. Reply with a time.", draft_source="template")
    if lead.get("evidence_label") in {"fit_only", "Public source"}:
        seen = playbooks._observation(lead)
        lead.update(draft=f"Hi {greeting}, " + (f'I read this on your public page: "{seen}". ' if seen else "")
            + "I would value your perspective. "
            "Could we discuss whether the problem I'm researching is relevant to your work in a 15-minute call? "
            "Happy to share what I find out. Reply with a time.",
            connection_note=f"Hi {greeting}, could we discuss whether this topic is relevant to your work in a 15-minute call? Happy to share what I find out. Reply with a time.")
    if lead.get("evidence_label") == "possible_fit":
        lead.update(draft=f"Hi {greeting}, I'm researching {topic}. Does this problem apply to your experience? "
            "If so, would you be open to a 15-minute research call? Happy to share what I find out. Reply with a time.",
            connection_note=f"Hi {greeting}, does the problem I'm researching apply to your experience? Open to a 15-minute research call? Happy to share what I find out. Reply with a time.")
    try:
        lead.update(draft=normalise_draft(lead["draft"], lead), connection_note=normalise_draft(lead["connection_note"], lead))
        checked = ReachDraft.model_validate({"id": 0, "body": lead["draft"], "connection_note": lead["connection_note"]})
        if len(checked.connection_note) > 280 or checked.body != lead["draft"] or checked.connection_note != lead["connection_note"]:
            raise ValueError("draft_not_safe_or_short")
        lead.update(draft=checked.body, connection_note=checked.connection_note)
    except ValueError:
        lead.update(draft="Hi, I'm researching this workflow and would value your experience. Would you be open to a 15-minute call? Happy to share what I find out. Reply with a time.",
            connection_note="Hi, open to a 15-minute call about your experience? Happy to share what I find out. Reply with a time.")
        if lead.get("evidence_label") == "possible_fit":
            lead.update(draft="Hi, does the problem I'm researching apply to your experience? If so, open to a 15-minute research call? Reply with a time.",
                connection_note="Hi, does the problem I'm researching apply to your experience? If so, open to a 15-minute research call? Reply with a time.")
    lead["x_note"] = lead["connection_note"]


def recipient_name(lead):
    person = lead.get("person")
    business = lead.get("kind") == "business" or lead.get("kind") != "person" and bool(lead.get("company")) and lead["company"].strip().casefold() == lead["name"].strip().casefold()
    if person and person.strip().casefold() != (lead.get("company") or lead["name"]).strip().casefold():
        return person.split()[0]
    return lead["name"] + " team" if business else lead["name"].split()[0]


def normalise_draft(text, lead):
    from app.gtm.discovery import normalise_note
    text = re.sub(r"^(?:Hi|Hello|Hey|Dear)\s+[^,!:]+[,!:]\s*", lambda _: f"Hi {recipient_name(lead)},\n\n", text, flags=re.I)
    return normalise_note(text)


def _pending(candidate, context):
    from app.gtm import journey
    if not candidate.get("quality_selected"):
        return None
    if journey._same_offer(candidate["url"], str(candidate.get("title") or ""), candidate["text"], context["competitors"], context["offer"], context["problem"], expert_role=candidate.get("expert_role")):
        return None
    if re.search(r"\b(?:intern|aspiring|seeking|open to work|job board|recruiter|staffing agency|marketplace|aggregator)\b", candidate["text"], re.I):
        return None
    if re.search(r"\bindependent\b|\b1 to 3 dentists\b", context["audience"], re.I) and _CHAINS.search(candidate["text"]):
        return None
    if candidate["routes"][0]["kind"] == "x" and not re.search(
        r"\b(?:freelance|founder|owner|manager|coordinator|coach|educator|instructor|YouTuber|engineer|practitioner|operator|author|researcher)\b|\bI (?:am|run|manage|need|teach|work|have|struggle|cannot|can't)\b|\bmy (?:clients|practice|invoices|child|children)\b", candidate["text"], re.I):
        return None
    name = candidate.get("profile_facts", {}).get("name") or candidate.get("author") or candidate.get("title") or ""
    if not name.strip() or not journey._verbatim(name, candidate["text"]) or not candidate.get("routes"):
        return None
    page = next((page for page in candidate["pages"] if page.get("text", "").strip()), None)
    if not page or not reach_url(page["url"]):
        return None
    lead = {"id": hashlib.sha256(candidate["url"].encode()).hexdigest()[:24], "name": name, "company": "", "role": "", "location": "",
        "fit": 0, "why": "Public search match; relevance to your work has not yet been checked.", "value_exchange": "Ask about their experience; do not assume they need your product.",
        "quote": page["text"][:1000], "source_url": page["url"], "routes": candidate["routes"], "country": candidate.get("country", ""),
        "lead_type": "unclassified", "expert_role": candidate.get("expert_role"), "judging_status": "pending", "outreach_hint": outreach_hint(candidate.get("country", "")), "signals": candidate.get("signals", [])}
    if candidate.get("evidence_label") == "fit_only":
        reason = enrich.fit_reason(lead["quote"], lead["source_url"])
        lead.update(evidence_label="fit_only", why=reason, value_exchange=reason)
    if candidate.get("evidence_label") == "possible_fit":
        lead["evidence_label"] = "possible_fit"
    _call_draft(lead, context["problem"])
    return lead


def profile_facts(title: str, text: str) -> dict:
    clean = re.sub(r"\s*(?:\||-)\s*LinkedIn\s*$", "", title, flags=re.I)
    parts = re.split(r"\s+[\-–—]\s+", clean, maxsplit=2)
    facts = {"name": parts[0], "role": parts[1] if len(parts) > 1 else "", "company": parts[2] if len(parts) > 2 else ""}
    role = re.split(r"\s+(?:at|@)\s+", facts["role"], maxsplit=1)
    if len(role) == 2:
        facts.update(role=role[0], company=role[1])
    match = re.match(r"^(.*?)\s*\(@([A-Za-z0-9_]{1,15})\)\s*/\s*X$", title)
    if match:
        facts.update(name=match[1].strip(), role="", company="", handle="@" + match[2])
    seniority = next((label for label, pattern in (("owner", r"owner|founder|principal|proprietor|managing partner|self.employed|freelance|sole trader"),
        ("executive", r"CEO|CTO|COO|CFO|chief|president|managing director"), ("director", r"VP|vice president|head of|director"), ("manager", r"manager|lead")) if re.search(r"\b(?:" + pattern + r")\b", text, re.I)), "")
    return facts | {"seniority": seniority}


def outreach_hint(country: str, corporate: dict | None = None, *, business_name: str = "", company_number: str = "") -> dict:
    if country in {"GB", "UK"}:
        company = bool(corporate or company_number or re.search(r"\b(?:Ltd|Limited|LLP|PLC|CIC|Group)\b", business_name, re.I))
        return {"country": "GB", "subscriber": "corporate" if corporate else "unverified" if company else "unverified_individual", "source_url": corporate["url"] if corporate else "",
            "note": "" if company else "UK note: if this is a sole trader, ask permission before sending marketing email."}
    if country in enrich.EEA_GEOS or country == "EU":
        return {"country": country, "subscriber": "unverified", "note": "Check local electronic-marketing rules and lawful basis; provide a privacy notice at first contact and honour suppression."}
    if country == "US":
        return {"country": "US", "subscriber": "unverified", "note": "Commercial email needs accurate headers, identification, a postal address and opt-out; honour suppression. Guessed business addresses may bounce."}
    return {"country": country, "subscriber": "unverified", "note": "Public contact details are not permission. Check recipient jurisdiction and honour suppression."}


def _diagnostic_url(url):
    from app.research.deep import safe_source_url

    source = safe_source_url(str(url or ""))
    return urlsplit(source)._replace(query="", fragment="").geturl() if source else ""


def observe_source(payload, url, raw, *, title="", page_type=None, companies=(), candidate_url=None):
    from app.core.local_mode import is_local
    from app.gtm.email_learning import opaque

    source = _diagnostic_url(url)
    if not is_local() or not source:
        return
    classified = enrich.classify(url, title)
    page_type = page_type or ("directory" if classified == "directory" or re.search(r"\bdirector(?:y|ies)\b", title, re.I) else "list" if enrich._LISTICLE.search(title)
        else "article" if classified == "article" else "company site")
    host = enrich._host(url)
    facts = enrich.extract(host, [(url, raw)], published_mailboxes=True)
    names = [person["name"] for person in facts["people"]]
    published, _ = enrich._published_contacts(raw)
    extracted = {contact["email"] for contact in facts["contacts"]}
    routes = []
    for email in list(dict.fromkeys(email.casefold() for email, _ in published))[:12]:
        company = email.split("@", 1)[0] in enrich.ROLE_EMAIL_LOCALS
        reason = ("" if email in extracted else "no_solicitation" if facts.get("no_solicitation")
            else "generic_inbox" if company else "email_not_eligible")
        routes.append({"kind": "email", "ref": opaque(email, "source-yield")[:16],
            "mailbox": "company" if company else "named_or_other", "rejected_by": reason})
    routes.extend({"kind": kind, **({"url": target} if (target := _diagnostic_url(facts[kind].get("url"))) else {})}
        for kind in ("contact_form", "booking", "phone") if facts.get(kind))
    if enrich._CONTACT_WORDS.search(urlsplit(url).path):
        routes.append({"kind": "website", "url": source, "rejected_by": "no_solicitation" if facts.get("no_solicitation") else ""})
    routes.extend({"kind": route["kind"], "url": _diagnostic_url(route["url"])} for route in leadership_routes(url, raw, facts["people"]))
    record = {"url": source, "page_type": page_type, "names": names, "companies": list(dict.fromkeys(
        name for name in [*companies, facts["name"]] if name))[:12], "routes": routes, "rejections": {}}
    records = payload.setdefault("_source_yield", {})
    previous = records.setdefault(source, record)
    if previous is not record:
        for field in ("names", "companies", "routes"):
            previous[field].extend(value for value in record[field] if value not in previous[field])
        if page_type in {"profile", "directory", "list"}:
            previous["page_type"] = page_type
    if candidate_url:
        payload.setdefault("_source_candidate", {}).setdefault(_diagnostic_url(candidate_url), set()).add(source)


def save_source_yield(payload):
    if records := payload.get("_source_yield"):
        from app.gtm import journey

        journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session:
            doc["_runtime"].setdefault("source_yield", {}).update(records))


def save_job_search_diagnostics(job, runtime, payload, *, report=False):
    from app.gtm.email_learning import opaque
    from app.research.local_providers import search_diagnostics, search_was_limited

    saved = json.loads(job.payload_json)
    previous = saved.get("_runtime", {})
    queries = previous.get("search_queries", {}) | (search_diagnostics.get() or {})
    records = runtime.get("source_yield", {}) | payload.get("_source_yield", {})
    reads = {_diagnostic_url(url): value for url, value in runtime.get("source_reads", {}).items()}
    sources = {}
    for url in (records | reads):
        if not url:
            continue
        record, read = records.get(url, {}), reads.get(url, {})
        routes = record.get("routes", [])
        reasons = Counter(record.get("rejections", {}))
        sources[opaque(url, "source-yield")] = {
            "page_type": record.get("page_type"), "names": len(record.get("names", [])),
            "companies": len(record.get("companies", [])), "routes": dict(Counter(route["kind"] for route in routes)),
            "rejected_routes": dict(Counter(route["rejected_by"] for route in routes if route.get("rejected_by"))),
            "rejections": sum(reasons.values()), "rejection_reasons": dict(reasons), "read_state": read.get("state"),
            "read_reason": read.get("reason"), "http_status": read.get("diagnostics", {}).get("http_status"),
        }
    sources = previous.get("source_yield", {}) | sources
    diagnostics = {"source_yield": sources, "search_queries": queries}
    if any(previous.get(key) != value for key, value in diagnostics.items()):
        saved["_runtime"] = previous | diagnostics
        job.payload_json = json.dumps(saved)
    if report and (queries or sources):
        by_outcome = {}
        for key, source in sources.items():
            outcome = " ".join(str(part) for part in (
                source["read_state"] or "observed", source["read_reason"], source["http_status"]) if part)
            by_outcome.setdefault(outcome, []).append(key[:12])
        summary = {"job_id": job.id, "queries": {
            key[:12]: [query["results"], query["reason"]] for key, query in queries.items()}, "sources": by_outcome}
        log.info("gtm_customer_search_diagnostics: %s", json.dumps(summary, separators=(",", ":")))
    return search_was_limited(queries)


def trace(payload: dict, url: str, stage: str, reason: str = "", **facts):
    from app.gtm import journey
    from app.gtm.discovery import Query
    # Content-free per-run tally, logged once at INFO with gtm_journey_discovery.
    # Reasons can carry exception text or URLs; keep only the leading code.
    code = re.match(r"[a-z0-9_]{0,60}", reason or "").group()
    track = facts.get("track") if facts.get("track") in {"seed", "signal", "need_signal", "channel", "portfolio", "reachable", "expert"} else "seed"
    source = _diagnostic_url(url)
    event = {"url": source, "stage": stage, "reason": code or ("qualified_public_source" if stage == "kept" else "")}
    if "track" in facts:
        event["track"] = track
    if facts.get("intent") in Query.model_fields["intent"].annotation.__args__:
        event["intent"] = facts["intent"]
    journey.log.info("gtm_lead_trace", extra={"lead_trace": event})
    key = f"{track}:{stage}:{code}" if code else f"{track}:{stage}"
    counts = payload.setdefault("_trace_counts", {})
    counts[key] = counts.get(key, 0) + 1
    rejected = stage in {"dropped", "rejected"} or stage.endswith("_rejected")
    if rejected and code and (records := payload.get("_source_yield")):
        for page in {source} | payload.get("_source_candidate", {}).get(source, set()):
            if page in records:
                reasons = records[page]["rejections"]
                if isinstance(reasons, list):
                    reasons = records[page]["rejections"] = dict(Counter(reasons))
                reasons[code] = reasons.get(code, 0) + 1
    if stage == "candidate" or rejected:
        # Public site domains only (never paths or text): shows which sources reach and fail the gates.
        try:
            host = (urlsplit(source).hostname or "").removeprefix("www.")[:60]
        except ValueError:
            host = ""
        if re.fullmatch(r"[a-z0-9.-]+", host):
            if stage in {"candidate", "dropped"}:
                hosts = payload.setdefault("_trace_hosts", {})
                hosts[f"{stage}:{host}"] = hosts.get(f"{stage}:{host}", 0) + 1
            if rejected:
                drops = payload.setdefault("_trace_drops", {})
                key = f"{code or stage}:{host}"
                drops[key] = drops.get(key, 0) + 1


def reach_url(value: str) -> str | None:
    from app.gtm.scrapy_leads import _seed_url
    from app.research.deep import safe_source_url
    url = safe_source_url(value) if _seed_url(value) else None
    if not url:
        return None
    parts, host = urlsplit(url), enrich._host(url)
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and re.fullmatch(r"/in/[^/]+/?", parts.path):
        return "https://www.linkedin.com" + parts.path.rstrip("/")
    if any(host == social or host.endswith("." + social) for social in ("x.com", "twitter.com")) and re.fullmatch(r"/[A-Za-z0-9_]{1,15}/?", parts.path):
        return "https://x.com" + parts.path.rstrip("/").lower()
    return url


def person_page_routes(url: str, raw: str, text: str, *, organisation_contact: bool = False) -> list[dict]:
    """A named person's page: any address it publishes on its own site, then the page itself."""
    host = enrich._host(url)
    facts = enrich.extract(host, [(url, raw)])
    if facts.get("no_solicitation"):
        return []
    company_page = organisation_contact and enrich.classify(url) == "company"
    if company_page:
        facts["readable_sources"] = [url]
        facts["leadership_routes"] = leadership_routes(url, raw, facts["people"])
    published = contact_routes(facts, host=host)
    emails = [route for route in published if route["kind"] == "email"]
    personal = [route for route in emails if route["email_kind"] != "business_role"]
    # shortcut: one person per page, so a roster's several personal addresses can't be attributed; split roster entries to keep them.
    if len(personal) > 1:
        emails = [route for route in emails if route["email_kind"] == "business_role"]
    return emails + ([route for route in published if route["kind"] != "email"] if company_page else []) + [
        {"kind": "website", "url": url, "source_url": url, "quote": text,
         **({"company_contact": True} if company_page and enrich._CONTACT_WORDS.search(urlsplit(url).path) else {})}]


def leadership_routes(url, raw, people):
    routes = []
    for link in dict.fromkeys(re.findall(r'https?://(?:www\.)?(?:linkedin\.com/in|x\.com|twitter\.com)/[\w-]+/?', raw, re.I)):
        target = reach_url(link)
        if not target:
            continue
        # shortcut: match names in public profile slugs; use published identity markup when vanity handles need attribution.
        slug = urlsplit(target).path.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"-[a-z0-9]*\d[a-z0-9]*$", "", slug, flags=re.I)
        slug = re.sub(r"[\W_]", "", slug).casefold()
        person = next((person for person in people if slug == re.sub(r"[\W_]", "", person["name"]).casefold()), None)
        if person:
            routes.append({"kind": "linkedin" if enrich._host(target) == "linkedin.com" else "x", "url": target,
                "source_url": url, "quote": person["quote"], "person": person["name"]})
    return routes


def contact_routes(facts: dict, *, host: str = "") -> list[dict]:
    routes = [{"kind": "email", "email": contact["email"], "email_kind": contact["kind"], "route_type": "published", "confidence": 100, "confidence_label": "Published address", "source_url": contact["source_url"], "quote": contact["quote"], "display_quote": contact.get("display_quote", contact["quote"]),
        **({"published_as": contact["published_as"]} if contact.get("published_as") else {})} for contact in facts["contacts"]]
    routes.extend({"kind": kind, **facts[kind]} for kind in ("booking", "contact_form", "phone") if facts.get(kind))
    if facts.get("readable_sources") and not facts.get("no_solicitation"):
        if (page := facts.get("contact_page")) and not facts.get("contact_form"):
            routes.append({"kind": "website", "url": page, "source_url": page, "company_contact": True,
                "quote": facts.get("contact_page_quote", "")})
        routes.extend(facts.get("leadership_routes", []))
    if not routes and facts.get("home") and not facts.get("no_solicitation"):
        # The business's own site is the honest last route: no address is implied and no email can be drafted from it.
        page = facts.get("contact_page") or facts["home"]
        if facts.get("readable_sources") and page not in facts["readable_sources"]:
            page = facts["readable_sources"][0]
        quote = (facts.get("market_role") or {}).get("homepage_quote") or next((source["text"]
            for source in facts.get("evidence_pages", []) if source["url"] == page), "")
        routes.append({"kind": "website", "url": page, "source_url": page, "quote": quote})
    for route in routes:
        if facts.get("readable_sources") and (route["kind"] == "contact_form" or route.get("email_kind") in {"business_role", "published_business"}):
            route["company_contact"] = True
            if route["kind"] == "email":
                route["confidence_label"] = "Published company address"
        route["display_quote"] = enrich.contact_excerpt(route.get("display_quote") or route.get("quote", ""), route.get("email") or route.get("value") or "")
    return sorted((route for route in routes if not host or enrich._host(route["source_url"]) == host),
        key=lambda route: ROUTE_PRIORITY[route["kind"]])


def route_priority(route):
    if route["kind"] == "email" and route.get("route_type") in {"pattern", "guess"}:
        return (1.1 if route["route_type"] == "pattern" else 1.5) + (100 - max(0, min(100, route.get("confidence", 40)))) / 500
    return -1 if route["kind"] == "booking" else 0 if route.get("decision_maker") else ROUTE_PRIORITY.get(route["kind"], 7)


def author_routes(source: str, text: str, quote: str, author: str = "") -> dict:
    from app.gtm.journey import _verbatim
    host = enrich._host(source)
    if host in {"news.ycombinator.com", "reddit.com"}:
        headers = list(re.finditer(r"(?m)^Posted by \[([^\]]+)\]\((https?://[^\s)]+)\)", text))
        for index, heading in enumerate(headers):
            block = text[heading.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)]
            if _verbatim(quote, block):
                text, author = heading[0] + "\n\n" + block, heading[1]
                break
    links = [(label, urljoin(source, url)) for label, url in re.findall(r"\[([^\]]+)\]\(([^\s)]+)\)", text)]
    links += [(re.sub(r"<[^>]+>", "", label), urljoin(source, url))
        for url, label in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, re.S | re.I)]
    passage = next((block for block in re.split(r"\n\s*\n", text) if _verbatim(quote, block)), "")
    header = text[:text.find(passage)] if passage else ""
    if len(header) > 1000 or re.search(r"\b(?:I|we|my|our)\b", header, re.I):
        header = ""
    scoped = header + passage
    if host == "coffeeforums.com":
        headers = list(re.finditer(r"(?m)^#{2,6}\s+\[([^\]]+)\]\((https?://[^\s)]+/members/[^\s)]+)\)", text))
        for index, heading in enumerate(headers):
            block = text[heading.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)]
            if _verbatim(quote, block) and enrich._host(heading[2]) == host and reach_url(heading[2]):
                return {"author": heading[1], "author_url": heading[2], "thread_url": source}
    for label, link in links:
        path = urlsplit(link).path
        if enrich._host(link) != host or not reach_url(link):
            continue
        if host == "news.ycombinator.com":
            if path != "/user" or parse_qs(urlsplit(link).query).get("id") != [label.strip()]:
                continue
        elif not (re.match(r"^/(?:u|user)/[^/]+/?$", path) if host == "reddit.com" else re.match(r"^/(?:u|users|members|profile)/[^/]+(?:/[^/]+)?/?$", path)):
            continue
        if not label.strip() or not ((author and label.strip().removeprefix("u/") == author.removeprefix("u/") and link in scoped)
            or (link in scoped and re.search(r"(?:author|posted by|submitted by|by)\s*[: ]*\[?" + re.escape(label), scoped, re.I))):
            continue
        return {"author": label.strip().removeprefix("u/"), "author_url": link, "thread_url": source}
    return {}


def profile_key(value):
    url = reach_url(value or "")
    host = enrich._host(url or "")
    if url and (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return "https://linkedin.com" + urlsplit(url).path.rstrip("/").casefold()
    if url and any(host == social or host.endswith("." + social) for social in ("x.com", "twitter.com")):
        return "https://x.com/" + urlsplit(url).path.strip("/").split("/", 1)[0].casefold()
    return url


def seen_business_domains(urls):
    from app.gtm.journey import _signal_url
    return {enrich._host(url) for url in urls if url and _signal_url(url) and enrich.classify(url) == "company"
        and not (enrich._host(url) == "reddit.com" or enrich._host(url).endswith(".reddit.com")
            or enrich._host(url).startswith(("forum.", "community."))
            or re.search(r"/(?:comments|posts?|threads?|topics?|users?|members?|t)/", urlsplit(url).path))}


def directory_listing(hit: dict, context: dict) -> bool:
    from app.core.local_mode import is_local
    from app.gtm.journey import _consumer_thread_url, _keywords, _person_audience
    from app.research.source_quality import _stems
    local_expert = is_local() and bool(hit.get("expert_role"))
    if (context.get("mode") != "B2B" or hit.get("expert_role")) and not local_expert:
        return False
    if not local_expert and _person_audience(context) and _consumer_thread_url(str(hit.get("url") or "")):
        return False
    title = str(hit.get("title") or "")
    text = title + " " + str(hit.get("snippet") or hit.get("description") or hit.get("content") or "")
    url = urlsplit(str(hit.get("url") or ""))
    if is_local() and not hit.get("expert_role") and context.get("target_type") != "person" and hit.get("kind") == "business":
        if enrich.classify(url.geturl(), title if url.path.strip("/") else "") in {"article", "directory"}:
            return True
    listing = re.search(r"\b(?:director(?:y|ies)|listings?|finder|find[- ](?:a[- ]|an[- ]|your[- ])|compare local)\b",
        text + " " + url.path, re.I)
    # "Top 10 gyms in Pune" pages list businesses; a business's own homepage ("Best gym in Pune") is not a listing.
    listing = listing or url.path.strip("/") and re.search(r"\b(?:\d+\s+best|top|best)\s+(?:\d+\s+)?(?:\w+\s+){0,2}\w+s\s+(?:in|near)\b", title, re.I)
    if is_local():
        listing = listing or re.search(r"/(?:members|speakers)/?$", url.path, re.I)
    # ponytail: lexical directory/audience overlap, use a grounded listing classifier if recall needs synonyms.
    audience = hit["expert_role"] if local_expert else context.get("audience", "")
    return bool(listing and _stems(_keywords(audience, 10)) & _stems(text))


def people_page_rejection(url: str, title: str = "") -> str:
    from urllib.parse import unquote
    host = enrich._host(url)
    if any(host == domain or host.endswith("." + domain) for domain in enrich._NON_PEOPLE_DOMAINS):
        return "non_people_domain"
    if host.split(".")[0] in {"careers", "jobs", "recruitment"}:
        return "employment_page"
    path = unquote(urlsplit(url).path).lower()
    if re.search(r"/(?:careers?|jobs?|vacancies|recruitment|join-us|job-board)(?:[/_.-]|$)", path):
        return "employment_page"
    if re.search(r"/(?:doi|abs|papers?|publications?|phds?|citations?|search|tags?|categories|feeds?|rss)(?:[/_.-]|$)", path):
        return "index_or_listing"
    if re.match(r"\s*(?:jobs?\b|careers?\b|vacancies\b|job search\b|phd (?:positions?|programmes?)\b)", title, re.IGNORECASE):
        return "employment_page"
    return ""


def candidate_hits(hits: list[dict], context: dict, payload: dict | None = None) -> list[dict]:
    from app.gtm import journey
    from app.gtm.discovery import hit_target_fit
    excluded = {profile_key(url) for url in context.get("seen_lead_urls", [])}
    excluded_domains = seen_business_domains(excluded)
    candidates, seen = [], set()
    def drop(url, reason):
        if payload is not None:
            trace(payload, url or "", "dropped", reason)
    for hit in hits:
        url = reach_url(str(hit.get("url") or ""))
        if url and (reason := people_page_rejection(url, str(hit.get("title") or ""))):
            drop(url, reason)
            continue
        reason = journey._consumer_hit_rejection(hit, context)
        if reason:
            drop(url, reason)
            continue
        if not hit.get("expert_role") and not hit_target_fit(hit, context):
            drop(url, "customer_industry_mismatch")
            continue
        key, host = profile_key(url), enrich._host(url or "")
        if not url or key in excluded or context.get("target_type") != "person" and host in excluded_domains:
            drop(url, "invalid_reach_url" if not url else "already_seen")
            continue
        social = not journey._signal_url(url)
        forum = host == "reddit.com" or host.endswith(".reddit.com") or host.startswith(("forum.", "community.")) or context.get("mode") == "B2C" and journey._consumer_thread_url(url)
        if context.get("target_type") == "person" and hit.get("kind") == "person":
            kind = "person"
        elif not hit.get("expert_role") and journey._person_audience(context) and journey._consumer_thread_url(url):
            kind = "person"
        elif social or forum:
            if hit.get("kind") != "person":
                drop(url, "not_person_profile")
                continue
            kind = "person"
        else:
            directory = directory_listing(hit, context)
            if not directory and any(host == domain or host.endswith("." + domain) for domain in journey.outreach._AGGREGATORS):
                drop(url, "aggregator")
                continue
            if not directory and enrich.classify(url, str(hit.get("title") or "")) != "company":
                drop(url, "directory_or_article")
                continue
            if journey._same_offer(url, str(hit.get("title") or ""), "", context.get("competitors", []), context.get("offer", ""), context.get("problem", ""), expert_role=hit.get("expert_role")):
                drop(url, "competitor_or_same_offer")
                continue
            kind, key = "business", url if directory else host
        if key in seen or journey._geography_mismatch(context["audience"], url,
                str(hit.get("title") or "") + " " + str(hit.get("snippet") or ""), str(hit.get("country") or "")):
            drop(url, "duplicate_candidate" if key in seen else "geography_mismatch")
            continue
        seen.add(key)
        candidates.append(hit | {"url": url, "kind": kind})
    return candidates


def visible(leads: list[dict], wid: str, *, b2b: bool = True, venture_id: str | None = None, session=None, trace_payload=None) -> list[dict]:
    if not leads:
        return []
    identities = [(lead, {str(route.get("email") or "").strip().lower() for route in lead.get("routes", [])},
        {profile_key(url) for url in [*(route.get("url") for route in lead.get("routes", [])), *lead.get("related_profiles", []),
            *(lead.get(key) for key in ("source_url", "profile_url", "author_url"))]})
        for lead in leads]
    from app.db.models import GtmContact, GtmSuppression
    from app.core.local_mode import is_local
    from app.gtm import journey
    with (nullcontext(session) if session is not None else journey.db()()) as session:
        if venture_id:
            session.info["gtm_consumer_ventures"] = {venture_id: not b2b}
        suppressed = set(session.scalars(select(func.lower(GtmSuppression.email)).where(GtmSuppression.workspace_id == wid)))
        suppressed_profiles = {profile_key(url) for url in session.scalars(select(GtmSuppression.linkedin_url).where(GtmSuppression.workspace_id == wid)) if url}
        contacts = list(session.scalars(select(GtmContact).where(GtmContact.workspace_id == wid)))
        from app.gtm.email_learning import globally_suppressed, opaque, prefetch_suppression
        def record_suppression(route):
            if trace_payload is None:
                return
            url = route.get("source_url")
            trace(trace_payload, url, "dropped", "suppression")
            source = trace_payload.get("_source_yield", {}).get(_diagnostic_url(url), {})
            ref = opaque(route["email"], "source-yield")[:16]
            for entry in source.get("routes", []):
                if entry.get("ref") == ref:
                    entry["rejected_by"] = "suppression"

        routes = [route for lead in leads for route in lead.get("routes", [])]
        prefetch_suppression(session, [route.get("email") for route in routes] + [contact.email for contact in contacts],
            workspace_id=wid, generated_emails=[route.get("email") for route in routes if route.get("route_type") in {"guess", "pattern"}]
                + [contact.email for contact in contacts if journey._json(contact.enrichment_json).get("email_route")])
        if is_local():
            from app.gtm.relationships.suppression import opted_out
            contacts_by_email = {(contact.email or "").strip().casefold(): contact for contact in contacts}
            for lead in leads:
                remaining_routes = []
                for route in lead.get("routes", []):
                    email = route.get("email")
                    if email:
                        contact = contacts_by_email.get(email.strip().casefold()) or GtmContact(workspace_id=wid, email=email)
                        if journey.outreach._suppressed(session, wid, contact, suppressed_emails=suppressed):
                            if lead.get("email_status") != "opted_out":
                                lead["email_status"] = "opted_out" if opted_out(session, wid, contact) else "suppressed"
                            record_suppression(route)
                            continue
                    remaining_routes.append(route)
                lead["routes"] = remaining_routes
        for lead in leads:
            blocked = [route for route in lead.get("routes", []) if route.get("email") and globally_suppressed(session, route["email"])]
            if blocked:
                lead["email_status"] = "opted_out" if any(opaque(route["email"]) in session.info["gtm_shared_opt_out"] for route in blocked) else "suppressed"
                if is_local():
                    suppressed.update(route["email"].strip().lower() for route in blocked)
                for route in blocked:
                    record_suppression(route)
            lead["routes"] = [route for route in lead.get("routes", []) if not route.get("email") or not globally_suppressed(session, route["email"])]
        from app.config import settings
        from app.gtm import email_learning
        for lead in leads:
            lead["routes"] = [route for route in lead["routes"] if route.get("route_type") not in {"guess", "pattern"} or b2b and settings.guessed_emails == "on"
                and lead.get("country") not in {"DE", "AT"} and not route["email"].split("@")[1].endswith((".de", ".at"))
                and route["email"].casefold() not in session.info["gtm_generated_blocked"]
                and route.get("pattern") not in email_learning.retired_patterns(route["email"].rsplit("@", 1)[-1], session=session)]
        stopped = [contact for contact in contacts if journey.outreach._suppressed(session, wid, contact, suppressed_emails=suppressed)]
    suppressed.update((contact.email or "").lower() for contact in stopped if contact.email)
    names = {" ".join(value for value in (contact.first_name, contact.last_name) if value).casefold() for contact in stopped} - {""}
    profiles = ({profile_key(contact.linkedin_url) for contact in stopped} | suppressed_profiles) - {None}
    for contact in stopped:
        data = journey._json(contact.enrichment_json)
        names.update(str(data.get(key) or "").casefold() for key in ("person_name", "author") if data.get(key))
        profiles.update(profile_key(data[key]) for key in ("author_url", "linkedin_url", "profile_url") if data.get(key))
        suppressed.update(str(route.get("email") or "").lower() for route in data.get("contacts") or [] if route.get("email"))
    profiles.discard(None)
    for lead in leads:
        lead["display_quote"] = enrich.fit_excerpt(lead.get("quote", ""))
        for route in lead.get("routes", []):
            route["display_quote"] = enrich.contact_excerpt(route.get("display_quote") or route.get("quote", ""), route.get("email") or route.get("value") or "")
        lead["people"] = [person for person in lead.get("people") or [] if person["name"].casefold() not in names and not (person.get("email") and person["email"].lower() in suppressed)]
    kept = []
    for lead, emails, lead_profiles in identities:
        blocked = (emails & suppressed or lead_profiles & profiles or lead.get("suppressed")
            or {str(lead.get(key) or "").casefold() for key in ("name", "person", "author")} & names)
        if blocked:
            if trace_payload is not None:
                trace(trace_payload, lead.get("source_url"), "dropped", "suppression")
            continue
        if any(route["kind"] in {"booking", "email", "contact_form", "phone", "website"} for route in lead.get("routes", [])) or str(lead.get("value_exchange") or "").strip():
            kept.append(lead)
    return kept


def rank(lead: dict) -> tuple:
    routes = lead["routes"]
    priority = min((route_priority(route) for route in routes), default=7)
    return bool(lead.get("company_contact_fallback")), lead.get("role_match") == "closest_senior", lead.get("evidence_label") == "possible_fit", priority, -lead["fit"], lead["id"]


def prefer_exact_roles(leads: list[dict]) -> list[dict]:
    exact_companies = {str(lead.get("company") or "").strip().casefold() for lead in leads
        if lead.get("role_match") == "exact" and lead.get("company")}
    kept, senior_companies = [], set()
    for lead in leads:
        company = str(lead.get("company") or "").strip().casefold()
        if lead.get("company_contact_fallback") and company in exact_companies:
            continue
        if lead.get("role_match") == "closest_senior":
            if company in exact_companies or company in senior_companies:
                continue
            senior_companies.add(company)
        kept.append(lead)
    return kept


async def discover(provider, hits: list[dict], signals: list[dict], context: dict, payload: dict, *, fallback=False) -> list[dict]:
    from app.gtm import journey, quality
    from app.gtm.discovery import hit_target_fit
    from app.core.jobs import FatalError, RetryableError
    from app.credits.errors import CreditError, InsufficientCredits
    from app.signals.local_reader import EXPECTED_PAGE_READ_ERRORS
    from app.signals.research import FirecrawlLimiterUnavailable
    from app.core.local_mode import is_local
    generic_experts = _generic_experts(context)
    organisation_contact = is_local() and context["mode"] == "B2B" and context.get("target_type") == "person" and bool(context.get("organisation_audience"))
    candidates, domains, urls, profiles = [], set(), set(), {}
    excluded = {profile_key(url) for url in context.get("seen_lead_urls", [])}
    excluded_domains = seen_business_domains(excluded)
    seen_profiles = set()
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    candidate_key = "public_reachable_candidates_quality_v1" if fallback else "reachable_candidates_quality_v1"
    if context.get("_candidate_round"):
        candidate_key += "_" + str(context["_candidate_round"])
    context = context | {"_fallback": fallback}
    if runtime.get(candidate_key) is not None and runtime.get(candidate_key + "_complete", True):
        hits, signals, candidates = [], [], runtime[candidate_key]
    elif is_local() and runtime.get(candidate_key):
        candidates = runtime[candidate_key]
        saved_urls = {candidate["url"] for candidate in candidates}
        hits = [hit for hit in hits if reach_url(str(hit.get("url") or "")) not in saved_urls]
    business_jobs, person_jobs, unread_people = [], [], []
    planned_sources = []
    journal = journey._journal(payload)

    def record_source(url, state, reason, diagnostics=None):
        if not is_local():
            return
        if state == "failed" and reason == enrich.SOURCE_SEARCH_LIMIT:
            state = "skipped"
        def update(row, doc, session):
            reads = doc["_runtime"].setdefault("source_reads", {})
            previous = reads.get(url, {})
            if previous.get("state") == "failed" and (state in {"reading", "skipped"} or
                    reason == "empty page" and diagnostics and diagnostics["http_status"] is None):
                return
            reads[url] = {"state": state, "reason": reason, **({"diagnostics": diagnostics} if diagnostics else {})}
            if any(result["state"] == "failed" for result in reads.values()):
                doc["source_access"] = "partial" if any(result["state"] == "read" for result in reads.values()) else "unavailable"
            if state in {"read", "failed", "skipped"}:
                from app.db.models import Job

                if session is not None and payload.get("_job_id"):
                    save_job_search_diagnostics(session.get(Job, payload["_job_id"]), doc["_runtime"], payload)
                if state == "read" and previous.get("state") != "read":
                    from core.logging import _local_secret_values
                    from core.redact import redact_string

                    host = urlsplit(_diagnostic_url(url)).hostname or ""
                    log.info("gtm_source_read", extra={"source_host": redact_string(host,
                        exact_values=[value.lower() for value in _local_secret_values() or ()])})
        journal.mutate(payload["journey_id"], payload, update)

    def save_candidates():
        if is_local():
            journal.mutate(payload["journey_id"], payload, lambda row, doc, session:
                doc["_runtime"].update({candidate_key: candidates, candidate_key + "_complete": False}))

    def failed_source(url, **details):
        failure = enrich.source_read_failure(url, **details)
        record_source(url, "failed", failure["reason"], failure.get("diagnostics"))

    read_gate = asyncio.Semaphore(enrich.CONCURRENCY)
    async def read_person(hit, url):
        async with read_gate:
            search_result_only = False
            record_source(url, "reading", None)
            try:
                raw = await enrich.read_source(lambda: journey._signal_call(payload, "public_page", url,
                    lambda: provider.scrape(url, main_only=False), meter="firecrawl_scrape", cache_identity={"main_only": False}))
            except CreditError:
                raise
            except FatalError as error:
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.fetch")
                if str(error) != "gtm_call_budget_exhausted":
                    raise
                if is_local():
                    failed_source(url, error=error)
                    return
                raw = None
            except Exception as error:
                if is_local():
                    failed_source(url, error=error)
                    return
                status = getattr(getattr(error, "response", None), "status_code", None)
                log.warning("gtm_person_page_unavailable", extra={"venture_id": payload["venture_id"],
                    "error_type": type(error).__name__, "status": status if type(status) is int else None,
                    "source_url": urlsplit(url)._replace(query="", fragment="").geturl()})
                raw = None
                search_result_only = True
            if is_local() and (reason := enrich.source_content_reason(raw)):
                failed_source(url, raw=raw, reason=reason)
                return
            if is_local():
                observe_source(payload, url, raw, page_type="profile", candidate_url=url)
            if raw is None:
                unread_people.append(url)
                trace(payload, url, "dropped", "public_person_page_unavailable")
            text = quality.main_text(raw or "")
            if quality.blocked_page(raw or ""):
                record_source(url, "failed", "blocked access")
                log.warning("gtm_person_page_unavailable", extra={"venture_id": payload["venture_id"],
                    "error_type": "blocked_page", "status": None, "source_url": urlsplit(url)._replace(query="", fragment="").geturl()})
                unread_people.append(url)
                search_result_only = True
                trace(payload, url, "dropped", "public_person_page_unreadable")
                if is_local():
                    return
            if search_result_only:
                text = str(hit.get("title") or "") + "\n" + str(hit.get("snippet") or hit.get("description") or "")
                if not re.search(enrich._NAME, text) or not hit_target_fit(hit, context):
                    return
            elif not text:
                failed_source(url, raw=raw, reason="unreadable text or format")
                return
            if not search_result_only:
                record_source(url, "read", None)
            routes = ([{"kind": "website", "url": url, "source_url": url, "quote": text}] if search_result_only
                else person_page_routes(url, raw, text, organisation_contact=organisation_contact))
            if not routes:
                trace(payload, url, "dropped", "no_solicitation")
                return
            # shortcut: one person per source page, split roster entries when several requested people share a page.
            candidates.append({"kind": "person", "url": url, "title": hit.get("title"), "text": text,
                "expert_role": hit.get("expert_role"),
                "pages": [{"url": url, "text": text}], "search_result_only": search_result_only,
                "contact_pages": [] if search_result_only else [(url, raw)],
                "routes": routes})
            trace(payload, url, "candidate", source="search_result" if search_result_only else "public_person_page")
            save_candidates()
    async def read_business(hit, url, host, text):
        async with read_gate:
            trace(payload, url, "candidate", source="business")
            async def public_page(page):
                saved = journey._journal(payload).document(payload["journey_id"])["_runtime"]
                public = saved.get("public_pages", {}).get(page)
                if isinstance(provider, PublicPages) and public is None and not saved.get("operations", {}).get(journey._signal_key(saved, "contact:" + host, page)):
                    try:
                        public = await journey._signal_call(payload, "public_page", page, lambda: PublicPages().scrape(page)) or ""
                    except FatalError as error:
                        log_founder_exception(error, payload["venture_id"], "gtm.reachable.public_page")
                        if str(error) != "gtm_call_budget_exhausted":
                            raise
                        public = ""
                    except Exception as exc:
                        log_founder_exception(exc, payload["venture_id"], "gtm.reachable.public_page")
                        public = ""
                    journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session:
                        doc["_runtime"].setdefault("public_pages", {}).update({page: public}))
                if public and quality.blocked_page(public):
                    trace(payload, page, "rejected", "blocked_page")
                    return ""
                return public if public and not re.match(r"\s*404", public) else ""
            async def scrape(page):
                try:
                    page_provider = provider
                    if isinstance(provider, PublicPages) and page != urljoin(url, "/"):
                        if not enrich.enabled():
                            return ""
                        from app.signals.research import HttpResearchProvider
                        page_provider = HttpResearchProvider()
                    public = await public_page(page) if page_provider is provider else ""
                    if public:
                        return public
                    value = await journey._signal_call(payload, "contact:" + host, page,
                        lambda: asyncio.wait_for(page_provider.scrape(page, main_only=False),
                            timeout=30 if isinstance(page_provider, PublicPages) else None),
                        meter=None if isinstance(page_provider, PublicPages) else "firecrawl_scrape", cache_identity={"main_only": False}) or ""
                except FirecrawlLimiterUnavailable:
                    raise
                except Exception as error:
                    if not is_local() or not isinstance(error, EXPECTED_PAGE_READ_ERRORS):
                        log_founder_exception(error, payload["venture_id"], "gtm.reachable.fetch")
                    trace(payload, page, "fetch_failed", enrich.source_failure_reason(error).replace(" ", "_"))
                    raise
                trace(payload, url, "fetched", page=page, chars=len(value))
                return value
            try:
                async def main_scrape(page):
                    public = await public_page(page)
                    if public:
                        return quality.main_text(public)
                    return await journey._signal_call(payload, "contact:" + host, page + "#quality-main-v1",
                        lambda: provider.scrape(page), meter=None if isinstance(provider, PublicPages) else "firecrawl_scrape",
                        cache_identity={"main_only": True, "excludeTags": quality.EXCLUDE_TAGS}) or ""
                source_context = context | {"expert_role": hit["expert_role"]} if hit.get("expert_role") else context
                facts = await enrich.enrich_site(provider, url, payload["workspace_id"], payload["venture_id"], scrape=scrape, published_mailboxes=True,
                    context=source_context, payload=payload, main_scrape=main_scrape, on_source=record_source,
                    on_read=lambda page, raw: observe_source(payload, page, raw, page_type="company site",
                        companies=[hit.get("title")], candidate_url=url))
                if facts.get("quality_rejected"):
                    failures = facts.get("read_failures")
                    reason = ("search_budget_exhausted" if failures and all(failure["reason"] == enrich.SOURCE_SEARCH_LIMIT for failure in failures)
                        else "source_unreadable" if failures else "homepage_anti_icp_or_low_fit")
                    trace(payload, url, "dropped", reason)
                    return
            except FirecrawlLimiterUnavailable:
                raise RetryableError("gtm_enrichment_slot_unavailable", retry_after_s=30) from None
            except RetryableError as error:
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.enrich")
                raise
            except CreditError as error:
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.enrich")
                trace(payload, url, "dropped", "credits_exhausted" if isinstance(error, InsufficientCredits) else "enrichment_failed")
                return
            except FatalError as error:
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.enrich")
                if str(error) in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                    trace(payload, url, "dropped", str(error))
                    return
                raise
            except Exception as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if is_local() and isinstance(error, EXPECTED_PAGE_READ_ERRORS):
                    failed_source(url, error=error)
                elif type(status) is int and 400 <= status < 600:
                    log.warning("gtm_enrichment_page_unavailable", extra={"venture_id": payload["venture_id"],
                        "error_type": type(error).__name__, "status": status})
                else:
                    log_founder_exception(error, payload["venture_id"], "gtm.reachable.enrich")
                trace(payload, url, "dropped", "enrichment_error", error=type(error).__name__)
                return
            if journey._same_offer(url, str(hit.get("title") or ""), facts["company_text"], context["competitors"], context["offer"], context["problem"], expert_role=hit.get("expert_role")):
                trace(payload, url, "dropped", "fetched_competitor_or_same_offer")
                return
            routes = contact_routes(facts, host=host)
            if context.get("_company_contact_fallback"):
                routes = [route for route in routes if route.get("company_contact") or route["kind"] in {"phone", "booking"}]
            hiring = []
            # logic adapted from ethanplusai/harvey (MIT)
            careers = facts.get("careers_url")
            if routes and careers and journey._signal_url(careers) and (careers in facts["pages"]
                    or len(set(facts["pages"]) - {facts["home"]}) < enrich.MAX_PAGES - 1):
                try:
                    raw = await scrape(careers)
                    if not enrich.source_content_reason(raw):
                        observe_source(payload, careers, raw, page_type="company site", companies=[hit.get("title")], candidate_url=url)
                    hiring = enrich.hiring_signals(careers, raw)
                    if hiring:
                        facts["evidence_pages"].append({"url": careers, "text": enrich._clean(raw)[0][:10000]})
                except Exception as error:
                    log_founder_exception(error, payload["venture_id"], "gtm.reachable.hiring")
                    if isinstance(error, FatalError) and str(error) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                        raise
                    trace(payload, url, "hiring_unavailable", type(error).__name__)
            # A site with no published route gets no guessed address either: a fit-only lead is never emailed on a guess.
            guesses = [] if hit.get("expert_role") or context.get("_company_contact_fallback") or any(route["kind"] == "website" for route in routes) else await asyncio.to_thread(enrich.guessed_routes, facts | {"country": facts.get("country") or hit.get("country")}, host, payload["workspace_id"], b2b=context["mode"] == "B2B")
            routes.extend(guesses)
            facts["guessed_routes"] = guesses
            routes.sort(key=route_priority)
            trace(payload, url, "contact_extracted", emails=len(facts["contacts"]), form=bool(facts["contact_form"]), phone=bool(facts["phone"]))
            if routes and len(candidates) < journey.MAX_CANDIDATES:
                pages = facts["evidence_pages"]
                corporate_name = re.search(r"\b(?:company|registered)\s*(?:name)?\s*[:\-]\s*([^\n<]{2,120}\b(?:Limited|Ltd|LLP|PLC))\b", facts["company_text"], re.I)
                company_number = re.search(r"\b(?:company|registration)\s*(?:number|no\.?)\s*[:#]?\s*([A-Z]{2}\d{6}|\d{8})\b", facts["company_text"], re.I)
                if journey._geography_mismatch(context["audience"], url, facts["company_text"], facts.get("country") or ""):
                    trace(payload, url, "dropped", "expert_geography_mismatch")
                    return
                candidates.append({"kind": "business", "expert_role": hit.get("expert_role"), "url": url, "title": hit.get("title"), "text": facts["company_text"],
                    "market_role": facts.get("market_role"), "quality_selected": bool(facts.get("market_role")), "evidence_label": facts.get("evidence_label"),
                    "pages": pages, "routes": routes, "people": facts["people"], "country": facts.get("country") or hit.get("country") or ("GB" if host.endswith(".uk") or re.search(r"\b(?:UK|United Kingdom|London|Manchester|Bristol|England)\b", text + facts["company_text"]) else ""),
                    "corporate_name": corporate_name[1] if corporate_name else facts.get("name") if re.search(r"\b(?:Limited|Ltd|LLP|PLC)\b", facts.get("name") or "", re.I) else None,
                    "company_number": company_number[1].upper() if company_number else None,
                    "registry": {key: hit[key] for key in ("registry_url", "attribution", "provider") if hit.get(key)},
                    "profile_urls": facts["profile_urls"], "signals": hiring})
                save_candidates()
                if lead := _pending(candidates[-1], context):
                    _publish(journal, payload, [lead])
            else:
                trace(payload, url, "dropped", "no_published_contact_route")
    business_count = 0
    hits = sorted(hits, key=lambda hit: context["mode"] == "B2B" and hit.get("kind") == "person")
    queued_urls = {reach_url(str(hit.get("url") or "")) for hit in hits}
    mined_count = 0
    mining_limit = 8 if is_local() else 10
    used_domains = {key.removeprefix("contact:") for key in runtime["calls_used"] if key.startswith("contact:")}
    for index, hit in enumerate(hits):
        url = reach_url(str(hit.get("url") or ""))
        if url and (reason := people_page_rejection(url, str(hit.get("title") or ""))):
            trace(payload, url, "dropped", reason, track="reachable")
            continue
        if not hit.get("expert_role") and journey._person_audience(context) and journey._consumer_thread_url(url or ""):
            hit = hit | {"kind": "person"}
        reason = journey._consumer_hit_rejection(hit, context)
        if reason:
            trace(payload, url or "", "dropped", reason, track="reachable")
            continue
        if not hit.get("expert_role") and not hit_target_fit(hit, context):
            trace(payload, url or "", "dropped", "customer_industry_mismatch")
            continue
        if profile_key(url) in excluded or context.get("target_type") != "person" and enrich._host(url or "") in excluded_domains or len(candidates) + len(business_jobs) + len(person_jobs) >= journey.MAX_CANDIDATES:
            trace(payload, url or "", "dropped", "already_seen_or_candidate_budget")
            continue
        trace(payload, str(hit.get("url") or ""), "search_result", intent=hit.get("kind"))
        if not url or url in urls:
            trace(payload, str(hit.get("url") or ""), "dropped", "invalid_reach_url" if not url else "duplicate_url")
            continue
        urls.add(url)
        host, path = enrich._host(url), urlsplit(url).path
        expert = bool(hit.get("expert_role"))
        text = str(hit.get("title") or "") + "\n" + str(hit.get("snippet") or hit.get("description") or "")
        if journey._signal_url(url) and hit.get("content"):
            text = str(hit.get("title") or "") + "\n" + str(hit["content"])
        if context.get("target_type") == "person" and hit["kind"] == "person" and journey._signal_url(url) and not (generic_experts and hit.get("expert_role")) and not directory_listing(hit, context):
            if not provider:
                raise RuntimeError("people_page_provider_unavailable")
            if len(person_jobs) >= runtime["ceilings"]["public_page"] - runtime["calls_used"].get("public_page", 0):
                trace(payload, url, "dropped", "public_person_page_limit")
                continue
            person_jobs.append(lambda hit=hit, url=url: read_person(hit, url))
            planned_sources.append(url)
            continue
        if directory_listing(hit, context):
            if mined_count >= mining_limit or not provider or (not fallback and not enrich.enabled()):
                continue
            try:
                async def read_directory():
                    reader = provider if is_local() else PublicPages()
                    return await journey._signal_call(payload, "public_page", url, lambda: reader.scrape(url, main_only=False),
                        cache_identity={"main_only": False})
                if is_local():
                    record_source(url, "reading", None)
                raw = await enrich.read_source(lambda: journey._signal_call(payload, "directory_pages", url, read_directory)) or ""
            except Exception as error:
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.directory")
                if is_local() and isinstance(error, (CreditError, RetryableError, FirecrawlLimiterUnavailable)):
                    raise
                if isinstance(error, FatalError) and str(error) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                    raise
                if is_local():
                    failed_source(url, error=error)
                trace(payload, url, "dropped", "directory_page_failed", error=type(error).__name__)
                continue
            if is_local():
                if reason := enrich.source_content_reason(raw):
                    failed_source(url, raw=raw, reason=reason)
                    continue
                record_source(url, "read", None)
            organisations = enrich.directory_organisations(raw, url, hit.get("expert_role") or context.get("audience", ""))
            observe_source(payload, url, raw, title=hit.get("title") or "", companies=[name for _, name in organisations])
            seeds = [{"url": home, "title": name if is_local() else enrich._host(home), "snippet": enrich.fit_excerpt(raw), "kind": "business",
                **({"expert_role": hit["expert_role"]} if hit.get("expert_role") else {})}
                for home, name in organisations]
            mined = []
            for seed in candidate_hits(seeds, context, payload):
                if seed["url"] in queued_urls or mined_count >= mining_limit:
                    trace(payload, url, "dropped", "duplicate_company" if seed["url"] in queued_urls else "company_hop_limit")
                    continue
                queued_urls.add(seed["url"])
                mined.append(seed)
                mined_count += 1
                trace(payload, seed["url"], "candidate", "directory_mined")
            if not seeds:
                trace(payload, url, "dropped", "no_official_company_link")
            hits[index + 1:index + 1] = mined
            continue
        if journey._person_audience(context) and not expert and hit["kind"] == "person" and journey._consumer_thread_url(url):
            try:
                hit, post, metadata = await journey._signal_page(provider, hit, payload, context=context)
                if metadata.get("statusCode", 200) >= 400 or not post.strip() or quality.blocked_page(post):
                    trace(payload, url, "dropped", "source_not_accessible", track="reachable")
                    continue
                text = post
                hit = hit | {"metadata": metadata}
            except Exception as error:
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.signal")
                if isinstance(error, FirecrawlLimiterUnavailable):
                    raise RetryableError("gtm_discovery_slot_unavailable", retry_after_s=30) from None
                if isinstance(error, RetryableError):
                    raise
                if isinstance(error, CreditError) or isinstance(error, FatalError) and str(error) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                    raise
                trace(payload, url, "dropped", "signal_read_failed", track="reachable", error=type(error).__name__)
                continue
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            if re.fullmatch(r"/in/[^/]+/?", path):
                if profile_key(url) in seen_profiles:
                    trace(payload, url, "dropped", "duplicate_profile_slug")
                    continue
                seen_profiles.add(profile_key(url))
                candidates.append({"kind": "person", "url": url, "title": hit.get("title"), "text": text,
                    "profile_facts": profile_facts(str(hit.get("title") or ""), text), "pages": [{"url": url, "text": text}], "routes": [{"kind": "linkedin", "url": url, "source_url": url, "quote": text}]})
                trace(payload, url, "candidate", source="linkedin", fetch="forbidden")
            else:
                trace(payload, url, "dropped", "not_linkedin_profile")
            continue
        if any(host == social or host.endswith("." + social) for social in ("x.com", "twitter.com")):
            match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/?", path)
            if not match or match[1].lower() in {"home", "search", "explore", "i", "intent", "share", "settings", "messages", "notifications", "hashtag", "lists", "login", "signup", "compose", "help", "about", "privacy", "tos", "download"}:
                trace(payload, url, "dropped", "not_x_profile")
                continue
            profile = "https://x.com/" + match[1]
            if profile.casefold() not in profiles:
                profiles[profile.casefold()] = {"kind": "person", "url": url, "title": hit.get("title"), "text": text,
                    "profile_facts": profile_facts(str(hit.get("title") or ""), text),
                    "handle": "@" + match[1], "profile_url": profile, "pages": [{"url": url, "text": text}],
                    "routes": [{"kind": "x", "url": profile, "source_url": url, "quote": text}]}
                candidates.append(profiles[profile.casefold()])
                trace(payload, url, "candidate", source="x", fetch="forbidden")
            else:
                profiles[profile.casefold()]["text"] += "\n" + text
                profiles[profile.casefold()]["pages"].append({"url": url, "text": text})
                trace(payload, url, "candidate", source="x", merged_into=profile)
            continue
        if hit["kind"] == "person":
            pages = quality.signal_pages(hit, text, context)
            evidence = "\n\n".join(page["text"] for page in reversed(pages))
            routes = author_routes(url, evidence, pages[0]["text"] if pages[0].get("source") == "search_snippet" else str(hit.get("snippet") or ""), str(hit.get("author") or ""))
            if journey._person_audience(context) and host in {"reddit.com", "news.ycombinator.com"}:
                signal = journey._qualify_signal(hit | {"kind": "customer_voice"}, evidence, hit.get("metadata") or {},
                    context["problem"], context["audience"], journey.utcnow(), context=context)
                if signal and signal.get("author_url"):
                    routes = {"author": signal["author"], "author_url": signal["author_url"], "thread_url": url}
            if not routes and host == "reddit.com" and re.fullmatch(r"/(?:u|user)/[^/]+/?", path):
                routes = {"author": path.rstrip("/").rsplit("/", 1)[-1], "author_url": url, "thread_url": url}
            if routes:
                candidates.append({"kind": "person", "url": url, "title": hit.get("title"), "text": text,
                    "metadata": hit.get("metadata") or {}, "created_utc": hit.get("created_utc"),
                    **({"signal_pages": hit["signal_pages"]} if hit.get("signal_pages") else {}),
                    "pages": pages, **routes, "routes": [{"kind": "forum", "url": routes["thread_url"], "source_url": url, "quote": text},
                        {"kind": "forum", "url": routes["author_url"], "source_url": url, "quote": text, "profile": True}]})
                trace(payload, url, "candidate", source="forum")
                continue
            else:
                if enrich.classify(url, str(hit.get("title") or "")) not in ({"company", "article"} if expert else {"company"}):
                    trace(payload, url, "dropped", "no_attributed_author_route")
                    continue
        registry_only = hit.get("provider") == "npi"
        if registry_only:
            trace(payload, url, "dropped", "own_homepage_required")
            continue
        if context["mode"] == "B2C" and not expert or not provider or (not fallback and not enrich.enabled() and not expert) or not journey._signal_url(url) or host in domains:
            trace(payload, url, "dropped", "business_disabled_or_duplicate_domain")
            continue
        if enrich.classify(url, str(hit.get("title") or "")) not in ({"company", "article"} if expert else {"company"}) or host == "reddit.com" or re.search(r"(?:^|\.)(?:forum|community)\.", host):
            trace(payload, url, "dropped", "directory_or_article")
            continue
        if journey._same_offer(url, str(hit.get("title") or ""), "", context["competitors"], context["offer"], context["problem"], expert_role=hit.get("expert_role")):
            trace(payload, url, "dropped", "competitor_or_same_offer")
            continue
        if business_count >= journey.MAX_PROSPECTS:
            continue
        business_count += 1
        if host not in used_domains and len(used_domains) >= journey.MAX_PROSPECTS:
            trace(payload, url, "dropped", "business_budget")
            continue
        domains.add(host)
        used_domains.add(host)
        business_jobs.append(lambda hit=hit, url=url, host=host, text=text: read_business(hit, url, host, text))
        planned_sources.append(urljoin(url, "/"))
    if is_local() and planned_sources:
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: [
            doc["_runtime"].setdefault("source_reads", {}).setdefault(url, {"state": "pending", "reason": None}) for url in planned_sources])
    _publish(journal, payload, [lead for candidate in candidates if (lead := _pending(candidate, context))])
    await journey._parallel(business_jobs + person_jobs)
    if is_local() and not candidates and not signals and (business_jobs or person_jobs):
        reads = journal.document(payload["journey_id"])["_runtime"].get("source_reads", {})
        if reads and all(result["state"] == "failed" for result in reads.values()):
            from app.signals.local_reader import SourcesUnreadable
            raise SourcesUnreadable("public_sources_unavailable")
    if person_jobs and len(unread_people) == len(person_jobs) and not candidates:
        raise RuntimeError("people_page_unavailable")
    for signal in signals:
        published = journey._signal_date(signal.get("published_at"))
        now = journey.utcnow()
        fresh = published and now - journey.timedelta(days=90 if context["mode"] == "B2C" else 365) <= published <= now
        if fresh and signal.get("author_url") and signal["source_url"] not in urls and profile_key(signal["source_url"]) not in excluded:
            urls.add(signal["source_url"])
            candidates.append({"kind": "person", "url": signal["source_url"], "title": signal.get("author"), "text": signal["quote"],
                **({"quality_selected": True, "evidence_label": "possible_fit"} if signal.get("evidence_label") == "possible_fit" else {}),
                "author": signal["author"], "pages": [{"url": signal["source_url"], "text": signal["quote"], **({"relevance": signal["evidence_relevance"], "evidence_label": "possible_fit"} if signal.get("evidence_label") == "possible_fit" else {})}], "signals": [signal],
                "routes": [{"kind": "forum", "url": signal["source_url"], "source_url": signal["source_url"], "quote": signal["quote"]},
                    {"kind": "forum", "url": signal["author_url"], "source_url": signal["source_url"], "quote": signal["quote"], "profile": True}]})
    # ponytail: eighty candidates per run; Search again expands coverage with seen-lead exclusions.
    candidates = quality.limit_fits(candidates, journey.MAX_CANDIDATES)
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc["_runtime"].update(
        {candidate_key: candidates, candidate_key + "_complete": True}))
    candidates = journal.document(payload["journey_id"])["_runtime"][candidate_key]
    qualified = []
    for candidate in candidates:
        try:
            if context.get("target_type") == "person" or candidate.get("expert_role"):
                selected = [{"url": page["url"], "text": page["text"], "relevance": 0.0} for page in candidate["pages"] if page["text"].strip()]
                candidate["evidence_label"] = "Public source"
            elif candidate.get("quality_selected"):
                selected = [] if candidate.get("evidence_label") == "fit_only" else candidate["pages"]
            else:
                selected = await quality.select_passages(candidate["pages"], context, payload, identity=candidate["url"])
        except FatalError as error:
            log_founder_exception(error, payload["venture_id"], "gtm.reachable.rank")
            if str(error) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                raise
            trace(payload, candidate["url"], "dropped", "evidence_rank_budget" if str(error) == "gtm_call_budget_exhausted" else "evidence_rank_uncertain")
            continue
        except CreditError as error:
            log_founder_exception(error, payload["venture_id"], "gtm.reachable.rank")
            trace(payload, candidate["url"], "dropped", "evidence_rank_credits_exhausted" if isinstance(error, InsufficientCredits) else "evidence_rank_failed")
            continue
        except Exception as error:
            if context.get("target_type") == "person":
                raise
            log_founder_exception(error, payload["venture_id"], "gtm.reachable.rank")
            trace(payload, candidate["url"], "dropped", "evidence_rank_failed", error=type(error).__name__)
            continue
        if not selected:
            selected = quality.fit_passages(candidate["pages"], context, candidate.get("market_role"), candidate["routes"], identity=candidate["url"]) if candidate["kind"] == "business" and not candidate.get("expert_role") else []
            if not selected and _profile_can_fit_without_need(candidate, context):
                # A public profile rarely mentions the problem; the judge still has to place them in the audience.
                selected = [{"url": page["url"], "text": page["text"], "relevance": 0.0} for page in candidate["pages"] if page["text"].strip()]
            if not selected:
                trace(payload, candidate["url"], "dropped", "no_relevant_need_passage")
                continue
            candidate["evidence_label"] = "fit_only"
        elif candidate.get("evidence_label") not in {"fit_only", "Public source"}:
            candidate["evidence_label"] = selected[0].get("evidence_label", "need_passage")
        if candidate.get("signal_pages"):
            signal = next((signal for page in selected if (signal := journey._qualify_signal(
                {"url": candidate["url"], "kind": "customer_voice", "signal_pages": candidate["signal_pages"]},
                candidate["text"], {}, context["problem"], context["audience"], journey.utcnow(),
                judged=page | {"quote": page["text"]}, context=context)) and signal.get("author_url")), None)
            if not signal or not signal.get("published_at"):
                continue
            source = next(page for page in candidate["signal_pages"] if page["author"] == signal["author"]
                and quality.grounded_quote(signal["quote"], page["text"]))
            selected = [page for page in selected if page.get("author") == source["author"]
                and quality.grounded_quote(page["text"], source["text"])]
            candidate.update(text=source["text"], author=signal["author"], author_url=signal["author_url"], signals=[signal])
            candidate["routes"] = [{"kind": "forum", "url": target, "source_url": candidate["url"], "quote": signal["quote"],
                **({"profile": True} if target == signal["author_url"] else {})}
                for target in (candidate["url"], signal["author_url"])]
        candidate.update(pages=selected, quality_selected=True)
        qualified.append(candidate)
    candidates = quality.limit_fits(qualified, journey.MAX_CANDIDATES)
    leads = []
    doc = journal.document(payload["journey_id"])
    published = {lead["id"] for field in ("reachable_leads", "industry_experts") for lead in doc.get(field, [])
        if lead.get("judging_status") == "judged" and lead.get("routes")}
    query_count = len(doc.get("discovery_plan") or []) + len(doc.get("expert_discovery_plan") or [])
    async def publish_ready(*, final=False):
        unique = quality.dedupe(sorted({lead["id"]: lead for lead in leads}.values(), key=rank))
        if context.get("target_type") == "person":
            unique = prefer_exact_roles(unique)[:journey._keep_limit(query_count, context.get("requested_count"))]
        kept = []
        for expert in (False, True):
            kept.extend(quality.limit_fits([lead for lead in unique if lead.get("judging_status") != "pending"
                and bool(lead.get("expert_role") or lead.get("lead_type") == "insight") == expert], journey._keep_limit(query_count, context.get("requested_count"))))
        unpublished = [lead for lead in kept if lead["id"] not in published and lead.get("routes")]
        while len(unpublished) >= PUBLISH_BATCH_SIZE or final and unpublished:
            batch, unpublished = unpublished[:PUBLISH_BATCH_SIZE], unpublished[PUBLISH_BATCH_SIZE:]
            ingested = _publish(journal, payload, batch)
            published.update(lead["id"] for lead in batch)
            if ingested and context["mode"] == "B2B" and context.get("target_type") != "person" and len(batch) == PUBLISH_BATCH_SIZE:
                await asyncio.to_thread(journey._draft_ready, payload["workspace_id"], payload["venture_id"])
        return kept + [lead for lead in unique if lead.get("judging_status") == "pending"]
    pending = [lead for candidate in candidates if (lead := _pending(candidate, context))]
    _publish(journal, payload, pending)
    batches = math.ceil(len(candidates) / 8)
    suffix = "-public" if fallback else ""
    prefix = journey._op_prefix(runtime)
    batch_offset = 0
    if context.get("_candidate_round"):
        offset_key = candidate_key + "_judge_offset"
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc["_runtime"].setdefault(
            offset_key, doc["_runtime"]["calls_used"].get(prefix + "-reachjudge", 0)))
        batch_offset = journal.document(payload["journey_id"])["_runtime"][offset_key]
    journey._progress(payload, "judging", [f"{prefix}-reachjudge-{index + batch_offset}{suffix}" for index in range(batches)])
    gate = asyncio.Semaphore(enrich.CONCURRENCY)
    company_routes = {candidate["url"]: [route for route in candidate["routes"] if route.get("company_contact")
        or route["kind"] in {"phone", "booking"}] for candidate in candidates
        if organisation_contact and candidate["kind"] == "person" and candidate.get("contact_pages") and enrich.classify(candidate["url"]) == "company"}
    async def judge(start):
        batch = [{"id": index, "url": candidate["url"], "title": candidate["title"], "kind": candidate["kind"],
            "text": candidate["text"][:12000], "expert_role": candidate.get("expert_role"), "evidence_label": candidate["evidence_label"], "evidence_pages": candidate["pages"],
            **({"company_contact_available": True} if company_routes.get(candidate["url"]) else {}),
            **({"search_result_only": True} if candidate.get("search_result_only") else {})} for index, candidate in enumerate(candidates[start:start + 8], start)]
        async with gate:
            try:
                targeting = context | {"target_type": None} if generic_experts and any(item.get("expert_role") for item in batch) else context
                judged = await journey._judge_batch(payload, "reachjudge", targeting | {"candidates": batch, "_batch": start // 8 + batch_offset, "_batches": batches + batch_offset})
                return start, batch, judged
            except Exception as error:
                return start, batch, error
    tasks = [asyncio.create_task(judge(start)) for start in range(0, len(candidates), 8)]
    async for start, batch, response in _finished(tasks):
        try:
            if isinstance(response, Exception):
                raise response
            judged = [Judgement.model_validate(item) for item in response]
        except CreditError as error:
            if context.get("target_type") == "person":
                raise
            log_founder_exception(error, payload["venture_id"], "gtm.reachable.judge")
            for candidate in candidates[start:start + 8]:
                trace(payload, candidate["url"], "dropped", "judge_credits_exhausted" if isinstance(error, InsufficientCredits) else "judge_failed")
            leads.extend(lead for candidate in candidates[start:start + 8] if (lead := _pending(candidate, context)))
            continue
        except FatalError as error:
            if context.get("target_type") == "person":
                raise
            if str(error).startswith("gtm_") and str(error) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                raise
            for candidate in candidates[start:start + 8]:
                trace(payload, candidate["url"], "dropped", "judge_failed", error=str(error))
            leads.extend(lead for candidate in candidates[start:start + 8] if (lead := _pending(candidate, context)))
            continue
        except Exception as error:
            if context.get("target_type") == "person":
                raise
            log_founder_exception(error, payload["venture_id"], "gtm.reachable.judge")
            for candidate in candidates[start:start + 8]:
                trace(payload, candidate["url"], "dropped", "judge_failed", error=type(error).__name__)
            leads.extend(lead for candidate in candidates[start:start + 8] if (lead := _pending(candidate, context)))
            continue
        batch_ids = {hashlib.sha256(candidate["url"].encode()).hexdigest()[:24] for candidate in candidates[start:start + 8]}
        def clear_pending(row, doc, session):
            unpublished_ids = {lead["id"] for lead in ready if lead["id"] not in published}
            for field in ("reachable_leads", "industry_experts"):
                doc[field] = [lead for lead in doc.get(field, []) if lead["id"] not in batch_ids
                    or lead.get("judging_status") != "pending" or lead["id"] in unpublished_ids]
        missing = journal.document(payload["journey_id"])["_runtime"].get("reachjudge_missing", {}).get(str(start // 8 + batch_offset) + suffix, [])
        leads.extend(lead for index in missing if start <= index < start + len(batch) and (lead := _pending(candidates[index], context)))
        for item in judged:
            if start <= item.id < start + len(batch):
                trace(payload, candidates[item.id]["url"], "judged", fit=item.fit, why=item.why, value_exchange=item.value_exchange)
            if not start <= item.id < start + len(batch) or item.fit < 70 or not item.why:
                if start <= item.id < start + len(batch):
                    trace(payload, candidates[item.id]["url"], "dropped", "low_fit" if item.fit < 70 else "missing_relevance")
                continue
            candidate = candidates[item.id]
            if journey._same_offer(candidate["url"], item.company or item.name or candidate.get("title", ""), candidate["text"], context["competitors"], context["offer"], context["problem"], expert_role=candidate.get("expert_role")):
                trace(payload, candidate["url"], "dropped", "competitor_or_same_offer")
                continue
            if not item.name and candidate.get("profile_facts", {}).get("name"):
                item.name = candidate["profile_facts"]["name"]
            if re.search(r"\bindependent\b|\b1 to 3 dentists\b", context["audience"], re.I) and _CHAINS.search(candidate["text"]):
                trace(payload, candidate["url"], "dropped", "chain_not_independent")
                continue
            if candidate["kind"] == "person" and re.search(r"\b(?:intern|aspiring|seeking|open to work|job board|recruiter|staffing agency)\b", candidate["text"], re.I):
                trace(payload, candidate["url"], "dropped", "non_customer_profile")
                continue
            page = next((page for page in candidate["pages"] if reach_url(page["url"]) == reach_url(item.url)
                and quality.homepage_quote(item.quote, page["text"])), None)
            quote = quality.homepage_quote(item.quote, page["text"]) if page else None
            named_expert = is_local() and candidate.get("expert_role") and candidate["kind"] == "business" and item.kind == "person"
            company_fallback = item.kind == "business" and bool(company_routes.get(candidate["url"]))
            wrong_kind = item.kind != candidate["kind"] and not named_expert and not company_fallback
            if wrong_kind or not quote or company_fallback and (not item.name or not journey._verbatim(item.name, quote)):
                trace(payload, candidate["url"], "dropped", "kind_mismatch" if wrong_kind else "unpublished_quote_or_source")
                continue
            item.quote = quote
            if item.kind == "person":
                text = candidate["text"]
                if journey._person_audience(context) and journey._consumer_thread_url(candidate["url"]) and not quality.grounded_quote(item.quote, text):
                    text += "\n\n" + item.quote
                if candidate.get("author") and not candidate.get("signals"):
                    signal = journey._qualify_signal({"url": candidate["url"], "kind": "customer_voice", "author": candidate["author"],
                        "created_utc": candidate.get("created_utc")}, text, candidate.get("metadata") or {}, context["problem"], context["audience"],
                        journey.utcnow(), judged=page | {"quote": item.quote}, context=context)
                    if not signal or not signal.get("published_at"):
                        trace(payload, candidate["url"], "dropped", "no_fresh_attributed_need")
                        continue
                    candidate["signals"] = [signal]
                if journey._same_offer(candidate["url"], item.company or item.name, text, context["competitors"], context["offer"], context["problem"], expert_role=candidate.get("expert_role")):
                    trace(payload, candidate["url"], "dropped", "profile_competitor_or_same_offer")
                    continue
                if re.search(r"\b(?:jobs? board|creative vacancies|debt collection agency|marketplace|aggregator)\b", text, re.I):
                    trace(payload, candidate["url"], "dropped", "profile_vendor_or_aggregator")
                    continue
                if candidate["routes"][0]["kind"] == "x":
                    individual = bool(re.search(r"\b(?:freelance|founder|owner|manager|coordinator|coach|educator|instructor|YouTuber|engineer|practitioner|operator|author|researcher)\b|\bI (?:am|run|manage|need|teach|work|have|struggle|cannot|can't)\b|\bmy (?:clients|practice|invoices|child|children)\b", text, re.I))
                    partner = bool(item.partner_quote and journey._verbatim(item.partner_quote, page["text"]) and re.search(
                        r"\b(?:teach\w*|training|coaching|program\w*|collaborat\w*|partner\w*|wholesale|classes|camp)\b", item.partner_quote, re.I))
                    if not individual and not partner:
                        trace(payload, candidate["url"], "dropped", "brand_without_published_partner_path")
                        continue
            if item.name and not journey._verbatim(item.name, candidate["text"]):
                trace(payload, candidate["url"], "dropped", "unpublished_profile_fact", fields=["name"])
                continue
            for field in ("role", "company", "location"):
                value = getattr(item, field)
                if not value and candidate.get("profile_facts", {}).get(field):
                    value = candidate["profile_facts"][field]
                    setattr(item, field, value)
                if value and not journey._verbatim(value, candidate["text"]):
                    match = re.search(r"\s+".join(re.escape(word) for word in value.split()), candidate["text"], re.I)
                    setattr(item, field, match[0] if match else "")
                    trace(payload, candidate["url"], "fact_corrected" if match else "fact_omitted", field=field, reason="verbatim_spelling" if match else "unpublished_optional_fact")
            if not item.role:
                role = re.search(r"\b(?:freelance\s+(?:graphic|web|product)\s+designer|(?:senior\s+)?swimming coach|swim coach|dental office manager|front desk manager|office manager|creative director)\b", candidate["text"], re.I)
                if role:
                    item.role = role[0]
            if (context.get("target_type") == "person" and not company_fallback and not (generic_experts and candidate.get("expert_role")) or named_expert) and (not item.role or not playbooks.real_person({
                    "kind": item.kind, "judging_status": "judged", "name": item.name, "company": item.company})):
                trace(payload, candidate["url"], "dropped", "missing_public_person_or_role")
                continue
            role_match = "exact"
            if context.get("target_type") == "person" and context.get("requested_role") and not company_fallback:
                from app.research.source_quality import _stems
                # shortcut: matching title stems only, use structured equivalence if synonymous titles need exact-match labels.
                if not _stems(context["requested_role"]).issubset(_stems(item.role)):
                    senior = re.search(r"\b(?:chief|head|director|manager|lead|president|founder|owner|principal|CEO|CTO|COO|VP)\b", item.role, re.IGNORECASE)
                    linked = all(value and journey._verbatim(value, item.quote) for value in (item.name, item.role, item.company))
                    if not context.get("organisation_audience") or not senior or not linked:
                        trace(payload, candidate["url"], "dropped", "not_grounded_senior_role")
                        continue
                    role_match = "closest_senior"
            if item.kind == "person" and not item.value_exchange.strip() and not (organisation_contact and all(
                    value and journey._verbatim(value, quote) for value in (item.name, item.role, item.company))):
                trace(payload, candidate["url"], "dropped", "no_concrete_value_exchange")
                continue
            if item.kind == "person" and not candidate.get("author") and not item.name:
                trace(payload, candidate["url"], "dropped", "missing_person_name")
                continue
            if candidate.get("author") and item.name and item.name != candidate["author"]:
                trace(payload, candidate["url"], "dropped", "author_mismatch")
                continue
            if journey._same_offer(candidate["url"], item.company or item.name, candidate["text"], context["competitors"], context["offer"], context["problem"], expert_role=candidate.get("expert_role")):
                trace(payload, candidate["url"], "dropped", "judged_competitor_or_same_offer")
                continue
            if not item.name and not candidate.get("author"):
                trace(payload, candidate["url"], "dropped", "missing_name")
                continue
            decision_roles = r"\b(?:founder|owner|president|principal|partner|CEO|COO|director|manager|head)\b"
            for route in candidate["routes"]:
                if route["kind"] == "x":
                    route.update(source_url=page["url"], quote=item.quote)
                person = next((person for person in candidate.get("people", []) if person.get("email") == route.get("email") or person["name"] == route.get("name")), None)
                if route.get("email_kind") == "named_person":
                    route["decision_maker"] = bool(person and re.search(decision_roles, person["role"], re.I))
            person = next((person for person in candidate.get("people", []) if person["name"] == next((route.get("name") for route in candidate["routes"] if route.get("route_type") in {"guess", "pattern"}), None)), None)
            person = person or next((person for person in candidate.get("people", []) if person.get("email") and re.search(decision_roles, person["role"], re.I)), None)
            if not person:
                # ponytail: no-email names require an explicit team page; verify About-page affiliation before expanding this.
                linked_people = {route.get("person") for route in candidate["routes"]}
                person = next((person for person in candidate.get("people", []) if person.get("email") or person["name"] in linked_people or re.search(
                    r"/(?:team|people|leadership)(?:/|$)", urlsplit(person["source_url"]).path, re.I)), None)
            if company_fallback or context.get("_company_contact_fallback"):
                person = None
            lead = {"id": hashlib.sha256(candidate["url"].encode()).hexdigest()[:24], "name": item.name or candidate["author"], "kind": item.kind,
                "company": item.company or (item.name if item.kind == "business" else ""), "role": item.role if item.kind == "person" else "",
                "location": item.location, "fit": item.fit, "why": item.value_exchange if item.kind == "person" else item.why,
                "value_exchange": item.value_exchange or item.why, "handle": candidate.get("handle"), "profile_url": candidate.get("profile_url"),
                "quote": item.quote, "source_url": page["url"], "market_role": candidate.get("market_role"), "evidence_relevance": page["relevance"],
                "country": candidate.get("country") or (enrich._COUNTRY_NAMES[location[0].lower()] if (location := enrich._NAME_RE.search(item.location)) else
                    "GB" if re.search(r"\bUK\b|United Kingdom|London|Manchester", item.location, re.I) else ""), "registry": candidate.get("registry"), "corporate_name": candidate.get("corporate_name"), "company_number": candidate.get("company_number"),
                "routes": company_routes[candidate["url"]] if company_fallback else candidate["routes"], "person": person["name"] if person else None,
                "person_role": person["role"] if person else None, "author": candidate.get("author"),
                "people": playbooks.decision_makers(candidate.get("people", []), item.company or (item.name if item.kind == "business" else "")),
                "related_profiles": [profile_key(url) for url in candidate.get("profile_urls", [])],
                "draft": "", "connection_note": "", "x_note": "", "expert_role": candidate.get("expert_role"), "lead_type": "insight" if candidate.get("expert_role") else item.lead_type, "judging_status": "judged", "signals": candidate.get("signals", [])}
            lead["evidence_label"] = candidate["evidence_label"]
            if company_fallback or context.get("_company_contact_fallback"):
                lead["company_contact_fallback"] = True
            if context.get("target_type") == "person" and not company_fallback:
                lead["role_match"] = role_match
            if lead["evidence_label"] == "Public source":
                lead.update(why=f'You can ask how this relates to your research. Your source says: "{item.quote}"',
                    value_exchange="You can ask about their published experience and share what you learn.")
            if lead["evidence_label"] == "fit_only" and lead["kind"] != "person":
                reason = enrich.fit_reason(lead["quote"], lead["source_url"])
                lead.update(why=reason, value_exchange=reason)
            if role_match == "closest_senior":
                from app.gtm.discovery import normalise_note
                lead["why"] = normalise_note(f'Closest senior role found at {item.company}, not an exact match for {context["requested_role"]}. ' + lead["why"])
            # ponytail: template when model drafting fails; regenerate a personalised note on the next discovery run.
            _call_draft(lead, context["problem"])
            lead["routes"].sort(key=route_priority)
            leads.append(lead)
            await publish_ready()
        ready = await publish_ready(final=is_local())
        journal.mutate(payload["journey_id"], payload, clear_pending)
        route_only = [lead for lead in ready if lead["id"] not in published and lead.get("judging_status") == "judged" and not contact_seed(lead)]
        if route_only:
            _publish(journal, payload, route_only)
            published.update(lead["id"] for lead in route_only)
    leads = await publish_ready(final=True)
    if any(lead.get("judging_status") == "pending" for lead in leads):
        journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
            discovery_notice="Some public matches could not be checked. Checked contacts are kept; the others are labelled for your review. Open their evidence and ask about their experience, or refine your audience and start a new search. No unconfirmed call is repeated."))
    person_search = context.get("target_type") == "person"
    if (context["mode"] == "B2B" or person_search) and provider and enrich.enabled() and (not person_search or getattr(provider, "name", "") == "firecrawl"):
        looked_up = 0
        for lead in leads:
            if lead.get("judging_status") == "pending" or person_search and published_route(lead):
                continue
            if not person_search and (lead["kind"] == "business" or lead["routes"][0]["kind"] not in {"linkedin", "x"}):
                continue
            candidate = next(candidate for candidate in candidates if profile_key(candidate["url"]) == profile_key(lead["source_url"]))
            if candidate.get("search_result_only"):
                continue
            text = candidate["text"]
            site = next((match[1] for match in re.finditer(r"\b(?:https?://)?(?:www\.)?([a-zA-Z0-9-]+\.(?:co\.uk|com|uk|net|org|design|studio))\b", text)
                if journey._signal_url("https://" + match[1]) and enrich.classify("https://" + match[1]) == "company"), "")
            business = lead["company"] or candidate.get("profile_facts", {}).get("company") or ""
            if not site and not business and not re.search(r"\b(?:freelance|self.employed|sole trader)\b", text, re.I):
                continue
            if any(profile_key(lead["source_url"]) in other.get("related_profiles", []) for other in leads if other is not lead):
                continue
            if not person_search and looked_up >= 6:
                break
            looked_up += 1
            query = f'"{lead["name"]}" {site or business} {"contact team" if person_search else "portfolio contact"} -site:linkedin.com -site:x.com -site:twitter.com -jobs'
            extra_reads = 0
            try:
                if person_search and candidate.get("contact_pages") and enrich.classify(lead["source_url"]) == "company":
                    results = [{"url": lead["source_url"], "title": business, "snippet": text}]
                else:
                    results = await journey._signal_call(payload, "person_site_search", query,
                        lambda: provider.search(query, include_content=False, exclude_domains=sorted(enrich._DIRECTORIES | {"linkedin.com", "x.com", "twitter.com"})), meter="firecrawl_search")
                for result in results or []:
                    if person_search and extra_reads >= enrich.MAX_PAGES - 1:
                        break
                    url = journey._signal_url(str(result.get("url") or ""))
                    trace(payload, str(result.get("url") or ""), "search_result", track="portfolio", person=lead["name"])
                    snippet = str(result.get("title") or "") + "\n" + str(result.get("snippet") or result.get("description") or "")
                    if not url or enrich.classify(url, snippet) != "company" or not journey._verbatim(lead["name"].casefold(), snippet.casefold()):
                        continue
                    host = enrich._host(url)
                    counts = journey._journal(payload).document(payload["journey_id"])["_runtime"]["calls_used"]
                    if host not in used_domains and len(used_domains) >= 15:
                        break
                    if counts.get("contact:" + host, 0) >= runtime["ceilings"]["contact_pages"]:
                        continue
                    used_domains.add(host)
                    trace(payload, url, "candidate", track="portfolio", person=lead["name"])
                    portfolio_pages = [tuple(page) for page in candidate.get("contact_pages", []) if enrich._host(page[0]) == host] if person_search else []
                    async def scrape(page):
                        nonlocal extra_reads
                        if person_search:
                            if extra_reads >= enrich.MAX_PAGES - 1:
                                raise FatalError("gtm_call_budget_exhausted")
                            extra_reads += 1
                        value = await journey._signal_call(payload, "contact:" + host, page,
                            lambda: provider.scrape(page, main_only=False), meter="firecrawl_scrape", cache_identity={"main_only": False}) or ""
                        trace(payload, url, "fetched", track="portfolio", page=page, chars=len(value))
                        return value
                    async def scrape_main(page):
                        return await journey._signal_call(payload, "contact:" + host, page + "#quality-main-v1",
                            lambda: provider.scrape(page), meter="firecrawl_scrape",
                            cache_identity={"main_only": True, "excludeTags": quality.EXCLUDE_TAGS}) or ""
                    home = urljoin(url, "/")
                    if person_search and portfolio_pages and home != portfolio_pages[0][0]:
                        portfolio_pages.insert(0, (home, await scrape(home)))
                    facts = await enrich.enrich_site(provider, url, payload["workspace_id"], payload["venture_id"], pages=portfolio_pages, scrape=scrape, published_mailboxes=not person_search,
                        context=None if person_search else context, payload=payload, main_scrape=scrape_main)
                    if facts.get("quality_rejected"):
                        continue
                    own_text = facts["company_text"]
                    linked = profile_key(lead["source_url"]) in {profile_key(link) for link in facts["profile_urls"]}
                    named = journey._verbatim(lead["name"].casefold(), own_text.casefold())
                    identity = "\n".join(enrich._clean(heading)[0] for page, raw in portfolio_pages for heading in re.findall(r"<(?:title|h[1-3])\b[^>]*>([\s\S]*?)</(?:title|h[1-3])>", raw, re.I))
                    owned = journey._verbatim(lead["name"].casefold(), identity.casefold()) or (facts.get("name") or "").casefold() == lead["name"].casefold() or any(person["name"].casefold() == lead["name"].casefold() for person in facts["people"])
                    affiliated = business and journey._verbatim(business.casefold(), own_text.casefold()) or re.search(r"\b(?:freelance|self.employed|sole trader)\b", text, re.I) and re.search(r"\b(?:design|designer|freelance|portfolio)\b", own_text, re.I)
                    verified = linked or named and owned and affiliated
                    person = next((person for person in facts["people"] if person["name"].casefold() == lead["name"].casefold()), None)
                    if person_search:
                        identity = [re.split(r"\s+[|:-]\s+", " ".join(enrich._clean(heading)[0].split()), maxsplit=1)[0].casefold()
                            for page, raw in portfolio_pages if page == home
                            for heading in re.findall(r"<(?:title|h1)\b[^>]*>([\s\S]*?)</(?:title|h1)>", raw, re.I)]
                        employer_name = " ".join(business.split()).casefold()
                        home_name = enrich.extract(host, [page for page in portfolio_pages if page[0] == home])["name"] or ""
                        employer_owned = employer_name and (employer_name in identity or " ".join(home_name.split()).casefold() == employer_name)
                        own_affiliation = enrich._host(lead["source_url"]) == host and business and journey._verbatim(business, text) and not re.search(
                            r"\b(?:testimonials?|reviews?|clients?|customers?)\b", text[:text.find(lead["name"])], re.I)
                        verified = employer_owned and (own_affiliation or person and (person.get("staff_verified") or person.get("email")))
                        if verified and own_affiliation:
                            if person is None:
                                person = {"name": lead["name"], "role": lead["role"], "source_url": lead["source_url"], "quote": lead["quote"]}
                                facts["people"].append(person)
                            person["staff_verified"] = True
                        facts["country"] = lead["country"] if lead["country"] in {"DE", "AT"} else facts.get("country") or lead["country"]
                    if not verified or journey._same_offer(url, result.get("title") or "", own_text, context["competitors"], context["offer"], context["problem"], expert_role=lead.get("expert_role")):
                        trace(payload, lead["source_url"], "portfolio_rejected", "unverified_own_site", site=url)
                        break
                    routes = contact_routes(facts, host=host)
                    if person_search:
                        routes = [route for route in routes if route["kind"] != "email" or route["email_kind"] == "business_role" or person and route["email"] == person.get("email")]
                        facts["people"] = [target for target in facts["people"] if target is person or target.get("email")]
                    if context["mode"] == "B2B":
                        routes.extend(await asyncio.to_thread(enrich.guessed_routes, facts, host, payload["workspace_id"], b2b=True))
                    lead["routes"].extend(route for route in routes if route not in lead["routes"])
                    lead["routes"].sort(key=route_priority)
                    lead["business_url"] = url
                    lead["country"] = (lead["country"] or facts.get("country")) if person_search else facts.get("country") or lead["country"]
                    if person_search and person:
                        lead.update(person=person["name"], person_role=person["role"])
                    trace(payload, lead["source_url"], "contact_extracted", site=url, emails=len(facts["contacts"]), form=bool(facts["contact_form"]))
                    break
            except Exception as error:
                if person_search and isinstance(error, (CreditError, FirecrawlLimiterUnavailable)):
                    raise
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.portfolio")
                if isinstance(error, FatalError) and str(error).startswith("gtm_") and str(error) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                    raise
                trace(payload, lead["source_url"], "portfolio_failed", type(error).__name__)
        leads.sort(key=rank)
    if person_search:
        save_source_yield(payload)
        if person_jobs and len(unread_people) == len(person_jobs) and not leads:
            raise RuntimeError("people_page_unavailable")
        leads = await publish_ready(final=True)
        _publish(journal, payload, leads, final=True)
        save_source_yield(payload)
        await asyncio.to_thread(record_judged_live_leads, payload["workspace_id"], payload["venture_id"], leads)
        return leads
    from app.signals.research import CompaniesHouseProvider
    for lead in leads:
        corporate = None
        if lead.get("judging_status") != "pending" and lead["country"] == "GB" and lead.get("corporate_name"):
            try:
                matches = await journey._signal_call(payload, "registry_lookup", lead["corporate_name"], lambda: CompaniesHouseProvider().search(lead["corporate_name"]))
                corporate = next((match for match in matches or [] if lead.get("company_number") and match["company_number"] == lead["company_number"]), None)
            except Exception as error:
                log_founder_exception(error, payload["venture_id"], "gtm.reachable.registry")
                trace(payload, lead["source_url"], "registry_unverified", type(error).__name__)
        lead["outreach_hint"] = outreach_hint(lead["country"], corporate, business_name=" ".join(str(lead.get(key) or "") for key in ("name", "company", "corporate_name")), company_number=lead.get("company_number") or "")
    # ponytail: merge only verified own-site/profile affiliation; brand aliases need verified owner affiliation.
    for lead in list(leads):
        if lead.get("judging_status") == "pending":
            continue
        if not any(route["kind"] in {"linkedin", "x"} and profile_key(route["url"]) == profile_key(lead["source_url"]) for route in lead["routes"]):
            continue
        business = next((business for business in leads if business is not lead and business.get("business_url") is None and (
            lead.get("business_url") and enrich._host(lead["business_url"]) == enrich._host(business["source_url"])
            or profile_key(lead["source_url"]) in business.get("related_profiles", [])
                and (re.sub(r"\W", "", lead["name"].casefold()) in re.sub(r"\W", "", business["name"].casefold())
                    or re.sub(r"\W", "", business["name"].casefold()) in re.sub(r"\W", "", lead["name"].casefold())))), None)
        if business:
            business["routes"].extend(route for route in lead["routes"] if route not in business["routes"])
            business.update(person=lead["name"], person_role=lead["role"])
            trace(payload, lead["source_url"], "merged", "published_business_profile", business_url=business["source_url"])
            leads.remove(lead)
    for lead in leads:
        if lead["routes"][0]["kind"] in {"linkedin", "x"} and lead["company"]:
            business = next((business for business in leads if business is not lead and business["routes"][0]["kind"] in {"email", "contact_form", "phone"}
                and business["company"].casefold() == lead["company"].casefold()), None)
            if business:
                lead["works_at"] = business["id"]
    leads = quality.dedupe(sorted(leads, key=rank))
    journey._progress(payload, "drafting", [f"{prefix}-reachdraft-{index}{suffix}" for index in range(math.ceil(len(leads) / 8))])
    async def draft(start):
        # ponytail: possible fits use question-only templates; validate fit questions before allowing model personalisation.
        batch = [{"id": index, "url": lead["source_url"], "text": lead.get("display_quote") or enrich.contact_excerpt(lead["quote"]), "name": lead["name"],
            "recipient_name": recipient_name(lead), "evidence_label": lead.get("evidence_label"),
            "role": lead.get("person_role") or lead["role"], "why": lead["why"], "outreach_hint": lead["outreach_hint"], "signals": lead.get("signals", [])}
            for index, lead in enumerate(leads[start:start + 8], start) if lead.get("judging_status") != "pending" and lead.get("evidence_label") != "possible_fit"]
        if not batch:
            return
        try:
            drafted = ReachDrafts.model_validate(await journey._discovery_model(payload, "reachdraft", context | {"candidates": batch, "_batch": start // 8})).items
        except CreditError as error:
            log_founder_exception(error, payload["venture_id"], "gtm.reachable.draft")
            for lead in leads[start:start + 8]:
                lead["draft_status"] = "credits_required" if isinstance(error, InsufficientCredits) else "draft_failed"
            return
        except FatalError as error:
            if str(error).startswith("gtm_") and str(error) not in {"gtm_call_budget_exhausted", "gtm_outcome_uncertain"}:
                raise
            code = str(error)
            for lead in leads[start:start + 8]:
                lead["draft_status"] = "outcome_uncertain" if "uncertain" in code or "settlement" in code else "provider_unavailable" if "provider_offline" in code or "byok_unavailable" in code else "draft_failed"
            log_founder_exception(error, payload["venture_id"], "gtm.reachable.draft")
            return
        except Exception as error:
            from app.llm.founder_provider import FounderLLMConfigError
            from llm_gateway.errors import ProviderCallError

            log_founder_exception(error, payload["venture_id"], "gtm.reachable.draft")
            for lead in leads[start:start + 8]:
                lead["draft_status"] = "model_failed" if isinstance(error, (ProviderCallError, FounderLLMConfigError)) else "draft_failed"
            return
        for candidate in batch:
            leads[candidate["id"]]["draft_status"] = "output_rejected"
        for item in drafted:
            if item.id in {candidate["id"] for candidate in batch}:
                lead = leads[item.id]
                greetings = [re.match(r"(?:Hi|Hello|Hey|Dear)\s+([^,!:]+)[,!:]", text, re.I) for text in (item.body, item.connection_note)]
                recipient = lead.get("person") or lead["name"]
                if any(match and not journey._verbatim(re.sub(r"\s+team$", "", match[1], flags=re.I).casefold(), recipient.casefold()) for match in greetings):
                    trace(payload, lead["source_url"], "draft_rejected", "wrong_recipient")
                    continue
                body, note = normalise_draft(item.body, lead), normalise_draft(item.connection_note, lead)
                leads[item.id].update(draft=body, connection_note=note, x_note=note[:280], draft_source="model", draft_status="personalised")
        _publish(journal, payload, leads[start:start + 8])
    await journey._parallel([lambda start=start: draft(start) for start in range(0, len(leads), 8)])
    _publish(journal, payload, leads, final=True)
    save_source_yield(payload)
    try:
        record_judged_live_leads(payload.get("workspace_id"), payload.get("venture_id"), [lead for lead in leads if lead.get("judging_status") == "judged"])
    except Exception as error:  # recording must never cost the founder their results
        log_founder_exception(error, payload.get("venture_id"), "gtm.reachable.record")
    return [lead for lead in leads if lead["draft"] and lead["connection_note"]]
