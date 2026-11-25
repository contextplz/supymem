"""Founder-facing research shaping; original diagnostics stay in saved evidence."""
from __future__ import annotations

import logging
import re
from html import unescape

log = logging.getLogger(__name__)
INTERNAL_FIELDS = {"internal_classification", "internal_startup_checklist", "internal_new_user_flow", "internal_priority_attempt"}
EMPTY_REPORT_MESSAGE = "Your next move: share a relevant link or ask potential customers about their current workaround."
REVIEW_CONTEXT_NEXT_STEP = "Your sources are saved. Increase your local model's context window in Settings > Keys and connections > Your AI provider and on your model server if needed, then choose Review saved sources in Research."


def search_notice(document: dict) -> dict | None:
    from app.research.errors import SEARCH_KEY_FIELDS
    from app.research.local_providers import SEARCH_LIMITED, SEARCH_UNAVAILABLE

    settings = "/settings/local"
    code = document.get("error_code") or ""
    provider = code.removeprefix("research_search_key_")
    if code.startswith("research_search_key_") and provider in SEARCH_KEY_FIELDS:
        section = "Your AI provider" if provider == "openai" else "Your search and page reader"
        return {"state": "key_rejected", "settings_href": settings,
                "next_step": f"Your search key was rejected. In Settings > Keys and connections > {section}, update your {SEARCH_KEY_FIELDS[provider]}, then choose Run research again in Research."}
    outcomes = {operation["response"].get("search_outcome")
                for operation in document.get("_runtime", {}).get("operations", {}).values()
                if isinstance(operation.get("response"), dict)}
    if "results" in outcomes:
        return None
    for state, message in (("limited", SEARCH_LIMITED), ("unavailable", SEARCH_UNAVAILABLE)):
        if state in outcomes:
            return {"state": state, "next_step": message, "settings_href": settings}
    if "no_matches" in outcomes:
        return {"state": "no_matches", "next_step": "Your built-in search found no matches. On Idea, describe your audience more broadly, then return to Research and choose Run research again."}
    return None


def verbatim_page_quote(source: dict, quote) -> bool:
    return bool(isinstance(quote, str) and quote.strip() and any(
        entry.get("verified_page") is True and entry.get("url") and entry["url"] == source.get("url")
        and quote in (entry.get("text") or "") for entry in source.get("additional_excerpts") or []))


NEXT_STEPS = {
    "demand": ("Talk to 5 potential customers about how they solve this problem today.", "customers"),
    "willingness_to_pay": ("Ask potential customers what they pay today and what would make them switch.", "customers"),
    "pricing": ("Compare current prices and included features for the closest alternatives.", "pricing"),
    "competitor_offering": ("Check what the closest competitors offer and who they serve.", "competitors"),
}


class Gap(str):
    def __new__(cls, kind: str, text: str):
        item = super().__new__(cls, text)
        item.kind = kind
        return item


def gap(kind: str, text: str) -> Gap:
    return Gap(kind, text)


def classify_gap(text: str) -> str:
    # ponytail: legacy/model prose uses conservative rules, replace with structured model kinds after pipeline migration.
    lower = text.lower()
    for kind, terms in (
        ("system_license_metadata", ("license metadata", "license compatibility", "legal compatibility")),
        ("system_unsent_material", ("could not be sent", "kept out of the ai", "data we do not send")),
        ("system_verification", ("excerpt", "source-page", "source page", "full pages", "full source", "verification", "verified pages", "unverified search")),
        ("system_request_limit", ("request limit", "cost limit", "research limit", "bounded subset", "request size")),
        ("system_adapter", ("unavailable", "unreadable", "cached snapshot", "could not be cached", "provider")),
        ("system_assessment", ("assessment failed", "evidence validation", "citations", "provenance")),
    ):
        if any(term in lower for term in terms):
            return kind
    for kind, terms in (
        ("willingness_to_pay", ("willingness", "willing to pay", "pay today", "payment", "buyer budget")),
        ("pricing", ("pricing", "price", "prices", "costs")),
        ("demand", ("demand", "customer", "interview", "buyer", "user need", "talk to", "manual trial")),
        ("competitor_offering", ("competitor", "competition", "offering", "alternatives", "prior attempt", "inactive attempt")),
    ):
        if any(term in lower for term in terms):
            return kind
    return "system_scope"


def gap_items(values) -> list[dict]:
    items = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, dict):
            kind, text = value.get("kind"), value.get("text")
        elif isinstance(value, str):
            kind, text = getattr(value, "kind", None) or classify_gap(value), str(value)
        else:
            continue
        if isinstance(kind, str) and isinstance(text, str) and text.strip():
            item = {"kind": kind, "text": text.strip()}
            if item not in items:
                items.append(item)
    return items


def saved_gaps(value: dict, field: str = "coverage_gaps") -> list[Gap]:
    return [gap(item["kind"], item["text"]) for item in gap_items(value.get("gap_items") or value.get(field) or [])]


def record_gaps(value: dict, field: str = "coverage_gaps") -> dict:
    value["gap_items"] = gap_items(value.get(field) or [])
    system = [item["kind"] for item in value["gap_items"] if item["kind"] not in NEXT_STEPS]
    if system:
        log.info("research_system_gaps kinds=%s", system)
    return value


def next_questions(value: dict) -> list[dict]:
    review = value.get("review") or value.get("validation") or {}
    if not isinstance(review, dict):
        review = {}
    owners = [review, value, *[value[key] for key in ("summary", "fast_scan", "prior_art") if isinstance(value.get(key), dict)]]
    items = gap_items([item for owner in owners for item in saved_gaps(owner, "gaps" if "gaps" in owner else "coverage_gaps")])
    questions = []
    for owner in owners:
        for field in ("next_questions", "open_questions", "suggested_investigations", "proposed_tests", "questions"):
            candidates = owner.get(field)
            for candidate in candidates if isinstance(candidates, list) else []:
                text = candidate.get("text") if isinstance(candidate, dict) else candidate
                if not isinstance(text, str):
                    continue
                kind = (candidate.get("kind") or classify_gap(text)) if isinstance(candidate, dict) else classify_gap(text)
                if kind not in NEXT_STEPS:
                    continue
                sentence = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))[0]
                if not re.match(r"^(Talk|Ask|Compare|Check|Interview|Test|Find|Learn|Confirm|Measure|Validate|Review|Contact|Explore|Offer|Run)\b", sentence, re.I) or re.search(r"excerpt|verif|establish|evidence|source.page", sentence, re.I):
                    sentence = NEXT_STEPS[kind][0]
                sentence = sentence.rstrip(".!?") + "."
                if not any(question["text"] == sentence for question in questions):
                    questions.append({"kind": kind, "text": sentence, "action": NEXT_STEPS[kind][1]})
    for item in items:
        kind = item["kind"]
        if kind in NEXT_STEPS and not any(question["kind"] == kind for question in questions):
            text, action = NEXT_STEPS[kind]
            questions.append({"kind": kind, "text": text, "action": action})
    return questions[:3]


def redact_contacts(text: str) -> str:
    from app.boundary.detectors.pii import _EMAIL, _E164, _NANP, _SSN, _STREET
    text = unescape(text)
    for pattern in (_EMAIL, _E164, _NANP, _SSN, _STREET):
        text = pattern.sub("", text)
    return re.sub(r"\b[\w.%+-]+@[^\s<>]+|\b(?:mailto|tel):[^\s<>]+", "", text, flags=re.I)


def clean_excerpt(text: str) -> str:
    # ponytail: bounded Markdown/nav heuristics, use a document parser if source formats grow.
    chrome = r"(?:skip to (?:main )?content|skip navigation|home|menu|navigation|log ?in|sign ?in|sign ?up|contact(?: us)?|privacy(?: policy)?|terms(?: of (?:service|use))?|cookie(?: policy| settings)?|accept (?:all )?cookies|about(?: us)?|pricing|products?|resources?|solutions?|documentation|docs|blog|careers|get started|book a demo|free bag.*join today.*)"
    text = re.sub(r"<(?:nav|header|footer|script|style)\b[^>]*>.*?</(?:nav|header|footer|script|style)>", "", text, flags=re.I | re.S)
    text = re.sub(r"!\[[^\]\n]*\]\([^\n)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^\n)]*\)",
                  lambda match: "" if re.fullmatch(chrome, match[1].strip(" *"), re.I) else match[1], text)
    text = re.sub(r"\b\d(?:\.\d)?\s*[★☆][^.]*?\b(?:reviews?|ratings?)\b", "", text)
    text = redact_contacts(text)
    text = re.sub(r"\b(?:watch demo|request a demo|tap to unmute|youtube|subscribe|\d[\d,.]* subscribers?)\b", "", text, flags=re.I)
    lines = [line.strip(" #*_|>\t") for line in unescape(text).splitlines()]
    text = " ".join(line for line in lines if not re.fullmatch(chrome, line, re.I))
    return " ".join(re.sub(r"<[^>]+>|[*_`#~]+", "", text).split())


def display_text(value) -> str:
    # ponytail: legacy placeholder prose is matched conservatively, prefer structured display fields for new reports.
    if not isinstance(value, str):
        return ""
    text = " ".join(clean_excerpt(value).split())
    if re.search(r"^(?:unknown\b|status:\s*(?:unknown|supporting_leads|challenging_leads|mixed_leads)|supporting leads\b|challenging leads\b|mixed leads\b|still unknown:|(?:still )?no (?:strong public |direct |usable |explicit |completed |cited |relevant )?(?:evidence|sources?|pricing|prior attempts|customer conversations)|not enough (?:public )?evidence|insufficient public|potential existing solution;|source excerpt and relevance|search excerpts are leads|observed public attention;|a related earlier attempt is inactive;|planning is cheap|public search cannot|existing products mean|few sources were|verified public source passages|verified sources describe)", text, re.I):
        return ""
    if re.search(r"\b(?:no (?:direct |public |usable )?(?:evidence|sources?) (?:was |were )?found|not enough (?:public )?evidence|still no evidence)\b", text, re.I):
        return ""
    if re.search(r"\b(?:need(?:s)? verification|does not (?:imply|establish|prove)|not (?:proof|evidence) of market demand|not demand for your offer)\b", text, re.I):
        return ""
    if len(text) > 320:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        kept = []
        for sentence in sentences:
            if len(" ".join([*kept, sentence])) > 320:
                break
            kept.append(sentence)
        return " ".join(kept)
    return text


def recommended_opening(context: dict | None = None) -> str:
    from app.idea.agent import is_unsure_answer
    from app.intake.smart import _prefill
    from core.copy import founder_copy

    context = context or {}
    customer = display_text(context.get("customer"))
    problem = display_text(context.get("problem"))
    offer = display_text(context.get("workflow") or context.get("offer"))
    if re.match(r"^(?:i|we|you)\b", offer, re.I):
        offer = display_text(_prefill(offer).get("offer"))
    customer, problem, offer = [text if not is_unsure_answer(text) and not re.search(r"\bno clue\b", text, re.I) else ""
                               for text in (customer, problem, offer)]
    if context.get("project_intent") == "personal":
        return founder_copy(f"Your best opening: try {offer or 'one small version'} on {problem or 'one task in your own routine'}. Keep it focused on your own use and record what works.")
    audience = customer or "one reachable user group"
    pain = problem.rstrip('.') or "one recurring task for this group"
    version = f"Your proposed first version: {offer.rstrip('.')}. " if offer else "Start with one useful result. "
    return founder_copy(f"Your best opening: focus on {audience}. The problem to test: {pain}. {version}Ask me to prepare a small test of whether this group needs that result before you build or sell.")


def founder_report(findings: dict, dimensions: dict, review: dict | None = None,
                   prior_art: dict | None = None, assumptions: list[str] | None = None, *, context: dict | None = None) -> dict:
    from app.core.local_mode import is_local
    from app.research.deep import safe_source_url
    from app.research.pricing import pricing_evidence_quote, pricing_point
    from app.research.categorize import competitor_grid
    from app.research.evidence_dimensions import _PROMOTION, customer_problem_evidence
    from app.research.prior_art.entities import entity_key, plain_text, product_description
    from app.research.source_quality import relevant_search_lead, research_topic

    sources = {finding["id"]: finding for group in findings.values() for finding in group
               if finding.get("id") and safe_source_url(finding.get("url")) and finding.get("founder_flag") != "exclude"
               and finding.get("evidence_kind") not in {"name_source", "search_lead_unverified"}
               and finding.get("category") != "review_lead"}
    by_url = {source["url"]: source for source in sources.values()}
    companies = {source_id: company for company in (prior_art or {}).get("companies", [])
                 for source_id in company.get("finding_ids", [])}
    ranks = {}
    for assessment in (review or {}).get("assessments", []):
        for source_id in assessment.get("source_ids", []):
            rank = 0 if assessment.get("category") == "competitor_direct" else 1
            ranks[source_id] = min(ranks.get(source_id, 2), rank)
    checks = {check.get("source_id"): check for check in (review or {}).get("competitor_checks", [])}
    display_dimensions = {}
    used_quotes = set()
    for area, dimension in dimensions.items():
        if area in INTERNAL_FIELDS:
            continue
        if not isinstance(dimension, dict):
            continue
        buckets = {}
        for field in ("support", "challenge"):
            candidates = list(dimension.get(field) or [])
            if area == "competition" and field == "support":
                candidates = [{"url": sources[source_id]["url"], "finding": check.get("product_quote")}
                              for source_id, check in checks.items() if source_id in sources and check.get("product_quote")] + candidates
            candidates.sort(key=lambda item: (ranks.get(by_url.get(item.get("url"), {}).get("id"), 2),
                                               by_url.get(item.get("url"), {}).get("founder_flag") != "pin"))
            entries, seen_companies, seen_names = [], set(), set()
            for candidate in candidates:
                source = by_url.get(candidate.get("url"))
                if not source:
                    continue
                if area == "competition" and _PROMOTION.search(str(source.get("title") or "") + " " + str(source.get("summary") or source.get("snippet") or "")) and not (context and relevant_search_lead(research_topic(context), {**source, "snippet": source.get("summary") or source.get("snippet")})):
                    continue
                if area in {"problem_validation", "customer_demand"} and _PROMOTION.search(str(source.get("title") or "") + " " + str(source.get("summary") or source.get("snippet") or "")):
                    continue
                if area in {"problem_validation", "customer_demand"} and not customer_problem_evidence({**source, "snippet": candidate.get("finding")}, context):
                    continue
                passages = [source.get("summary") or source.get("snippet") or "",
                            *[excerpt.get("text") or "" for excerpt in source.get("additional_excerpts") or []]]
                raw_quote = clean_excerpt(candidate.get("finding") or "")
                quote = pricing_evidence_quote(raw_quote) if area == "pricing" else display_text(raw_quote)
                grounded = quote and any(quote in clean_excerpt(passage) or bool(plain_text(quote)) and plain_text(quote) in plain_text(passage) for passage in passages)
                if not grounded and (field == "challenge" or area != "competition"):
                    continue
                if not grounded:
                    quote = next((display_text(passage) for passage in passages if display_text(passage)), "")
                if not quote:
                    continue
                if area == "pricing" and not pricing_evidence_quote(quote):
                    continue
                company = companies.get(source["id"], {})
                name = display_text(company.get("name")) or display_text(source.get("title"))
                if area == "competition":
                    key = company.get("key") or entity_key(source["url"])
                    if not key or key in seen_companies or name.casefold() in seen_names:
                        continue
                    seen_companies.add(key)
                    seen_names.add(name.casefold())
                    text = product_description(quote)
                    if not name or not text:
                        continue
                    text = f"{name}: {text}"
                else:
                    text = quote
                point = pricing_point(quote, name, source["url"]) if area == "pricing" else None
                if point:
                    text = " - ".join((point["product"], point["price"], point["billing_period"]))
                    quote = display_text(quote)
                identity = (source["url"], quote.casefold())
                if identity in used_quotes:
                    continue
                used_quotes.add(identity)
                entries.append({"name": name, "finding": text, "quote": quote, "url": source["url"], "source_id": source["id"], **({"pricing": point} if point else {})})
                if len(entries) == 5:
                    break
            buckets[field] = entries
        display_dimensions[area] = buckets
    for company in (prior_art or {}).get("companies", []):
        for signal in company.get("traction_signals", []):
            source = sources.get(signal.get("finding_id"))
            labels = {"hn_points": "Hacker News points", "hn_comments": "Hacker News comments", "github_stars": "GitHub stars", "app_rating_count": "app ratings", "team_size": "team members"}
            if source and type(signal.get("value")) is int and signal["value"] > 0 and signal.get("type") in labels:
                text = f"{company['name']}: {signal['value']:,} {labels[signal['type']]}"
                bucket = display_dimensions.get("customer_demand", {}).get("support", [])
                if len(bucket) < 5:
                    bucket.append({"name": company["name"], "finding": text, "quote": text, "url": source["url"], "source_id": source["id"]})
    display_dimensions = {area: row for area, row in display_dimensions.items() if row["support"] or row["challenge"]}
    ordered = ["competition", "customer_demand", "pricing", "problem_validation", "willingness_to_pay", "market_size"]
    facts, seen = [], set()
    for index in range(5):
        for area in ordered:
            entries = display_dimensions.get(area, {}).get("support", [])
            if index >= len(entries):
                continue
            entry = entries[index]
            identity = (entry["url"], entry["quote"])
            if identity not in seen:
                facts.append(entry)
                seen.add(identity)
            if len(facts) == 3:
                break
        if len(facts) == 3:
            break
    if not facts and not dimensions:
        for source in sources.values():
            raw = source.get("summary") or source.get("snippet") or ""
            if _PROMOTION.search(str(source.get("title") or "") + " " + raw):
                continue
            text = display_text(raw)
            if text:
                facts.append({"finding": f"{source.get('title') or 'Related source'}: {product_description(text)}",
                              "quote": text, "url": source["url"], "source_id": source["id"]})
            if len(facts) == 3:
                break
    contradictions = []
    for dimension in display_dimensions.values():
        for entry in dimension["challenge"]:
            if not any((other["url"], other["quote"]) == (entry["url"], entry["quote"]) for other in contradictions):
                contradictions.append(entry)
    if is_local():
        for entry in [*facts, *(entry for dimension in display_dimensions.values() for entries in dimension.values() for entry in entries)]:
            if not verbatim_page_quote(sources[entry["source_id"]], entry.get("quote")):
                entry.pop("quote", None)
    return {"dimensions": display_dimensions, "findings": facts, "contradictions": contradictions[:3],
            "empty_message": EMPTY_REPORT_MESSAGE if not facts and not display_dimensions else None,
            "opening": recommended_opening(context),
            "assumptions": [text for value in assumptions or [] if (text := display_text(value))][:2],
            "competitor_grid": competitor_grid(sources, review, companies)}


def founder_payload(value):
    if isinstance(value, list):
        return [founder_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "internal_classification" in value:
        value = {key: item for key, item in value.items() if key != "checklist"}
    result = {key: founder_payload(item) for key, item in value.items()
              if key not in INTERNAL_FIELDS and key not in {"gap_items", "coverage_gap", "coverage_gap_kind", "coverage_gaps", "gaps", "limitations", "open_questions", "suggested_investigations", "proposed_tests", "questions", "next_questions", "unknown"}}
    for field in ("quote", "finding", "summary", "title", "text", "product_quote", "customer_quote", "positioning_quote", "pricing_quote", "limits_quote", "credentials_quote"):
        if isinstance(result.get(field), str):
            result[field] = display_text(result[field])
    for field, item in list(result.items()):
        if isinstance(item, str) and item.strip() and field not in {"status", "state", "quote", "summary", "title", "text"} and not display_text(item):
            result.pop(field)
        elif field in {"why", "why_not"} and isinstance(item, list):
            result[field] = [text.replace("Your idea review found:", "Your assumption:")
                             for text in item if display_text(text)]
    for field in ("overview", "summary"):
        if not value.get("url") and isinstance(result.get(field), str) and (not display_text(result[field]) or classify_gap(result[field]) in {"system_verification", "system_assessment", "system_request_limit"}
                or re.search(r"does? not establish|no evidence establishes|sources do not establish", result[field], re.I)):
            result.pop(field)
    if isinstance(value.get("evidence_dimensions"), dict) and isinstance(value.get("sources"), list):
        sources = [{**source, "id": source.get("id") or str(index),
                    "summary": source.get("snippet") or source.get("summary") or ""}
                   for index, source in enumerate(value["sources"]) if isinstance(source, dict)]
        display = founder_report({"sources": sources}, value["evidence_dimensions"], value.get("review"), context=value)
        result["founder_report"] = display
        result["evidence_dimensions"] = {key: {**result["evidence_dimensions"][key], **dimension}
                                         for key, dimension in display["dimensions"].items()}
    if result.get("verdict") == "no_sources" and not result.get("why_not"):
        result["why_not"] = [EMPTY_REPORT_MESSAGE]
    if any(key in value for key in ("gap_items", "coverage_gaps", "gaps", "open_questions", "suggested_investigations", "proposed_tests", "questions", "review", "validation")):
        questions = next_questions(value)
        result["next_questions"] = questions
        result["coverage_gaps"] = [question["text"] for question in questions]
        if "gaps" in value:
            result["gaps"] = [question["text"] for question in questions]
        if "proposed_tests" in value and all(isinstance(item, str) for item in value["proposed_tests"]):
            result["proposed_tests"] = [question["text"] for question in questions]
        elif isinstance(value.get("proposed_tests"), list):
            result["proposed_tests"] = founder_payload(value["proposed_tests"][:3])
    return result


def founder_markdown(markdown: str, meta: dict) -> str:
    from app.idea.validation_agent import review_text

    if markdown.strip() == EMPTY_REPORT_MESSAGE:
        return EMPTY_REPORT_MESSAGE + "\n"
    lines = []
    skipping = 0
    omitted_claim = False
    owners = [meta, *[meta[key] for key in ("summary", "fast_scan", "review", "prior_art") if isinstance(meta.get(key), dict)]]
    diagnostics = {str(item) for owner in owners for item in saved_gaps(owner) if item.kind not in NEXT_STEPS}
    for line in markdown.splitlines():
        if line.lstrip().startswith("|"):
            line = "|".join("" if cell.strip().lower().startswith("unknown") else cell for cell in line.split("|"))
        if line.startswith("#"):
            depth = len(line) - len(line.lstrip("#"))
            if skipping and depth <= skipping:
                skipping = 0
            if re.search(r"\b(gaps|biggest risk|strengths|limitations|what remains unknown|what to find out next|what i'd do next|recommended next tests|next steps)\b", line, re.I):
                skipping = depth
            omitted_claim = False
            line = re.sub(r"(?i)(executive summary|what challenges the idea|cited risks|risks and alternatives)", "Market insights", line)
        if re.search(r"This is an AI assessment, not proof of demand, users or revenue\.|^Your (?:idea review found|assumption):", line, re.I):
            continue
        source_line = bool(re.search(r"https?://|^\s*(Source:|Excerpt:|Additional excerpt:)", line))
        if omitted_claim and re.match(r"^\s*(?:Source:|Excerpt:|Additional excerpt:)", line):
            continue
        if line.strip().lstrip("-> ") in diagnostics:
            omitted_claim = True
            continue
        content = line.strip().lstrip("-> *")
        content = re.sub(r"^(?:Lead|Challenging lead|Excerpt|Additional excerpt|Top risk):\s*", "", content, flags=re.I)
        if content and (not display_text(content) or not source_line and not review_text(content) or re.search(r"\b(?:First seen|Last seen|Public pricing|User complaint):\s*Unknown\b", content, re.I)):
            omitted_claim = bool(clean_excerpt(content))
            if source_line and not omitted_claim and not skipping:
                lines.append(redact_contacts(line))
            continue
        if content:
            omitted_claim = False
        if not skipping and not (not source_line and line.strip() and classify_gap(line) in {
            "system_verification", "system_license_metadata", "system_unsent_material", "system_adapter", "system_request_limit", "system_assessment"
        }):
            lines.append(redact_contacts(line).replace("Your idea review found:", "Your assumption:"))
    # ponytail: pipe-table Markdown only, use a parser if report table formats expand.
    def prune_table(match):
        rows = match[0].splitlines()
        divider = [row for row in rows[1:] if re.fullmatch(r"[\s|:-]+", row)]
        found = [row for row in rows[1:] if not re.fullmatch(r"[\s|:-]+", row)
                 and any(display_text(cell) for cell in row.strip().strip("|").split("|")[1:])]
        return "\n".join([rows[0], *divider, *found]) + "\n" if found else ""

    lines = re.sub(r"(?m)(?:^[ \t]*\|[^\n]*(?:\n|$))+", prune_table, "\n".join(lines)).splitlines()
    rendered, headings = [], []
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+", line)
        if heading:
            level = len(heading[1])
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, line))
        elif line.strip():
            rendered.extend(title for _, title in headings)
            headings = []
            rendered.append(line)
        elif rendered and not headings:
            rendered.append(line)
    if not any(line.strip() and not re.match(r"^(?:#|State:|Your assumption:)", line) for line in rendered):
        return EMPTY_REPORT_MESSAGE + "\n"
    if not any(re.match(r"^#+\s+Your opening\b", line, re.I) for line in rendered):
        display = meta.get("founder_report") or {}
        rendered.extend(["", "## Your opening", "", display.get("opening") or recommended_opening(meta)])
    from core.copy import no_dashes
    if meta.get("mode") == "sample":
        from app.research.deep import SAMPLE_RESEARCH_NOTE
        rendered = [SAMPLE_RESEARCH_NOTE, "", *[line for line in rendered if line != SAMPLE_RESEARCH_NOTE]]
    return no_dashes("\n".join(rendered).rstrip()) + "\n"
