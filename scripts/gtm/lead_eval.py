"""Replay labelled QA snapshots through the real reachable-lead gate; never send."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/unit/gtm/fixtures/lead_eval.json"


def load_fixture():
    text = FIXTURES.read_text()
    return json.loads(text.replace("https://profiles.test/", "https://www.linkedin.com/")
        .replace("https://reviews.test/", "https://yelp.com/").replace("https://threads.test/", "https://www.reddit.com/"))


def metrics(leads, labels):
    selected = [lead for lead in leads if lead.get("judging_status") == "judged"
        and lead.get("lead_type") != "insight" and not lead.get("expert_role")]
    verdicts = [labels.get(lead["source_url"]) for lead in selected]
    labelled = all(verdict is not None for verdict in verdicts)
    return {"kept": len(selected), "precision_at_5": sum(bool(verdict and verdict["buyer"] and verdict["relevant"])
        for verdict in verdicts[:5]) / 5 if labelled else None,
        "junk_rate": sum(bool(verdict["junk"]) for verdict in verdicts) / len(verdicts) if labelled and verdicts else 0.0 if labelled else None,
        "labelled": sum(verdict is not None for verdict in verdicts)}


async def evaluate_case(case, *, live=False, labels=None):
    from app.gtm import journey, quality, reachable
    from scripts.qa.eval_reachable_leads import LocalRun, evaluate
    if live:
        venture = {key: case[key] for key in ("name", "audience", "problem", "competitors")}
        original_call = LocalRun.call
        async def call_with_cache(self, *args, cache_identity=None, **kwargs):
            return await original_call(self, *args, **kwargs)
        with patch.object(LocalRun, "call", call_with_cache):
            local, _, _, failure = await evaluate(venture | {"has_customers": False})
        leads = local.doc.get("reachable_leads", [])
        return metrics(leads, labels or {}) | {"failure": failure, "queries": local.doc.get("discovery_plan", [])}
    local = LocalRun(journey)
    snapshots = {row["url"]: row for row in case["snapshots"]}
    context = {"audience": case["audience"], "problem": case["problem"], "competitors": case["competitors"],
        "offer": "", "mode": case["mode"], "seen_lead_urls": case.get("seen_lead_urls", [])}
    hits = reachable.candidate_hits(case["hits"], context) if case["mode"] == "B2B" else case["hits"]
    pages = {row["url"]: row["text"] for row in case["snapshots"]}
    cache = {}
    class RecordedPages:
        async def scrape(self, url, *, main_only=True):
            return pages.get(url, "")
    async def model(payload, stage, context):
        if stage == "marketrole":
            row = context["candidates"][0]
            verdict = snapshots[row["url"]]["judgement"]
            rejected = bool(verdict.get("reject_reason"))
            return {"role_in_market": "unknown" if rejected else "buyer", "icp_fit": 0.0 if rejected else 0.95,
                "anti_icp": rejected, "homepage_quote": row["text"][:1000]}
        if stage == "passages":
            quote = snapshots[context["_domain"]]["judgement"].get("quote", "")
            return {"items": [{"id": row["id"], "relevance": 0.95 if quality.grounded_quote(row["text"], quote) else 0.0}
                for row in context["candidates"]]}
        if stage == "reachjudge":
            items = [snapshots[row["url"]]["judgement"] | {"id": row["id"]} for row in context["candidates"]]
            for item, row in zip(items, context["candidates"]):
                if item.get("quote"):
                    item["quote"] = next((page["text"] for page in reversed(row["evidence_pages"])
                        if quality.grounded_quote(page["text"], item["quote"])), item["quote"])
            return {"items": items}
        return {"items": []}
    async def call(*args, cache_identity=None, **kwargs):
        return await local.call(*args, **kwargs)
    with ExitStack() as patches:
        patches.enter_context(patch.object(journey, "utcnow", return_value=datetime(2026, 10, 16, tzinfo=timezone.utc)))
        patches.enter_context(patch.object(journey, "_journal", return_value=local))
        patches.enter_context(patch.object(journey, "_signal_call", call))
        patches.enter_context(patch.object(journey, "_discovery_model", model))
        patches.enter_context(patch("app.gtm.email_learning.cached", side_effect=cache.get))
        patches.enter_context(patch("app.gtm.email_learning.cache_save", side_effect=lambda key, value: cache.update({key: value})))
        patches.enter_context(patch("app.gtm.scrapy_leads._fetch", side_effect=AssertionError("offline eval attempted network")))
        patches.enter_context(patch("app.gtm.enrich.guessed_routes", return_value=[]))
        patches.enter_context(patch.dict(os.environ, {"GTM_ENRICH_ENABLED": "1", "FIRECRAWL_API_KEY": "offline-fixture"}))
        payload = {"workspace_id": "local-eval", "venture_id": "local-eval", "journey_id": "local-eval", "_job_attempt": 1}
        leads = await reachable.discover(RecordedPages(), hits, [], context, payload)
    return metrics(leads, {row["url"]: row["label"] for row in case["snapshots"]}) | {
        "retrieved": len(case["hits"]), "seeds": len(hits), "candidates": len(local.doc["_runtime"].get("reachable_candidates_quality_v1", [])),
        "drafted": sum(bool(lead.get("draft")) for lead in leads if lead.get("judging_status") == "judged"),
        "queries": journey._fallback_plan(case["audience"], case["problem"], case["competitors"])}


async def run(*, live=False, labels=None):
    fixture = load_fixture()
    from app.gtm import journey, reachable
    restaurant = next(case for case in fixture["ventures"] if case["name"] == "ShiftSwap QA")
    seeds = reachable.candidate_hits(fixture["restaurant_search_results"], restaurant | {"seen_lead_urls": []})
    queries = journey._fallback_plan(restaurant["audience"], restaurant["problem"], restaurant["competitors"])
    return {"mode": "live" if live else "offline", "fixture_basis": fixture["basis"],
        "restaurant_recall": {"seeds": len(seeds), "queries": queries},
        "ventures": {case["name"]: await evaluate_case(case, live=live, labels=(labels or {}).get(case["name"])) for case in fixture["ventures"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Manual paid run using configured Firecrawl and model keys; no sends")
    parser.add_argument("--labels", type=Path, help="Live ground truth: {venture: {source_url: {buyer, relevant, junk}}}")
    args = parser.parse_args()
    if args.live and not os.environ.get("FIRECRAWL_API_KEY", "").strip():
        parser.error("--live requires FIRECRAWL_API_KEY")
    if args.live and os.environ.get("LLM_OFFLINE", "0").lower() in {"1", "true", "yes"}:
        parser.error("unset LLM_OFFLINE for --live evaluation")
    sys.path[:0] = [str(ROOT), str(ROOT / "services/company_os"), str(ROOT / "packages")]
    labels = json.loads(args.labels.read_text()) if args.labels else None
    print(json.dumps(asyncio.run(run(live=args.live, labels=labels)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
