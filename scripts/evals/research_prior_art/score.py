"""Offline comparison of saved dossier JSON. Does not call any provider."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import statistics
from urllib.parse import urlsplit

DIMENSIONS = (
    "problem_validation", "customer_demand", "competition", "pricing",
    "willingness_to_pay", "market_size", "risks",
)
KNOWN_STATUSES = {"live", "inactive", "dead", "acquired"}
COVERED_STATUSES = {"supporting_leads", "challenging_leads", "mixed_leads"}


def identity(value: str) -> str:
    """Exact host matching; shared-host identities retain their entity path."""
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not host or not re.fullmatch(r"[a-z0-9.-]+", host) or parsed.username or parsed.password:
            return ""
        if host in {"github.com", "ycombinator.com"}:
            parts = parsed.path.strip("/").split("/")
            return host + "/" + "/".join(parts[:2]).lower() if len(parts) >= 2 else ""
        return host
    except ValueError:
        return ""


def keys(row: dict) -> set[str]:
    return {key for value in [row.get("key"), row.get("domain"), row.get("website"),
                              row.get("repo"), *(row.get("domains") or []), *(row.get("aliases") or [])]
            if isinstance(value, str) and (key := identity(value))}


def object_value(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def sources_for(dossier: dict) -> dict[str, dict]:
    findings = dossier.get("findings", [])
    if isinstance(findings, dict):
        findings = [row for group in findings.values() if isinstance(group, list) for row in group]
    sources = {}
    for row in findings:
        if isinstance(row, dict) and row.get("id") and row.get("founder_flag") != "exclude":
            if row["id"] in sources:
                raise ValueError("duplicate finding id")
            sources[row["id"]] = row
    return sources


def metadata(source: dict) -> dict:
    evidence = object_value(source.get("evidence"))
    return {**object_value(evidence.get("prior_art")), **object_value(source.get("prior_art")),
            **object_value(evidence.get("repository")), **object_value(source.get("repository"))}


def excerpts(source: dict) -> list[str]:
    evidence = object_value(source.get("evidence"))
    values = [source.get("summary"), evidence.get("snippet")]
    values += [row.get("text") for row in evidence.get("additional_excerpts", []) if isinstance(row, dict)]
    values += [row.get("text") for row in source.get("additional_excerpts", []) if isinstance(row, dict)]
    result = []
    for value in values:
        if isinstance(value, str) and (text := " ".join(value.split())) and text not in result:
            result.append(text)
    return result


def quote_choices(source: dict) -> list[str]:
    # Independent reconstruction of selection.quote_options' stored-excerpt
    # contract, so the CLI needs no application boot or provider credentials.
    spans = []
    for remaining in excerpts(source):
        while remaining:
            end = min(1000, len(remaining))
            if end < len(remaining) and (boundary := remaining.rfind(" ", 0, end + 1)) > 0:
                end = boundary
            span, remaining = remaining[:end], remaining[end:].lstrip()
            if span not in spans:
                spans.append(span)
    return spans if len(spans) <= 24 else []


def citation_valid(citation: dict, sources: dict[str, dict]) -> bool:
    identifier = citation.get("finding_id") or citation.get("source_id")
    source = sources.get(identifier) if isinstance(identifier, str) else None
    if not source:
        return False
    quote = citation.get("quote")
    index = citation.get("quote_index")
    if type(index) is int:
        choices = quote_choices(source)
        return 0 <= index < len(choices) and (quote is None or isinstance(quote, str) and " ".join(quote.split()) == choices[index])
    return isinstance(quote, str) and bool(quote.strip()) and any(
        " ".join(quote.split()) in text for text in excerpts(source))


def citations_in(value):
    if isinstance(value, dict):
        if "finding_id" in value or "source_id" in value:
            yield value
        else:
            for child in value.values():
                yield from citations_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from citations_in(child)


def status_grounded(company: dict, sources: dict[str, dict]) -> bool:
    status = company.get("status")
    for ref in company.get("status_evidence") or []:
        if ref.get("finding_id") not in company.get("finding_ids", []):
            continue
        source = sources.get(ref.get("finding_id"))
        if not source:
            continue
        data, evidence = metadata(source), object_value(source.get("evidence"))
        basis = ref.get("basis")
        observed = str(data.get("status", "")).lower()
        if basis == "yc_status" and {"active": "live", "public": "live"}.get(observed, observed) == status:
            return True
        if basis in {"page_available", "public_page", "verified_page", "homepage"} and status == "live" and (
            source.get("page_verified") or evidence.get("page_verified")):
            return True  # Public-page availability, not an independently audited business status.
        if basis == "repository_archived" and status == "inactive" and data.get("archived") is True:
            return True
        if citation_valid(ref, sources) and status in {"inactive", "dead", "acquired"}:
            return True
    return False


def grounding(prior: dict, sources: dict) -> tuple[int, int, int]:
    checks, pattern_failures = [], 0
    companies = prior.get("companies") or []
    company_sources = {c.get("key"): set(c.get("finding_ids") or []) for c in companies}
    for company in companies:
        owned = [sources[fid] for fid in company.get("finding_ids", []) if fid in sources]
        if company.get("name"):
            name = str(company["name"]).casefold()
            checks.append(any(str(metadata(s).get("company_name") or metadata(s).get("name") or "").casefold() == name or
                              (name == company.get("key") and keys(company) & keys({"website": s.get("url")})) or
                              name in " ".join([str(s.get("title") or ""), *excerpts(s)]).casefold()
                              for s in owned))
        if company.get("website"):
            checks.append(any(keys(company) & keys({"website": metadata(s).get("company_url") or s.get("url")})
                              for s in owned))
        if company.get("status") in KNOWN_STATUSES:
            checks.append(status_grounded(company, sources))
        for field in ("first_seen", "last_seen"):
            if company.get(field):
                checks.append(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(company[field]))) and any(
                    str(company[field]) == str(metadata(s).get(key))[:10] for s in owned
                    for key in ("date", "published_at", "pushed_at", "created_at", "last_activity_at")))
        for field in ("what_they_built", "product", "product_quote", "pricing_now", "pricing_history",
                      "complaints", "why_failed", "positioning_change"):
            value = company.get(field)
            if value:
                refs = list(citations_in(value))
                checks.append(bool(refs) and all(citation_valid(ref, sources) for ref in refs))
        for signal in company.get("traction_signals") or []:
            source = sources.get(signal.get("finding_id"))
            key = {"hn_points": "points", "hn_comments": "comments", "github_stars": "stars"}.get(signal.get("type"), signal.get("type"))
            expected = metadata(source).get(key) if source else None
            checks.append(type(expected) in {int, float} and type(signal.get("value")) in {int, float}
                          and expected == signal["value"])
    for lesson in prior.get("lessons") or []:
        refs = lesson.get("citations") or []
        valid = bool(refs) and all(citation_valid(ref, sources) for ref in refs)
        cited = {ref.get("finding_id") or ref.get("source_id") for ref in refs}
        linked = {key for key in lesson.get("company_keys", []) if company_sources.get(key, set()) & cited}
        pattern_ok = lesson.get("kind") != "pattern" or len(linked) >= 2
        pattern_failures += not pattern_ok
        checks.append(valid and pattern_ok)
    return sum(checks), len(checks), pattern_failures


def number(value):
    return float(value) if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def score_case(gold: dict, dossier: dict, judgments: dict | None = None) -> dict:
    summary = object_value(dossier.get("summary"))
    prior = object_value(summary.get("prior_art") or dossier.get("prior_art"))
    sources = sources_for(dossier)
    companies = prior.get("companies") or []
    candidate_basis = "prior_art_companies"
    if "prior_art" not in summary and "prior_art" not in dossier:
        candidate_basis = "legacy_admitted_sources"
        seen = set()
        companies = []
        for source in sources.values():
            evidence = object_value(source.get("evidence"))
            kind = source.get("evidence_kind") or evidence.get("evidence_kind")
            key = identity(str(source.get("url") or ""))
            if (key and key not in seen and kind not in {"name_source", "search_lead_unverified"}
                    and source.get("category") != "review_lead"):
                companies.append({"key": key, "website": source["url"], "status": "unknown"})
                seen.add(key)
    # Duplicates consume a rank slot but can never inflate distinct recall.
    top = companies[:8]
    direct = gold.get("direct") or []
    hits = sum(any(keys(expected) & keys(found) for found in top) for expected in direct)
    all_gold = [*direct, *(gold.get("dead") or []), *(gold.get("oss") or [])]
    known_relevant = [any(keys(expected) & keys(found) for expected in all_gold) for found in top]
    labels = []
    for company, known in zip(top, known_relevant):
        labels.append(True if known else next((judgments[k] for k in keys(company)
                                              if judgments and type(judgments.get(k)) is bool), None))
    judged = sum(label is not None for label in labels)
    # Precision@8 uses eight slots, including empty slots and repeated entities.
    seen, unique_relevant = set(), 0
    for company, label in zip(top, labels):
        company_keys = keys(company)
        if label is True and company_keys and not seen & company_keys:
            unique_relevant += 1
        seen.update(company_keys)
    dead = gold.get("dead") or []
    dead_hits = sum(any(keys(expected) & keys(found) and found.get("status") in {"inactive", "dead"}
                        and status_grounded(found, sources) for found in companies) for expected in dead)
    valid, total, pattern_failures = grounding(prior, sources)
    dimensions = object_value(summary.get("evidence_dimensions") or dossier.get("evidence_dimensions"))
    coverage = sum(object_value(dimensions.get(dim)).get("status") in COVERED_STATUSES for dim in DIMENSIONS)
    cost = object_value(prior.get("cost"))
    actual = number(cost.get("usd_actual"))
    cost_basis = "reported_provider_actual" if actual is not None else None
    if cost.get("measurement_complete") is True and (recorded := number(cost.get("usd_recorded"))) is not None:
        actual, cost_basis = recorded, "reported_recorded_cost"
    charged = number(object_value(summary.get("usage")).get("credits_micro")) if summary.get("operations") else None
    charged = charged / 1_000_000 if charged is not None else None
    if (object_value(summary.get("usage")).get("has_unsettled") or cost.get("incomplete") is True
            or cost.get("measurement_complete") is False):
        actual, cost_basis = None, "incomplete_receipts"
    if object_value(summary.get("usage")).get("has_unsettled"):
        charged = None
    latency = None
    if dossier.get("created_at") and dossier.get("completed_at"):
        try:
            elapsed = (datetime.fromisoformat(dossier["completed_at"].replace("Z", "+00:00")) -
                       datetime.fromisoformat(dossier["created_at"].replace("Z", "+00:00"))).total_seconds()
            latency = number(elapsed)
        except (ValueError, TypeError):
            pass
    mode = object_value(dossier.get("evaluation")).get("evidence_mode", "unclassified")
    result = {"id": gold["id"], "evidence_mode": mode, "candidate_basis": candidate_basis, "returned_at8": len(top),
              "recall_at8": hits / len(direct) if direct else None,
              "precision_at8": unique_relevant / 8 if judged == len(top) else None,
              "precision_of_returned_at8": unique_relevant / len(top) if top and judged == len(top) else None,
              "precision_at8_known_positive_lower_bound": unique_relevant / 8,
              "unjudged_at8": len(top) - judged, "dead_recall": dead_hits / len(dead) if dead else None,
              "dead_gold_count": len(dead), "grounding_rate": valid / total if total else None,
              "grounded_claims": valid, "claim_count": total, "pattern_contract_failures": pattern_failures,
              "dimension_coverage": coverage, "dimensions_present": sum(dim in dimensions for dim in DIMENSIONS),
              "cost_usd": actual, "cost_basis": cost_basis, "cost_usd_estimate": number(cost.get("usd_estimate")),
              "charged_cost_usd": charged, "charged_cost_basis": "settled_credits" if charged is not None else None,
              "latency_seconds": latency,
              "receipt_count": len(summary.get("operations") or [])}
    targets = {"recall_at8": .6, "precision_at8": .7, "dead_recall": .4, "grounding_rate": .95,
               "dimension_coverage": 4, "cost_usd": .4, "latency_seconds": 240}
    result["targets"] = {key: (None if result[key] is None else result[key] <= threshold
                               if key in {"cost_usd", "latency_seconds"} else result[key] >= threshold)
                         for key, threshold in targets.items()}
    return result


def aggregate(rows: list[dict], expected: int) -> dict:
    def percentile(key, p):
        values = sorted(row[key] for row in rows if row[key] is not None)
        return values[max(0, math.ceil(p * len(values)) - 1)] if values else None
    def median(key):
        values = [row[key] for row in rows if row[key] is not None]
        return statistics.median(values) if values else None
    def mean(key):
        values = [row[key] for row in rows if row[key] is not None]
        return statistics.mean(values) if values else None
    return {"expected_cases": expected, "scored_cases": len(rows), "complete_gold_run": len(rows) == expected,
            "evidence_modes": sorted({row["evidence_mode"] for row in rows}),
            "quality_evidence": "offline scoring only; live execution and founder usefulness require independent verification",
            "recall_at8_mean": mean("recall_at8"), "precision_at8_mean": mean("precision_at8"),
            "dead_recall_mean": mean("dead_recall"),
            "grounding_rate": sum(r["grounded_claims"] for r in rows) / sum(r["claim_count"] for r in rows)
                              if sum(r["claim_count"] for r in rows) else None,
            "dimension_coverage_median": median("dimension_coverage"),
            "cost_usd_p50": median("cost_usd"), "cost_usd_p95": percentile("cost_usd", .95),
            "latency_seconds_p50": median("latency_seconds"), "latency_seconds_p95": percentile("latency_seconds", .95),
            "measurement_counts": {key: sum(row[key] is not None for row in rows)
                                   for key in ("precision_at8", "dead_recall", "cost_usd", "latency_seconds")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exports", type=Path, help="Directory of {gold id}.json dossier exports")
    parser.add_argument("--gold", type=Path, default=Path(__file__).with_name("gold"))
    parser.add_argument("--judgments", type=Path, help="Optional JSON: {idea_id: {company_key: true|false}}")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    golds = [json.loads(p.read_text()) for p in sorted(args.gold.glob("*.json"))]
    if not golds or len({g["id"] for g in golds}) != len(golds):
        parser.error("gold must contain unique cases")
    judgments = json.loads(args.judgments.read_text()) if args.judgments else {}
    rows, missing = [], []
    for gold in golds:
        path = args.exports / (gold["id"] + ".json")
        if not path.exists():
            missing.append(gold["id"])
            continue
        rows.append(score_case(gold, json.loads(path.read_text()), judgments.get(gold["id"])))
    report = {"summary": aggregate(rows, len(golds)), "missing_cases": missing, "cases": rows}
    output = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
