"""Primary category for a finding (doc 11 §2). Deterministic; flash LLM is optional."""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

CATEGORIES = (
    "competitor_direct",
    "competitor_alternative",
    "competitor_funded",
    "competitor_accelerator",
    "competitor_emerging",
    "adjacent_solution",
    "oss_repo",
    "oss_component",
    "api_service",
    "saas_service",
    "market_signal",
    "pricing_signal",
    "regulation",
    "internal_capability",
    "other",
)


def categorize_finding(title: str, text: str) -> tuple[str, list[str]]:
    blob = f"{title} {text}".lower()
    tags: list[str] = []
    if any(k in blob for k in ("yc ", "y combinator", "techstars")):
        tags.append("accelerator")
        return "competitor_accelerator", tags
    if any(k in blob for k in ("raised", "series", "seed round", "funding")):
        tags.append("funded")
        return "competitor_funded", tags
    if any(k in blob for k in ("github.com", "opensource", "open source", "mit license")):
        return "oss_repo", tags
    if any(k in blob for k in ("api ", "stripe", "twilio", "sendgrid")):
        return "api_service", tags
    if any(k in blob for k in ("reddit", "hacker news", "show hn", "waitlist")):
        return "market_signal", tags
    if "pricing" in blob or "plans" in blob:
        return "pricing_signal", tags
    if any(k in blob for k in ("alternative to", "vs ", "competitor")):
        return "competitor_direct", tags
    if title.strip():
        return "competitor_direct", tags
    return "other", tags


def competitor_grid(sources: dict, review: dict | None, companies: dict) -> dict:
    from app.core.local_mode import is_local
    from app.research.pricing import pricing_evidence_quote, review_metrics
    from app.research.presentation import clean_excerpt, display_text, verbatim_page_quote
    from app.research.prior_art.entities import entity_key

    fetched = {source_id: {**source, "observed_at": next((item.get("observed_at") for item in source.get("additional_excerpts") or []
                        if item.get("verified_page") is True), source.get("observed_at"))}
               for source_id, source in sources.items() if source.get("page_verified") is True}
    rows, identities = {}, {}
    local = is_local()
    def grounded(source, value):
        if not isinstance(value, str) or not value.strip():
            return None
        if local:
            return value if verbatim_page_quote(source, value) and display_text(value) == value else None
        excerpts = [item.get("text") or "" for item in source.get("additional_excerpts") or [] if item.get("verified_page") is True]
        if source.get("evidence_kind") == "web":
            excerpts.append(source.get("summary") or "")
        normalized = clean_excerpt(value)
        return value if normalized and any(normalized in clean_excerpt(excerpt) for excerpt in excerpts) else None
    for check in (review or {}).get("competitor_checks", []):
        source_id = check.get("source_id")
        source = fetched.get(source_id)
        if not source or not grounded(source, check.get("product_quote")):
            reason = "ungrounded_product_quote" if source else "unverified_page" if source_id in sources else "missing_source"
            log.info("research_competitor_dropped reason=%s", reason)
            continue
        company = companies.get(source_id, {})
        key = company.get("key") or entity_key(source["url"]) or source["url"]
        identities[source_id] = key
        row = rows.setdefault(key, {"name": display_text(company.get("name")) or display_text(source.get("title")) or "Alternative",
            "type": None, "url": source["url"], "price_of_comparable_item": None, "positioning_line": None,
            "rating": None, "review_count": None, "source_date": source.get("observed_at"), "field_sources": {}})
        if check.get("competition_type") in {"direct", "indirect", "substitute"}:
            row["type"] = row["type"] or check["competition_type"]
        comparable_quote = grounded(source, check.get("comparable_item_quote"))
        comparable = pricing_evidence_quote(comparable_quote)
        rating_quote = grounded(source, check.get("rating_quote"))
        count_quote = grounded(source, check.get("review_count_quote"))
        rating, count = review_metrics(rating_quote, count_quote)
        if not rating_quote or not re.search(r"\b(?:rated|rating|average|overall)\b|\b\d[\d,]*\s+reviews?\b", rating_quote, re.I):
            rating = None
        for field, value in (("price_of_comparable_item", comparable),
                             ("positioning_line", grounded(source, check.get("positioning_quote"))),
                             ("rating", rating), ("review_count", count)):
            if row[field] is None and value is not None:
                row[field] = display_text(value) if isinstance(value, str) else value
                row["field_sources"][field] = {"url": source["url"], "source_date": source.get("observed_at"),
                    "quote": rating_quote if field == "rating" else count_quote if field == "review_count" else
                    comparable_quote if field == "price_of_comparable_item" and local else value}
    reviews, seen = [], set()
    for item in (review or {}).get("customer_reviews", []):
        key = identities.get(item.get("competitor_source_id"))
        source = fetched.get(item.get("source_id"))
        if not key or not source:
            continue
        quote = grounded(source, item.get("quote"))
        if not quote:
            continue
        rating, count = review_metrics(quote, quote)
        if count is not None or re.search(r"\b(?:average|overall)\b", quote, re.I) or rating not in {1, 2, 3, 4} or rating == 4 and not re.search(r"\b(?:but|however|except|wish|although|yet)\b", quote, re.I):
            continue
        # ponytail: identical review text is counted once, use published review IDs to distinguish genuine duplicates.
        identity = (key, " ".join(quote.casefold().split()))
        if identity in seen:
            continue
        seen.add(identity)
        reviews.append({"competitor": rows[key]["name"], "rating": rating, "quote": display_text(quote),
                        "url": source["url"], "source_date": source.get("observed_at"),
                        "theme": display_text(item.get("theme"))})
    reviews.sort(key=lambda item: (item["rating"] == 4, item["rating"]))
    themes = {}
    for item in reviews:
        if not item["theme"]:
            continue
        theme = themes.setdefault(item["theme"].casefold(), {"theme": item["theme"], "reviews": []})
        theme["reviews"].append(item)
    complaints = sorted([{**theme, "review_count": len(theme["reviews"]), "thin": len(theme["reviews"]) < 3}
                         for theme in themes.values()], key=lambda theme: -theme["review_count"])
    candidates = [theme["theme"] for theme in complaints if not theme["thin"]]
    gap = ("Possible gaps to test: " + "; ".join(candidates) + ". Complaints do not prove an unserved market."
           if candidates else "no clear gap found")
    return {"rows": sorted(rows.values(), key=lambda row: {"direct": 0, "indirect": 1, "substitute": 2}.get(row["type"], 3)),
            "complaint_themes": complaints, "gap": gap}


def competitor_grid_markdown(grid: dict) -> str:
    from core.copy import no_dashes

    if not grid["rows"] and not grid["complaint_themes"]:
        return ""

    def cell(value):
        return no_dashes(str(value)).replace("|", "\\|").replace("\n", " ") if value is not None else "Unknown"
    lines = ["## Competitor grid", "Only fetched sources are shown. Classifications and complaint themes are AI assessments, not evidence of demand.",
             "| Alternative | Type (AI) | Price of comparable item | Positioning line | Rating | Review count | Source date |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in grid["rows"]:
        fields = []
        for field in ("price_of_comparable_item", "positioning_line", "rating", "review_count"):
            value = cell(row[field])
            source = row["field_sources"].get(field)
            if source:
                value += f" ([source]({source['url']}))"
            fields.append(value)
        lines.append("| " + " | ".join([f"[{cell(row['name'])}]({row['url']})", cell(row["type"]), *fields, cell(row["source_date"])]) + " |")
    lines.extend(["", "### Complaints, 1-3 stars before qualified 4-star praise"])
    for theme in grid["complaint_themes"]:
        label = "thin" if theme["thin"] else "repeated complaint, not a proven gap"
        lines.append(f"- {cell(theme['theme'])}: {theme['review_count']} distinct reviews ({label}).")
        for item in theme["reviews"]:
            lines.append(f"  - {cell(item['rating'])} stars: {cell(item['quote'])} ([source]({item['url']}), {cell(item['source_date'])}).")
    lines.extend(["", "### Gap", grid["gap"]])
    return no_dashes("\n\n".join(lines[:2]) + "\n\n" + "\n".join(lines[2:]))
