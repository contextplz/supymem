"""Conservative coverage map for a snippet-only idea research scan.

These are leads from search excerpts, not verified page contents or market facts.
An absent fact is unknown; it is never a negative finding.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from app.research.pricing import pricing_evidence_quote


DIMENSIONS = (
    "problem_validation",
    "customer_demand",
    "competition",
    "pricing",
    "willingness_to_pay",
    "market_size",
    "risks",
)


def explicit_demand_leads(value: Any) -> list[dict[str, Any]]:
    """Hide legacy generic-snippet rows that were incorrectly called demand."""
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict) and row.get("source_strength") == "search_excerpt_unverified"]

_PATTERNS = {
    "problem_validation": re.compile(r"\b(struggl(?:e|ing)|pain point|frustrat(?:ed|ing)|problem|workaround|manual process)\b", re.I),
    "customer_demand": re.compile(r"\b(looking for|need (?:a|an|this)|wish there was|request(?:ed|ing) a|seeking a)\b", re.I),
    "pricing": re.compile(r"(?:\$|€|£|₹)\s*\d|\b(?:priced at|plans start at|per month|per user|annual subscription)\b", re.I),
    "willingness_to_pay": re.compile(
        r"\b(?:(?:we|i|customers?|buyers?|teams?)\s+(?:paid|pay|bought|purchased|budgeted|subscribed)"
        r"|(?:we|i)\s+(?:would|will)\s+pay|(?:we|i)['’]d\s+pay)\b", re.I),
    "market_size": re.compile(r"\b(?:market size|addressable market|TAM|SAM)\b", re.I),
    "risks": re.compile(r"\b(?:risk|concern|limitation|security issue|regulatory|compliance challenge)\b", re.I),
}
_CHALLENGE_PATTERNS = {
    "customer_demand": re.compile(r"\b(?:we|i) (?:do not|don't) need\b", re.I),
    "willingness_to_pay": re.compile(r"\b(?:nobody|no one) (?:will|would) pay\b|\b(?:we|i) (?:will not|won't|would not) pay\b", re.I),
}

_CUSTOMER_VOICE_HOSTS = (
    "reddit.com",
    "news.ycombinator.com",
    "stackoverflow.com",
    "quora.com",
)

_PROMOTION = re.compile(r"\b(?:launch hn|show hn|we(?:['’]re| are) (?:the )?founders? of|i(?:['’]m| am) (?:the )?founder of|press release|request a demo|watch demo|try our|our product|we (?:built|launched)|i (?:built|launched))\b", re.I)
_FIRST_PERSON = re.compile(r"\b(?:i|we|my|our)\b", re.I)


def customer_problem_evidence(source: dict, context: dict | None = None) -> bool:
    """Only topical, independent customer pain/demand can support validation."""
    from app.research.source_quality import _tokens, relevant_search_lead, research_topic

    text = str(source.get("snippet") or source.get("summary") or "")
    title = str(source.get("title") or "")
    topic = research_topic(context or {})
    if not topic or not _customer_voice_source(str(source.get("url") or "")) or _PROMOTION.search(title + " " + text):
        return False
    if not _FIRST_PERSON.search(text) or not any(_PATTERNS[name].search(text) for name in ("problem_validation", "customer_demand")):
        return False
    audience = _tokens(str((context or {}).get("customer") or ""))
    if audience and not audience & _tokens(title + " " + text):
        return False
    return relevant_search_lead(topic, {**source, "snippet": text})


def _customer_voice_source(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host.startswith(("forum.", "community.")) or any(host == base or host.endswith("." + base) for base in _CUSTOMER_VOICE_HOSTS)


def evidence_dimensions(
    sources: list[dict[str, Any]], competitors: list[dict[str, Any]], review: dict | None = None,
    *, prior_art: dict | None = None, context: dict | None = None
) -> dict[str, dict[str, Any]]:
    """Label explicit excerpt leads without upgrading them to validated demand.

    A product page can establish a possible alternative or advertised price. It
    cannot establish customer demand or willingness to pay for the founder's idea.
    Those dimensions therefore require a non-product source with explicit language.
    """
    dimensions = {
        name: {
            "status": "unknown",
            "source_strength": "search_excerpt_unverified",
            "support": [],
            "challenge": [],
            "unknown": "No direct evidence found in this bounded search scan.",
        }
        for name in DIMENSIONS
    }
    competitor_urls = {str(c.get("website") or "") for c in competitors}
    for competitor in competitors[:8]:
        url = str(competitor.get("website") or "")
        if url:
            dimensions["competition"]["support"].append(
                {"url": url, "finding": "Potential existing solution; fit and positioning need verification."}
            )
    for source in sources:
        url = str(source.get("url") or "")
        snippet = str(source.get("snippet") or "")
        if not url or not snippet:
            continue
        customer_voice = _customer_voice_source(url)
        verified = bool(source.get("verified"))
        for name, pattern in _CHALLENGE_PATTERNS.items():
            if name == "customer_demand" and not customer_problem_evidence(source, context):
                continue
            if customer_voice and url not in competitor_urls and pattern.search(snippet) and len(dimensions[name]["challenge"]) < 5:
                dimensions[name]["challenge"].append({"url": url, "finding": snippet[:240],
                                                     **({"verified": True} if verified else {})})
        for name, pattern in _PATTERNS.items():
            if name in {"problem_validation", "customer_demand"} and not customer_problem_evidence(source, context):
                continue
            if name in {"customer_demand", "willingness_to_pay", "problem_validation"} and (url in competitor_urls or not customer_voice):
                continue
            if verified and not customer_voice and name != "pricing":
                continue  # Long vendor pages mention risk or market size in passing; only prices are meaningful.
            negative = _CHALLENGE_PATTERNS.get(name)
            price = pricing_evidence_quote(snippet) if name == "pricing" else None
            match = pattern.search(snippet) if name != "pricing" else price
            if match and not (negative and negative.search(snippet)) and len(dimensions[name]["support"]) < 5:
                # A verified page is long: quote the matching passage, not its start.
                start = max(0, match.start() - 100) if verified and name != "pricing" else 0
                entry = {"url": url, "finding": price if name == "pricing" else snippet[start:start + 240]}
                if verified:
                    entry["verified"] = True
                dimensions[name]["support"].append(entry)
    # The cited review can identify useful context beyond literal snippet keywords.
    # It still supplies leads, never proof that this venture has demand or buyers.
    by_id = {source.get("id"): source for source in sources if source.get("id")}
    for assessment in (review or {}).get("assessments", []):
        category = str(assessment.get("category") or "")
        areas = (["competition"] if category.startswith("competitor_") or category == "adjacent_solution"
                 else [])
        for source_id in assessment.get("source_ids") or []:
            source = by_id.get(source_id)
            if not source:
                continue
            for area in areas:
                support = dimensions[area]["support"]
                if len(support) < 5 and not any(lead["url"] == source["url"] for lead in support):
                    support.append({"url": source["url"], "finding": (
                        "Potential existing solution or adjacent workflow; fit needs verification."
                        if area == "competition" else
                        "Related customer discussion; the problem and demand still need customer verification.")})
    for check in [*(review or {}).get("service_checks", []), *(review or {}).get("competitor_checks", [])]:
        source = by_id.get(check.get("source_id"))
        price = pricing_evidence_quote(check.get("pricing_quote"))
        support = dimensions["pricing"]["support"]
        if source and price and len(support) < 5 and not any(lead["url"] == source["url"] for lead in support):
            support.append({"url": source["url"], "finding": price})
    # Typed API observations can establish public alternatives, reactions and
    # attention. They never validate the founder's demand or willingness to pay.
    def add(area, citation, *, challenge=False, text=None):
        source = by_id.get(citation.get("finding_id"))
        if not source:
            return
        quote = citation.get("quote") or text
        bucket = dimensions[area]["challenge" if challenge else "support"]
        if quote and len(bucket) < 5 and not any(entry["url"] == source["url"] for entry in bucket):
            bucket.append({"url": source["url"], "finding": quote})
    for company in (prior_art or {}).get("companies", []):
        built = company.get("what_they_built")
        if built:
            add("competition", built)
        for price in company.get("pricing_now", []):
            if pricing_evidence_quote(price.get("quote")):
                add("pricing", price)
        for complaint in company.get("complaints", []):
            source = by_id.get(complaint.get("finding_id"))
            if source and customer_problem_evidence({**source, "snippet": complaint.get("quote")}, context):
                add("problem_validation", complaint)
        for signal in company.get("traction_signals", []):
            if type(signal.get("value")) is int and signal["value"] > 0:
                add("customer_demand", signal, text="Observed public attention; this is not demand for your offer.")
        for status in company.get("status_evidence", []) if company.get("status") == "inactive" else []:
            add("risks", status, challenge=True, text="A related earlier attempt is inactive; its reason for ending remains unknown.")
        if company.get("why_failed"):
            add("risks", company["why_failed"], challenge=True)
    for name, row in dimensions.items():
        if row["support"] and row["challenge"]:
            row["status"] = "mixed_leads"
            row["unknown"] = "Conflicting source excerpts need verification with target customers."
        elif row["challenge"]:
            row["status"] = "challenging_leads"
            row["unknown"] = "A source reports a challenge; it does not establish market-wide demand."
        elif row["support"]:
            if any(item.get("verified") for item in row["support"]):
                row["source_strength"] = "page_verified"
            row["status"] = "supporting_leads"
            row["unknown"] = "Source excerpt and relevance need verification; this is not proof of market demand."
        elif name == "pricing":
            row["unknown"] = "Insufficient public pricing evidence; this does not imply customers will not pay."
        elif name == "willingness_to_pay":
            row["unknown"] = "No observed payment, procurement, or customer interview evidence for this offer."
        elif name == "competition":
            row["unknown"] = "No close alternative surfaced in this scan; the market is not established as empty."
    return dimensions
