"""Compile prior attempts from persisted evidence. Model hints never become facts."""
from __future__ import annotations

import re
from html import unescape
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.research.pricing import pricing_evidence_quote
from app.research.presentation import clean_excerpt
from app.research.selection import quote_options
from core.clock import utcnow

_HOSTS = {"github.com", "news.ycombinator.com", "medium.com", "notion.site", "substack.com", "apps.apple.com", "techcrunch.com", "failory.com", "cbinsights.com", "indiehackers.com", "producthunt.com", "reddit.com", "youtube.com", "g2.com", "capterra.com"}
# ponytail: known publisher domains only, add observed hosts or a maintained classifier as coverage grows.
_HOSTS |= {"cnn.com", "bbc.com", "bbc.co.uk", "reuters.com", "apnews.com", "nytimes.com", "washingtonpost.com", "theguardian.com", "wsj.com", "bloomberg.com", "forbes.com", "fortune.com", "businessinsider.com", "cnbc.com", "nbcnews.com", "abcnews.go.com", "cbsnews.com", "foxnews.com", "npr.org", "huffpost.com", "time.com", "newsweek.com", "usatoday.com", "yahoo.com", "msn.com", "wikipedia.org", "wikihow.com", "quora.com", "pinterest.com", "instagram.com", "facebook.com", "tiktok.com", "wordpress.com", "blogspot.com", "wixsite.com", "squarespace.com"}
_TENANT_HOSTS = {"github.io", "netlify.app", "vercel.app", "pages.dev", "web.app", "firebaseapp.com", "supymem.com"}
_CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:\+?\d[\s().-]*){8,}", re.I)
_COMPLAINT = re.compile(r"\b(?:bug|broken|frustrat\w*|struggl\w*|cannot|can't|unreliable|expensive|failed|failure|workaround)\b", re.I)
_CLOSURE = re.compile(r"\b(?:we|our (?:company|startup|business)|the company)\s+(?:have\s+)?(?:shut down|closed|ceased operations)\s+(?:because|due to)\b", re.I)


def plain_text(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)|!\[[^\]]*\]\[[^\]]*\]", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^\n]*?\)|\[([^\]]*)\]\[[^\]]*\]", lambda match: match[1] or match[2] or "", text)
    text = re.sub(r"(?m)^\s*(?:\[[^\]]+\]:\s*\S+.*|(?:[-+*•]|\d+[.)]|>+)\s+)", " ", text)
    text = re.sub(r"(?<!\S)(?:[-*•]|\d+[.)])\s+", " ", text)
    text = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>|https?://\S+", " ", text)
    text = re.sub(r"[*_`#~]+", "", text)
    return " ".join(text.split())


def product_description(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", " ", text)
    def link_text(match):
        label, url = match.groups()
        return " " if re.search(r"#(?:main|content)\b|/(?:cart|login|account)\b", url, re.I) or re.search(
            r"^(?:skip\b|home$|menu$|sign (?:in|up)$|log in$|contact(?: us)?$|about(?: us)?$|shop(?: now)?$|privacy|terms|cookie|free bag|.*join today)",
            label.strip(" *"), re.I) else label
    text = re.sub(r"\[([^\]]*)\]\(([^\n]*?)\)", link_text, text)
    text = re.sub(r"<(?:nav|header|footer|script|style)\b[^>]*>.*?</(?:nav|header|footer|script|style)>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>|https?://\S+|[*_`#]+", " ", text)
    text = re.sub(r"\bskip to (?:main )?content\b|\b\d(?:\.\d)?\s*[★☆][^.]*?\b(?:reviews?|ratings?)\b", " ", text, flags=re.I)
    # ponytail: bounded boilerplate heuristics, use structured page extraction if new templates defeat them.
    lines = [line for line in unescape(text).splitlines() if not re.match(
        r"^\s*(?:home|menu|sign in|log in|privacy policy|terms of service|cookie settings|all rights reserved)\s*$", line, re.I)]
    clean = plain_text("\n".join(lines))
    clean = " ".join(re.split(r"(?<=[.!?])\s+", clean)[:2])
    if len(clean) > 280:
        clean = clean[:277].rsplit(" ", 1)[0].rstrip("., ") + "…"
    return clean


def _contains_contact(text: str) -> bool:
    # A validated ISO source date is not a phone number. Keep the original quote
    # unchanged; this normalization is only the contact-admission check.
    def known_date(match):
        return "<source date>" if _date(match.group()) else match.group()
    return bool(_CONTACT.search(re.sub(r"\b\d{4}-\d{2}-\d{2}\b", known_date, text)))


def entity_key(url: str) -> str | None:
    from app.research.deep import safe_source_url
    safe = safe_source_url(url)
    if not safe:
        return None
    parsed = urlsplit(safe)
    host = (parsed.hostname or "").removeprefix("www.")
    if any(host.endswith("." + parent) for parent in _TENANT_HOSTS):
        return host  # Distinct hosted tenants must never merge into the platform.
    if host in _HOSTS or any(host.endswith("." + parent) for parent in _HOSTS):
        path = parsed.path.rstrip("/")
        if host == "github.com" and len(path.strip("/").split("/")) == 2:
            return host + path.lower()
        return None  # Shared publishing/forum hosts never masquerade as companies.
    # Keep distinct hosts unless an observed canonical company URL associates
    # them. Suffix guesses can merge unrelated regional businesses and facts.
    return host


def _date(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.year < 1970 or parsed > utcnow().replace(tzinfo=timezone.utc) + timedelta(days=1):
            return None
        return parsed.date().isoformat()
    except ValueError:
        return None


def _meta(source: dict) -> dict:
    evidence = source.get("evidence") or {}
    return evidence.get("prior_art") or source.get("prior_art") or {}


def _name(source: dict, key: str, seeds: list[dict]) -> str:
    meta = _meta(source)
    title = str(source.get("title") or "")
    snippets = " ".join(quote_options([source]).get(source["id"], []))
    name = meta.get("company_name")
    if isinstance(name, str) and name.strip() and not _CONTACT.search(name):
        return name[:120]
    for seed in seeds:
        hint = str(seed.get("name") or "")
        domain = str(seed.get("domain") or "")
        if not domain or entity_key(domain if "://" in domain else "https://" + domain) != key:
            continue
        match = re.search(r"(?<!\w)" + re.escape(hint) + r"(?!\w)", title + " " + snippets, re.I) if hint else None
        if match and not _CONTACT.search(match.group()):
            return match.group()[:120]  # Actual source span, not the model's spelling.
    repo = (source.get("evidence") or {}).get("repository") or {}
    if repo.get("full_name"):
        return str(repo["full_name"])[:120]
    return key  # An observed domain is honest when no company name was confirmed.


def compile_prior_art(sources: list[dict], review: dict | None, *, plan: dict | None = None,
                      gaps: list[str] | None = None, cost: dict | None = None,
                      include_unconfirmed: bool = False) -> dict:
    from app.research.deep import safe_source_url
    # Compilation does not send a model prompt. Review separately caps its
    # sources at 50; durable entity records can resolve every saved finding.
    options = {source["id"]: quote_options([source])[source["id"]] for source in sources}
    records: dict[str, dict] = {}
    source_keys = {}
    by_id = {source["id"]: source for source in sources}
    dates = []
    for source in sources:
        evidence = source.get("evidence") or {}
        if evidence.get("founder_flag") == "exclude" or evidence.get("evidence_kind") in {"name_source", "search_lead_unverified"}:
            continue
        meta = _meta(source)
        # An HN comment's company association is inherited from its fetched parent.
        homepage = safe_source_url(str(meta.get("homepage") or ""))
        website = homepage if homepage and entity_key(homepage) else safe_source_url(str(meta.get("company_url") or source.get("url") or ""))
        key = entity_key(website or "")
        if not key:
            continue
        if not meta and not any(check.get("source_id") == source["id"]
                                for check in (review or {}).get("competitor_checks", [])):
            continue  # Topic-related editorial pages are not competitor entities.
        source_keys[source["id"]] = key
        company = records.setdefault(key, {"key": key, "name": _name(source, key, (plan or {}).get("seed_competitors", [])),
            "website": website, "kind": "oss" if key.startswith("github.com/") or meta.get("source_kind") == "repository_metadata" else "direct" if meta.get("source_kind") == "yc_company" else "adjacent",
            "status": "unknown", "status_evidence": [], "finding_ids": [], "source_types": [],
            "what_they_built": None, "pricing_now": [], "complaints": [], "positioning": None, "traction_signals": [],
            "first_seen": None, "last_seen": None, "confidence": "low", "timeline": [], "why_failed": None})
        company["finding_ids"].append(source["id"])
        source_type = str(meta.get("source_kind") or evidence.get("evidence_kind") or "web")
        if source_type == "yc_company":
            company["name"] = _name(source, key, [])
        if source_type not in company["source_types"]:
            company["source_types"].append(source_type)
        published = _date(meta.get("date") or meta.get("published_at") or meta.get("created_at") or meta.get("last_activity_at")
                          or (evidence.get("repository") or {}).get("pushed_at"))
        if published:
            dates.append(published)
            company["first_seen"] = min(company["first_seen"] or published, published)
            company["last_seen"] = max(company["last_seen"] or published, published)
            company["timeline"].append({"finding_id": source["id"], "date": published,
                "event": "Public launch" if source_type == "hn_item" and meta.get("launch_type") in {"show_hn", "launch_hn"} else "Repository activity" if source_type == "repository_metadata" else "Source published"})
        status = str(meta.get("status") or "").lower()
        if source_type == "yc_company" and not meta.get("cache_stale") and status in {"live", "active", "inactive", "acquired", "public"}:
            company["status"] = {"active": "live", "public": "live"}.get(status, status)
            company["status_evidence"].append({"finding_id": source["id"], "basis": "yc_status"})
        elif evidence.get("page_verified") and company["status"] == "unknown":
            company["status"] = "live"
            company["status_evidence"].append({"finding_id": source["id"], "basis": "page_available"})
        elif (evidence.get("repository") or {}).get("archived") is True:
            company["status"] = "inactive"
            company["status_evidence"].append({"finding_id": source["id"], "basis": "repository_archived"})
        for field, label in (("points", "hn_points"), ("comments", "hn_comments"), ("stars", "github_stars")):
            value = meta.get(field, (evidence.get("repository") or {}).get(field))
            if type(value) is int and value >= 0:
                company["traction_signals"].append({"finding_id": source["id"], "type": label, "value": value})
        quote = next((value for value in options.get(source["id"], []) if not _contains_contact(value)), None)
        # A current first-party page can explicitly report closure even when
        # YC/archives are unavailable. Availability alone never says alive/dead.
        if source_type == "web" and evidence.get("page_verified") and entity_key(str(source.get("url") or "")) == key:
            closure = next((value for value in options.get(source["id"], [])
                            if (match := _CLOSURE.search(value)) and match.group().lower().startswith(("we ", "our "))
                            and not _contains_contact(value)), None)
            if closure:
                company["status"] = "inactive"
                company["status_evidence"].append({"finding_id": source["id"], "basis": "first_party_closure"})
                company["why_failed"] = {"finding_id": source["id"], "quote": closure}
        if quote and source_type in {"yc_company", "repository_metadata", "hn_item"} and not company["what_they_built"]:
            company["what_they_built"] = {"finding_id": source["id"], "quote": quote}
        if quote and source_type in {"hn_comment", "github_issue"} and _COMPLAINT.search(quote):
            company["complaints"].append({"finding_id": source["id"], "quote": quote,
                                        "source_type": source_type})
    for check in (review or {}).get("competitor_checks", []):
        sid = check.get("source_id")
        company = records.get(source_keys.get(sid))
        if not company:
            continue
        for field, target in (("product_quote", "what_they_built"), ("pricing_quote", "pricing_now"), ("positioning_quote", "positioning")):
            quote = check.get(field)
            if field == "pricing_quote":
                quote = pricing_evidence_quote(quote)
            if quote and not _contains_contact(quote) and any(clean_excerpt(quote) and clean_excerpt(quote) in text for text in options.get(sid, [])):
                entry = {"finding_id": sid, "quote": quote}
                if target != "pricing_now":
                    company[target] = entry
                    if target == "what_they_built":
                        company["kind"] = "direct"
                else:
                    company[target].append(entry)
    # Prioritize independent corroboration without inventing relevance from source count.
    companies = sorted((company for company in records.values() if include_unconfirmed or any(
        plain_text(citation.get("quote") or "") for citation in [company["what_they_built"], company["positioning"],
            company["why_failed"], *company["pricing_now"], *company["complaints"]] if citation)),
        key=lambda company: (-len(company["source_types"]), company["key"]))[:15]
    accepted_keys = {company["key"] for company in companies}
    for company in companies:
        built = company["what_they_built"]
        if built:
            source = by_id[built["finding_id"]]
            preferred = next((value for value in (_meta(source).get("meta_description"), _meta(source).get("description"), source.get("summary"))
                              if isinstance(value, str) and value.strip() and not _contains_contact(value)
                              and any(clean_excerpt(value) and clean_excerpt(value) in text for text in options.get(source["id"], []))), None)
            built["description"] = product_description(built["quote"])
            if preferred and (not built["description"] or "[" in built["quote"]):
                built["description"] = product_description(preferred)
        company["confidence"] = "medium" if len(company["source_types"]) > 1 else "low"
        if company["status"] == "inactive" and company["kind"] != "oss":
            company["kind"] = "dead"
        company["complaints"] = company["complaints"][:3]
        company["timeline"] = sorted(company["timeline"], key=lambda entry: entry["date"])[:8]
    observations = {}
    for observation in (review or {}).get("observations", []):
        observations.setdefault(observation.get("source_id"), []).append(observation.get("quote"))
    lessons = []
    for assessment in (review or {}).get("assessments", [])[:8]:
        citations = []
        missing = False
        for sid in assessment.get("source_ids", []):
            quotes = observations.get(sid, [])
            if source_keys.get(sid) not in accepted_keys or not quotes:
                missing = True
                break
            resolved = [{"finding_id": sid, "quote_index": index} for index, text in enumerate(options.get(sid, []))
                        if any(isinstance(quote, str) and clean_excerpt(quote) and clean_excerpt(quote) in text for quote in quotes)]
            if not resolved:
                missing = True
                break
            citations.extend(resolved)
        keys = sorted({source_keys[cite["finding_id"]] for cite in citations})
        text = assessment.get("assessment")
        if not missing and citations and isinstance(text, str) and not _CONTACT.search(text):
            lessons.append({"text": plain_text(text), "kind": "pattern" if len(keys) >= 2 else "anecdote",
                            "citations": citations, "company_keys": keys, "confidence": "low"})
            # A closure status alone never explains why an attempt ended.
            for citation in citations:
                key = source_keys[citation["finding_id"]]
                quote = options[citation["finding_id"]][citation["quote_index"]]
                if records[key]["status"] == "inactive" and _CLOSURE.search(quote):
                    records[key]["why_failed"] = {"finding_id": citation["finding_id"], "quote": quote}
    for company in companies:
        for citation in [company["what_they_built"], company["positioning"], company["why_failed"],
                         *company["pricing_now"], *company["complaints"]]:
            if citation:
                citation["quote"] = plain_text(citation["quote"])
    for lesson in lessons:
        for citation in lesson["citations"]:
            citation["quote"] = plain_text(options[citation["finding_id"]][citation["quote_index"]])
    people, seen = [], set()
    for sid, key in source_keys.items():
        if key not in accepted_keys:
            continue
        meta = _meta(by_id[sid])
        url = safe_source_url(str(meta.get("author_url") or ""))
        name = str(meta.get("author") or meta.get("owner") or "")
        host = (urlsplit(url).hostname or "") if url else ""
        if (not url or not name or _CONTACT.search(name + " " + url) or url in seen
                or host not in {"news.ycombinator.com", "github.com", "ycombinator.com", "www.ycombinator.com"}):
            continue
        seen.add(url)
        people.append({"name": name[:120], "public_url": url, "relation": "Public contributor to " + records[key]["name"],
                       "role_claim_finding_id": sid})
    cutoff = (utcnow() - timedelta(days=730)).date().isoformat()
    return {"companies": companies, "lessons": lessons, "people": people[:10],
            "freshness": {"newest_source": max(dates) if dates else None, "oldest_used": min(dates) if dates else None,
                          "stale_companies": [c["key"] for c in companies if c["last_seen"] and c["last_seen"] < cutoff]},
            "coverage_gaps": list(dict.fromkeys(gaps or [])), "cost": cost or {}}
