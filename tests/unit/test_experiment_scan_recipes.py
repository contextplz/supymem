"""Bounded research recipes work for unrelated business and consumer audiences."""
import asyncio
import json
import re
import threading
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.company_os_harness import local_owner_clients
from tests.unit.test_autonomous_experiments import credit_meter, run_jobs, venture


@pytest.fixture(params=[
    ("company", "freight brokers in Morocco", "tracking overdue shipment documents"),
    ("consumer", "weekend cyclists", "planning safe bike routes"),
], ids=["b2b-morocco", "consumer-cycling"])
def research_snapshot(request, monkeypatch, venture):
    from app.experiments import autonomous, ledger
    from app.state import db

    mode, who, problem = request.param
    monkeypatch.setattr(autonomous.metered, "price_micro", lambda *args: 1)
    _client, venture_id, workspace_id = venture
    with db()() as session:
        direction = ledger.current_direction(session, venture_id)
    return {"venture_id": venture_id, "workspace_id": workspace_id, "direction": direction,
            "stage": "ideating", "journey": {"icp": {"who": who, "problem": problem, "mode": mode, "needs_input": False}},
            "waitlist": {}, "app_id": None}


def test_recipe_runner_uses_shared_complaint_hook(research_snapshot, monkeypatch):
    from app.experiments import autonomous
    from app.signals.research import ScrapedText, SearchResults

    extracted = []
    hit = {"url": "https://www.reddit.com/r/research/comments/abc123/task/", "title": "Public task discussion"}

    async def search(query, **kwargs):
        assert kwargs == {"tbs": "qdr:m3"}
        return SearchResults([hit], provider_rows=1, rejected_shape=0, rejected_missing_url=0,
                             usage={"firecrawl_self_hosted": True})

    async def scrape(url):
        return ScrapedText("A dated complaint", metadata={}, usage={"firecrawl_self_hosted": True})

    def complaint(icp, source, text, metadata, now, context):
        extracted.append((icp["who"], text))
        return {**source, "quote": text, "published_at": now.isoformat()}

    monkeypatch.setattr(autonomous, "_complaint", complaint)
    provider = SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1, search=search, scrape=scrape)
    specs = [spec for spec in autonomous.select_runnable(research_snapshot, provider) if spec["scan"] in {"demand", "competitors"}]
    for spec in specs:
        recipe = autonomous.RECIPES[spec["scan"]]
        assert spec["queries"] == recipe["query_builder"](spec["icp"])
        assert spec["extractor"] == "complaint" and spec["pages"] == 6
        result = asyncio.run(autonomous._scan(spec, provider))
        assert result["complaints"] == 1 and result["sources"][0]["quote"] == "A dated complaint"
        assert result["actual_micro"] == len(spec["queries"]) + 1
        legacy = {key: value for key, value in spec.items() if key not in
            {"variants", "metric", "extractor", "recency", "success_threshold", "failure_threshold"}}
        assert asyncio.run(autonomous._scan(legacy, provider))["complaints"] == 1
    assert len(extracted) == 4 and all(who == research_snapshot["journey"]["icp"]["who"] for who, text in extracted)


def test_hypotheses_fill_short_slots_and_gap_queries_dedupe_audience(research_snapshot, monkeypatch):
    from app.core.local_mode import is_local
    from app.experiments import autonomous
    from app.research.prior_art import plan

    icp = research_snapshot["journey"]["icp"]
    task = icp["problem"]
    icp["problem"] = f"{icp['who'].capitalize()} need a way to fill gaps in {task}. This is a second sentence."
    terms = autonomous._hypothesis_slots(icp)
    phrase = autonomous._search_phrase(terms["problem"])
    monkeypatch.setattr(plan, "queries", lambda *args: [
        ("prior_category", f"{terms['who']} {phrase} {terms['who']} alternatives"),
        ("prior_complaints", f"{phrase} reviews {phrase}"),
    ])
    specs = [spec for spec in autonomous.select_runnable(research_snapshot,
        SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1)) if spec["scan"] in {"demand", "competitors"}]
    for spec in specs:
        assert len(spec["hypothesis"]) <= 160 and spec["hypothesis"].count(".") == 1
        assert "need a way to" not in spec["hypothesis"] and "second sentence" not in spec["hypothesis"]
        assert "trouble filling gaps" in spec["hypothesis"]
    gaps = next(spec for spec in specs if spec["scan"] == "competitors")
    assert all(query.lower().count(icp["who"].lower()) <= 1 for query in gaps["queries"])
    assert all(query.count(icp["problem"]) <= 1 for query in gaps["queries"])
    assert all(query.count(phrase) <= 1 and "second sentence" not in query for query in gaps["queries"])
    product = "Latch Latch" if icp["mode"] == "company" else "Bell Bell"
    deduped = autonomous._dedupe_query(f'"{product}" {task} {task} alternatives', icp, [task])
    assert f'"{product}"' in deduped and deduped.count(task) == 1
    for spec in specs:
        long_icp = {**icp, "who": (icp["who"] * 12).replace(" ", "—"), "problem": task * 12}
        hypothesis = autonomous._hypothesis(autonomous.RECIPES[spec["scan"]]["hypothesis_template"],
            long_icp)
        if is_local():
            assert "—" not in hypothesis
            assert autonomous._hypothesis_slots({**icp, "problem": task * 12})["problem"] in hypothesis
            long_snapshot = {**research_snapshot, "journey": {**research_snapshot["journey"], "icp": long_icp}}
            runnable = next(item for item in autonomous.select_runnable(long_snapshot,
                SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1)) if item["scan"] == spec["scan"])
            assert runnable["hypothesis"] == hypothesis
            assert len(runnable["queries"]) <= autonomous.MAX_SEARCHES and runnable["pages"] == autonomous.MAX_PAGES
        else:
            assert len(hypothesis) <= 160 and "—" not in hypothesis


@pytest.mark.parametrize("saved_understanding", ["problem", "offer", "missing"])
def test_first_person_ideas_never_enter_cards_or_provider_queries(research_snapshot, monkeypatch, saved_understanding):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.experiments import autonomous
    from app.gtm import journey
    from app.state import db
    from core.clock import utcnow

    icp = research_snapshot["journey"]["icp"]
    who, problem = icp["who"], icp["problem"]
    raw = f"I run a small app for {who} to help with {problem}. I want my customers to have an easier day."
    fields = {"customer": who, "_answered_fields": {"customer": who}}
    if saved_understanding == "problem":
        fields["problem"] = problem
    elif saved_understanding == "offer":
        fields["offer"] = f"{problem.capitalize()} software for {who}"
    venture_id, workspace_id = research_snapshot["venture_id"], research_snapshot["workspace_id"]
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = raw
        session.add(IdeaSession(id="pitch_session", venture_id=venture_id, workspace_id=workspace_id,
            case="know", state="reviewing", current_revision_id="pitch_revision",
            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(IdeaRevision(id="pitch_revision", session_id="pitch_session", venture_id=venture_id,
            workspace_id=workspace_id, rev=1, source="founder", structured=json.dumps(fields), created_at=utcnow()))
        session.commit()
    icp.update(who=raw, problem=raw,
        competitors=[{"name": raw}, {"name": "First alternative"}, {"name": "Second alternative"}],
        segments=[raw, *[f"{who} group {index}" for index in range(4)]])
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: deepcopy(research_snapshot["journey"]))
    monkeypatch.setattr(autonomous, "autopilot_enabled", lambda: True)
    snapshot = autonomous._snapshot(venture_id)
    queries = []

    async def search(query, **kwargs):
        queries.append(query)
        return []

    provider = SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1, search=search)
    specs = [spec for spec in autonomous.select_runnable(snapshot, provider) if spec.get("runner") == "scan"]
    monkeypatch.setattr(autonomous, "available", lambda venture_id: specs)
    texts = []
    if saved_understanding == "missing" or is_local() and saved_understanding == "offer":
        assert not specs and not queries
        texts.extend(autonomous._hypothesis(recipe["hypothesis_template"], {"who": raw, "problem": raw})
            for recipe in autonomous.RECIPES.values() if recipe.get("hypothesis_template"))
    else:
        assert {spec["scan"] for spec in specs} == {kind for kind, recipe in autonomous.RECIPES.items()
            if recipe.get("query_builder")}
        for spec in specs:
            assert autonomous._hypothesis_slots(spec["icp"])["task"].casefold() == problem.casefold()
            prepared = autonomous.prepare(venture_id, spec["id"])
            texts.extend([spec["hypothesis"], spec["method"], prepared["body_md"],
                json.dumps(prepared["actions"]), *spec["queries"]])
            asyncio.run(autonomous._scan(spec, provider))
        assert queries == [query for spec in specs for query in [*(query for variant in spec["variants"] for query in variant["params"]["queries"]),
                                                                 *spec.get("broader_queries", [])]]
    for text in texts + queries:
        assert all(forbidden.casefold() not in text.casefold() for forbidden in ("I run", "I want", "my ", raw))
    with db()() as session:
        assert session.get(Venture, venture_id).raw_idea == raw
        assert json.loads(session.get(IdeaRevision, "pitch_revision").structured) == fields


@pytest.mark.parametrize("execution", ["single", "segments", "trend", "cached", "recorded"])
def test_confirmation_describes_actual_work_without_embedding_hypothesis(research_snapshot, monkeypatch, execution):
    from app.core.local_mode import is_local
    from app.experiments import autonomous

    icp = research_snapshot["journey"]["icp"]
    icp["segments"] = [f"{icp['who']} group {index}" for index in range(3)]
    specs = autonomous.select_runnable(research_snapshot, SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1))
    scan = execution if execution in {"segments", "trend"} else "community"
    spec = next(spec for spec in specs if spec["scan"] == scan)
    if execution == "cached":
        spec = {**spec, "queries": [], "pages": 0, "reused_demand": {"result": {}}}
    elif execution == "recorded":
        spec = {**spec, "runner": "observe"}
    monkeypatch.setattr(autonomous, "available", lambda venture_id: [spec])
    reply = autonomous.prepare(spec["venture_id"], spec["id"])
    body = reply["body_md"]
    assert spec["hypothesis"] not in body and " for " + spec["hypothesis"] not in body
    if is_local():
        run, = reply["actions"]
        assert run["action"] == "experiment.run" and "confirmation" not in run
        assert run["payload"]["fingerprint"] == spec["fingerprint"]
        assert run["payload"]["ceiling_micro"] == spec["ceiling_micro"]
    else:
        assert spec["hypothesis"] in " ".join(reply["actions"][0]["confirmation"]["consequences"])
    if execution == "cached":
        assert "reuse your saved demand scan" in body and "without new searches" in body
    elif execution == "recorded":
        assert "read your recorded results." in body
    else:
        assert f"up to {len(spec['queries'])} public queries" in body
        assert f"up to {spec['pages'] * len(spec['variants'])} source pages" in body
        assert f"across {len(spec['variants'])} test variation" in body
        if execution == "trend":
            assert "audience" not in body


def test_empty_scans_are_honest_and_give_a_specific_next_step(research_snapshot, monkeypatch):
    from app.experiments import autonomous

    icp = research_snapshot["journey"]["icp"]
    icp.update(competitors=[{"name": "First alternative"}, {"name": "Second alternative"}],
        segments=[f"{icp['who']} group {index}" for index in range(4)])
    monkeypatch.setattr(autonomous, "autopilot_enabled", lambda: True)
    specs = [spec for spec in autonomous.select_runnable(research_snapshot, SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1))
        if spec.get("runner") == "scan"]
    for spec in specs:
        buckets = [{**variant, "sources": [], "retrieved_sources": 0} for variant in spec["variants"]]
        for retrieved in (0, 3):
            for bucket in buckets:
                bucket["retrieved_sources"] = retrieved
            result = autonomous._scan_result(spec, buckets, 0, 0)
            assert result["outcome"] == "inconclusive" and not result["sources"]
            if spec["scan"] in {"demand", "competitors"}:
                assert "does not tell you there is no demand" in result["insight"]
            else:
                assert "Absence of evidence" in result["insight"]
            assert 'Say "find customers"' in result["insight"]
            assert "You have evidence" not in result["insight"] and "You have public reasons" not in result["insight"]


def test_result_copy_is_plain_and_keeps_evidence_details(research_snapshot, monkeypatch):
    from app.experiments import autonomous

    icp = research_snapshot["journey"]["icp"]
    icp.update(competitors=[{"name": "First alternative"}, {"name": "Second alternative"}],
        segments=[f"{icp['who']} group {index}" for index in range(4)])
    monkeypatch.setattr(autonomous, "autopilot_enabled", lambda: True)
    specs = [spec for spec in autonomous.select_runnable(research_snapshot, SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1))
        if spec.get("runner") == "scan"]
    for spec in specs:
        buckets = [{**variant, "sources": [], "retrieved_sources": 0} for variant in spec["variants"]]
        result = autonomous._scan_result(spec, buckets, 7, 1)
        body = result["insight"]
        assert all(term not in body.lower() for term in (
            "support:", "refute:", "measured", "min_events", "source request failed", ">=", "<=",
            "you have evidence of workaround", "you have public reasons"))
        assert body.count("Next:") == 1
        assert 'Say "find customers"' in body
        assert result["outcome"] == "inconclusive" and not result["sources"]
        assert result["failed_sources"] == 1 and result["actual_micro"] == 7
        assert "events" in result["measured"] and "by_variant" in result
    demand = next(spec for spec in specs if spec["scan"] == "demand")
    source = {"url": "https://public.example/discussion", "title": "Public discussion",
        "quote": f"People struggle with {icp['problem']}.", "published_at": "2026-10-01"}
    result = autonomous._scan_result(demand, [{**demand["variants"][0],
        "sources": [source], "retrieved_sources": 1}], 5, 0)
    assert source["quote"] in result["insight"] and result["insight"].count("Next:") == 1
    assert result["measured"]["events"] == 1 and result["sources"] == [source]
    for measured in ({"events": 0}, {"events": 6, demand["metric"]: 6},
                     {"events": 6, demand["metric"]: 0}):
        summary = autonomous._outcome_line(demand, measured)
        assert all(term not in summary.lower() for term in ("support:", "refute:", "measured", ">=", "<="))
    for scan in ("page_test", "channel_test", "tool_evidence"):
        summary = autonomous._outcome_line({**demand, "scan": scan}, {"events": 0}, verdict="inconclusive")
        assert "not have enough evidence" in summary and summary.count("Next:") == 1
        assert all(term not in summary.lower() for term in ("support:", "refute:", "measured", ">=", "<="))
    observed = autonomous._observe({"scan": "outreach", "venture_id": research_snapshot["venture_id"],
        "angles": [{"title": icp["who"], "sent": 0, "replies": 0, "reply_rate": 0}]})
    assert "0/0 human replies" in observed["insight"] and observed["insight"].count("Next:") == 1
    assert "not a randomized winner" in observed["insight"] and observed["actual_micro"] == 0


@pytest.fixture
def market(research_snapshot, monkeypatch, venture):
    from app.conversion import waitlist
    from app.experiments import autonomous
    from app.gtm import journey
    from app.signals.research import ScrapedText, SearchResults

    snapshot = research_snapshot
    icp = snapshot["journey"]["icp"]
    icp["competitors"] = [{"name": name} for name in (
        ("Document Harbor", "Freight Desk") if icp["mode"] == "company" else ("Trail Compass", "Cycle Note"))]
    calls, pages = [], {}

    class Provider:
        name = "firecrawl"
        search_micro = 1
        scrape_micro = 1
        respond = staticmethod(lambda query, kwargs: [])

        async def search(self, query, **kwargs):
            calls.append(("search", query, kwargs))
            hits = self.respond(query, kwargs)
            pages.update({hit["url"]: hit for hit in hits})
            return SearchResults(deepcopy(hits), provider_rows=len(hits), rejected_shape=0, rejected_missing_url=0,
                                 usage={"firecrawl_self_hosted": True})

        async def scrape(self, url):
            calls.append(("scrape", url))
            hit = pages[url]
            return ScrapedText(hit["snippet"], metadata={"published_at": hit.get("published_at"),
                "author": hit.get("author"), **hit.get("metadata", {})}, usage={"firecrawl_self_hosted": True})

    provider = Provider()
    from app.core.local_mode import is_local
    if is_local():
        def measured(method, price, purpose):
            async def call(*args, **kwargs):
                return await autonomous.metered.call(lambda: method(*args, **kwargs), purpose=purpose,
                    provider=provider.name, payer="founder", estimate_micro=price,
                    workspace_id=snapshot["workspace_id"], venture_id=snapshot["venture_id"],
                    actual_micro=lambda response: autonomous.metered.firecrawl_actual_micro(response, price))
            return call

        monkeypatch.setattr(provider, "search", measured(provider.search, provider.search_micro, "research.search"))
        monkeypatch.setattr(provider, "scrape", measured(provider.scrape, provider.scrape_micro, "research.fetch"))
    original_snapshot = autonomous._snapshot
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: deepcopy(snapshot["journey"]))
    monkeypatch.setattr(waitlist, "status", lambda *args, **kwargs: {})
    monkeypatch.setattr(autonomous, "_provider", lambda: provider)
    monkeypatch.setattr(autonomous, "_snapshot", lambda venture_id: deepcopy(snapshot))
    client, venture_id, workspace_id = venture
    return client, venture_id, workspace_id, snapshot["journey"], {}, provider, calls, snapshot, original_snapshot


def complaints(snapshot, count, *, days=2, group="research", prefix="sample"):
    from core.clock import utcnow

    icp = snapshot["journey"]["icp"]
    return [{"url": f"https://www.reddit.com/r/{group}/comments/{prefix}{index}/task/",
             "title": f"{icp['who']}: {icp['problem']}", "author": f"researcher_{index}",
             "snippet": f"I struggle with {icp['problem']} and waste hours each week.",
             "published_at": (utcnow() - timedelta(days=days)).isoformat()} for index in range(count)]


@pytest.fixture
def run_recipe(market, credit_meter):
    from app.chat.store import list_messages
    from app.core.local_mode import is_local
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db

    _client, venture_id, _workspace_id, _view, _landing, _provider, calls, _snapshot, _app = market

    def execute(scan):
        spec = next(spec for spec in autonomous.available(venture_id) if spec["scan"] == scan)
        before_calls, before_meters = len(calls), len(credit_meter)
        prepared = autonomous.prepare(venture_id, spec["id"])
        run = prepared["actions"][0]
        if is_local():
            assert run["action"] == "experiment.run" and "confirmation" not in run
            payload = run["payload"]
            assert "Maximum cost:" not in prepared["body_md"]
        else:
            card = run["confirmation"]
            payload = card["resulting_state"]
            assert card["action"] == "experiment.run"
        assert payload["ceiling_micro"] == spec["ceiling_micro"]
        assert payload["fingerprint"] == spec["fingerprint"]
        assert len(spec["hypothesis"]) <= 160 and spec["runner"] == "scan" and spec["approval"] == "once"
        assert spec["question"].startswith("Q") and spec["catalog_key"].startswith("E")
        assert spec["success_threshold"] and spec["failure_threshold"]
        assert spec["requires"] == (["research"] if is_local() else ["firecrawl"])
        assert all(key in spec for key in ("assumption", "variable", "variants", "runner_params", "metric", "min_sample",
            "sample_unit", "max_duration_days", "founder_minutes", "compliance", "next_suggestion", "direction"))
        reply = asyncio.run(autonomous.run(venture_id, payload))
        assert reply["kind"] == "experiment_running" and len(calls) == before_calls and len(credit_meter) == before_meters
        run_jobs()
        with db()() as session:
            row = session.get(Experiment, spec["id"])
            context = json.loads(row.context)
            assert row.state == "concluded", context
            assert context["spec"]["runner_params"] == spec["runner_params"] and "pending_result" not in context
        result = context["result"]
        terminal = [message for message in list_messages(venture_id) if message["kind"] == "experiment_result"][-1]
        assert len(terminal["citations"]) == len(result["sources"]) and "Outcome:" in terminal["body_md"]
        assert all(all(citation.get(key) == value for key, value in source.items())
                   for citation, source in zip(terminal["citations"], result["sources"]))
        assert "—" not in terminal["body_md"]
        if scan not in {"demand", "competitors"}:
            assert "you" in terminal["body_md"].lower()
        charged = credit_meter[before_meters:]
        assert sum(meter.actual_micro for meter in charged) == result["actual_micro"] <= spec["ceiling_micro"]
        assert sum(meter.estimate_micro for meter in charged) <= spec["ceiling_micro"]
        assert all(meter.settle_result is not None for meter in charged)
        return spec, result

    return execute


def assert_empty_is_inconclusive(spec, provider):
    from app.experiments import autonomous

    provider.respond = lambda query, kwargs: []
    result = asyncio.run(autonomous._scan(spec, provider))
    assert result["outcome"] == "inconclusive" and not result["sources"]
    assert "Absence of evidence" in result["insight"]


def test_community_map_groups_links_and_reuses_demand_without_extra_credits(market, run_recipe):
    from app.experiments import autonomous

    _client, venture_id, _wid, _view, _landing, provider, calls, snapshot, original_snapshot = market
    hits = complaints(snapshot, 4, group="primary") + complaints(snapshot, 2, group="secondary", prefix="secondary")
    provider.respond = lambda query, kwargs: hits
    fresh = next(spec for spec in autonomous.available(venture_id) if spec["scan"] == "community")
    demand_spec, demand_result = run_recipe("demand")
    snapshot["demand_result"] = original_snapshot(venture_id)["demand_result"]
    assert snapshot["demand_result"]["id"] == demand_spec["id"] and snapshot["demand_result"]["result"] == demand_result
    before = len(calls)
    spec, result = run_recipe("community")
    assert spec["ceiling_micro"] == result["actual_micro"] == 0 and len(calls) == before
    assert result["outcome"] == "supported" and result["measured"]["primary_community_share"] == 4 / 6
    assert [group["count"] for group in result["details"]] == [4, 2]
    assert all(group["rules_url"].endswith("/about/rules/") and group["links"] for group in result["details"])
    assert_empty_is_inconclusive(fresh, provider)


def test_trend_keeps_identical_queries_and_dated_windows_for_both_domains(market, run_recipe):
    from app.experiments import autonomous

    _client, _vid, _wid, _view, _landing, provider, calls, snapshot, _app = market
    recent = complaints(snapshot, 4)
    year = recent + complaints(snapshot, 4, days=200, prefix="year")
    year[-1]["published_at"] = None
    year[-1]["snippet"] = "Posted 200 days ago\n" + year[-1]["snippet"]
    older = complaints(snapshot, 2, days=500, prefix="older")
    undated = {**recent[0], "url": recent[0]["url"].replace("sample", "undated"), "published_at": None}
    provider.respond = lambda query, kwargs: (recent + [undated] if kwargs["tbs"] == "qdr:m3"
        else year if kwargs["tbs"] == "qdr:y" else older)
    spec, result = run_recipe("trend")
    assert result["outcome"] == "supported" and result["measured"]["recent_share"] == .5
    assert [len(bucket["sources"]) for bucket in result["by_variant"]] == [4, 8, 2]
    assert result["by_variant"][0]["qualifying_per_result"] == .8
    assert len({tuple(variant["params"]["queries"]) for variant in spec["variants"]}) == 1
    assert any(call[0] == "search" and call[2]["tbs"].startswith("cdr:1") for call in calls)

    def partial(query, kwargs):
        if kwargs["tbs"] == "qdr:y":
            raise RuntimeError("Unavailable window")
        return recent if kwargs["tbs"] == "qdr:m3" else older

    provider.respond = partial
    incomplete = asyncio.run(autonomous._scan(spec, provider))
    assert incomplete["outcome"] == "inconclusive" and incomplete["failed_sources"] == 3
    assert_empty_is_inconclusive(spec, provider)


def test_segment_heatmap_compares_only_supplied_segments(market, run_recipe):
    from app.experiments import autonomous

    _client, venture_id, _wid, view, _landing, provider, _calls, snapshot, _app = market
    assert "segments" not in {spec["scan"] for spec in autonomous.available(venture_id)}
    view["icp"]["segments"] = [f"night-shift {view['icp']['who']}", f"daytime {view['icp']['who']}"]
    view["icp"]["segments"].append(view["icp"]["segments"][0].upper())
    provider.respond = lambda query, kwargs: complaints(snapshot, 6 if "night" in query else 2,
        prefix="night" if "night" in query else "day")
    spec, result = run_recipe("segments")
    assert result["outcome"] == "supported" and result["measured"]["lead_ratio"] == 3
    assert [len(bucket["sources"]) for bucket in result["by_variant"]] == [6, 2]
    assert [variant["label"] for variant in spec["variants"]] == view["icp"]["segments"][:2]
    assert spec["ceiling_micro"] == 18
    assert_empty_is_inconclusive(spec, provider)
    view["icp"]["segments"].append(f"seasonal {view['icp']['who']}")
    comparison = next(spec for spec in autonomous.available(venture_id) if spec["scan"] == "segments")
    for smallest, expected in ((1, "inconclusive"), (5, "refuted")):
        provider.respond = lambda query, kwargs: complaints(snapshot,
            6 if "night" in query else smallest if "seasonal" in query else 5,
            prefix="night" if "night" in query else "seasonal" if "seasonal" in query else "day")
        compared = asyncio.run(autonomous._scan(comparison, provider))
        assert compared["outcome"] == expected and compared["measured"]["lead_ratio"] == 1.2
        assert compared["measured"]["spread_ratio"] == 6 / smallest


def test_postings_require_recent_matching_duties_and_extract_off_loop(market, run_recipe, monkeypatch):
    from app.core.local_mode import is_local
    from app.experiments import autonomous

    _client, _vid, _wid, _view, _landing, provider, _calls, snapshot, _app = market
    hits = complaints(snapshot, 5)
    for index, hit in enumerate(hits):
        hit.update(url=f"https://jobs.example/positions/{index}", title="Operations coordinator",
            snippet=f"Company: Employer {index}\nRole: Operations coordinator\nResponsibilities: {snapshot['journey']['icp']['problem']} manually.\nApply now.")
    hits += [{**hits[0], "url": "https://jobs.example/positions/old", "published_at": complaints(snapshot, 1, days=120)[0]["published_at"]},
             {**hits[0], "url": "https://jobs.example/positions/undated", "published_at": None}]
    hits[0]["metadata"] = {"datePosted": hits[0]["published_at"]}
    hits[0]["published_at"] = None
    provider.respond = lambda query, kwargs: hits
    threads, original = [], autonomous._posting

    def posting(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(autonomous, "_posting", posting)
    loop_thread = threading.get_ident()
    spec, result = run_recipe("postings")
    assert result["outcome"] == "supported" and result["measured"]["recent_postings"] == 5
    assert all(source["company"].startswith("Employer") and source["role"] == "Operations coordinator" and source["duties"] for source in result["sources"])
    assert threads and all(thread != loop_thread for thread in threads)
    alternatives = autonomous.select_runnable(snapshot, SimpleNamespace(name="exa", search_micro=1, scrape_micro=1))
    if is_local():
        alternative = next(item for item in alternatives if item["scan"] == "postings")
        assert alternative["provider"] == "exa" and alternative["pages"] == spec["pages"]
        assert alternative["ceiling_micro"] == spec["ceiling_micro"]
    else:
        assert alternatives == []
    assert_empty_is_inconclusive(spec, provider)


def test_review_mining_clusters_once_and_counts_only_sourced_cross_competitor_themes(market, run_recipe, monkeypatch):
    from app.core.local_mode import is_local
    from app.experiments import autonomous

    _client, _vid, _wid, view, _landing, provider, _calls, snapshot, _app = market
    hits = complaints(snapshot, 6)
    for index, hit in enumerate(hits):
        name = view["icp"]["competitors"][index // 3]["name"]
        hit["title"] += " " + name
        hit["snippet"] = f"{name}: 1-star review.\n" + hit["snippet"]
        hit["snippet"] += "\nRelated alternative: " + view["icp"]["competitors"][1 - index // 3]["name"]
    hits += [{**hits[0], "url": hits[0]["url"].replace("sample", "undated"), "published_at": None},
             {**hits[0], "url": hits[0]["url"].replace("sample", "positive"), "snippet": hits[0]["snippet"].replace("1-star", "5-star")},
             {**hits[0], "url": hits[0]["url"].replace("sample", "mixed"),
              "snippet": "5-star review.\n" + complaints(snapshot, 1)[0]["snippet"] + "\n1-star review.\nNo personal experience was recorded."}]
    provider.respond = lambda query, kwargs: hits
    model_calls = []
    if is_local():
        monkeypatch.setattr("app.credits.estimate.estimate_llm_purpose_micro", lambda *args, **kwargs: 7)

    async def cluster(messages, **kwargs):
        model_calls.append((messages, kwargs))
        assert kwargs["max_tokens"] == 768 and len(json.loads(messages[1]["content"])) == 6
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"themes": [
            {"label": "Manual coordination", "source_indexes": [0, 1, 3, 4]},
            {"label": "Repeated rework", "source_indexes": [2, 5]},
        ]})))])

        async def respond():
            return response

        return await autonomous.metered.call(respond, purpose=kwargs["purpose"], workspace_id=kwargs["workspace_id"],
            venture_id=kwargs["venture_id"], provider="configured_model", estimate_micro=7, actual_micro=lambda response: 7,
            request_id=kwargs["request_id"], before_meter_exit=kwargs["before_meter_exit"])

    monkeypatch.setattr(autonomous.metered, "llm", cluster)
    spec, result = run_recipe("reviews")
    assert len(model_calls) == 1 and result["outcome"] == "supported"
    assert result["measured"]["theme_share"] == 4 / 6 and len(result["sources"]) == 6
    assert result["actual_micro"] == 15 and spec["ceiling_micro"] >= result["actual_micro"]
    for indexes in ([0, 0], [False], [99]):
        with pytest.raises(ValueError):
            autonomous._review_result(spec, result, json.dumps({"themes": [{"label": "Invalid", "source_indexes": indexes}]}))
    assert_empty_is_inconclusive(spec, provider)
    assert len(model_calls) == 1


def test_pricing_ladder_preserves_explicit_units_and_rejects_financial_metrics(market, run_recipe):
    from app.experiments import autonomous
    from core.clock import utcnow

    _client, _vid, _wid, view, _landing, provider, _calls, snapshot, _app = market
    hits = complaints(snapshot, 4)
    names = [item["name"] for item in view["icp"]["competitors"]]
    unit = "user" if view["icp"]["mode"] == "company" else "booking"
    for index, hit in enumerate(hits):
        hit.update(url=f"https://prices.example/{index}", title=names[index % 2],
                   snippet=f"{names[index % 2]} supports {view['icp']['problem']}.\nPricing: €{40 if index == 0 else 20} per month per {unit}.")
    hits[2]["snippet"] = f"{names[0]} earned €80000 in revenue per month."
    hits[3]["snippet"] = f"{names[1]} salary costs €3000 per month."
    provider.respond = lambda query, kwargs: hits
    spec, result = run_recipe("pricing")
    assert result["outcome"] == "supported" and result["measured"]["priced_products"] == 2
    assert [point["price"] for point in result["details"]] == ["€20", "€40"]
    assert all(point["billing_period"] == "month" and point["source"] for point in result["details"])
    assert all(point["billing_unit"] == unit for point in result["details"])
    assert len(result["sources"]) == 2
    mixed = {"url": "https://prices.example/comparison", "title": f"{names[0]} vs {names[1]}",
             "snippet": f"{names[0]} and {names[1]} alternatives.\n{names[1]} plan costs €60 per month (docs: https://help.example/users)."}
    mixed_point = autonomous._price(spec["icp"], mixed, mixed["snippet"], {}, utcnow(), {})["price_point"]
    assert mixed_point["product"] == names[1] and mixed_point["billing_unit"] == "not stated"
    short_name = names[0].split()[0]
    unrelated = {"url": "https://prices.example/unrelated", "title": short_name + "ology",
                 "snippet": f"{short_name}ology plan costs €40 per month."}
    assert autonomous._price({**spec["icp"], "competitors": [{"name": short_name}]},
                             unrelated, unrelated["snippet"], {}, utcnow(), {}) is None
    assert_empty_is_inconclusive(spec, provider)


def test_workarounds_keep_paid_and_unknown_prices_without_inventing_anchors(market, run_recipe):
    from app.experiments import autonomous
    from core.clock import utcnow

    _client, _vid, _wid, view, _landing, provider, _calls, snapshot, _app = market
    hits = complaints(snapshot, 3)
    for index, hit in enumerate(hits):
        hit.update(url=f"https://templates.example/{index}", title=f"Task spreadsheet template {index}",
            snippet=f"Spreadsheet template for {view['icp']['problem']}.\n" + (f"Template plan costs €{12 + index} one-time." if index < 2 else "A spreadsheet template with no published price."))
    hits[0]["snippet"] += "\nPremium template plan costs €30 one-time."
    hits[2]["snippet"] += "\nStarter template plan costs €0 one-time."
    provider.respond = lambda query, kwargs: hits
    spec, result = run_recipe("workarounds")
    assert result["outcome"] == "supported" and result["measured"]["paid_workarounds"] == 2
    assert len(result["sources"]) == 3 and result["sources"][2]["price_point"]["price"] == "€0"
    assert len(result["details"]) == 4
    hits[2]["snippet"] = f"Spreadsheet template for {view['icp']['problem']}. No price was published."
    unknown = autonomous._workaround(spec["icp"], hits[2], hits[2]["snippet"], {}, utcnow(), {})
    assert unknown["price_point"] is None
    assert_empty_is_inconclusive(spec, provider)


def test_redteam_requires_repeated_sourced_failure_causes_not_absence(market, run_recipe):
    _client, _vid, _wid, view, _landing, provider, _calls, snapshot, _app = market
    hits = complaints(snapshot, 3)
    for index, hit in enumerate(hits):
        hit.update(url=f"https://postmortems.example/{index}", title=f"Public shutdown account {index}",
            snippet=f"We built a service for {view['icp']['problem']}.\nWe shut down because customer adoption remained too costly.")
    hits[2]["snippet"] = f"We shut down a service for {view['icp']['problem']}. No cause was published."
    provider.respond = lambda query, kwargs: hits
    spec, result = run_recipe("redteam")
    assert result["outcome"] == "supported" and result["measured"]["shared_cause_count"] == 2
    assert len(result["sources"]) == 2 and result["details"][0][0]["cause"] == "customer adoption remained too costly"
    assert "your plan" in result["insight"] and "not a verdict" in spec["method"]
    assert_empty_is_inconclusive(spec, provider)


@pytest.mark.parametrize("who,problem", [
    ("independent pottery studios in Portland", "Pottery studios in Portland need a way to fill last-minute wheel class cancellations from their waitlist automatically."),
    ("community choirs in Accra", "Community choirs in Accra need a way to reschedule rehearsal rooms with their members every week."),
])
def test_shortened_hypothesis_never_ends_on_a_connector(who, problem):
    from app.core.local_mode import is_local
    from app.experiments import autonomous

    text = autonomous._hypothesis(autonomous.RECIPES["pricing"]["hypothesis_template"], {"who": who, "problem": problem})
    if is_local():
        assert autonomous._hypothesis_slots({"who": who, "problem": problem})["task"] in text
    else:
        assert len(text) <= 160
    assert not re.search(r"\b(?:from|their|the|of|to|with|for|and)\s+(?:give|gives)\b", text)
