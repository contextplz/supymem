"""Zero-contact research and admission stay bounded across unrelated markets."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.company_os_harness import local_owner_clients

from tests.unit.test_experiment_scan_recipes import research_snapshot
from tests.unit.test_autonomous_experiments import run_jobs, venture


SCANS = {"paying_for_bad_fix", "graveyard", "search_demand", "distribution",
         "audience_owners", "adjacent_swap", "anti_pitch", "unit_economics"}


def set_enabled(value):
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"run_for_me_experiments": value}, {})


def segments(snapshot):
    return (["customs agents", "warehouse coordinators", "shipping clerks"]
            if snapshot["journey"]["icp"]["mode"] == "company" else
            ["commuter cyclists", "touring cyclists", "trail cyclists"])


@pytest.mark.parametrize("flags", [None, {}, {"experiments_autopilot_v1": False},
    {"experiments_autopilot_v1": 1}, {"experiments_autopilot_v1": "true"}, {"experiments_autopilot_v1": True}])
def test_new_recipes_require_literal_policy_flag(research_snapshot, monkeypatch, flags):
    from app.core.local_mode import is_local
    from app.credits.estimate import estimate_llm_purpose_micro
    from app.experiments import autonomous

    monkeypatch.setattr(autonomous, "get_policy", lambda key: flags)
    snapshot = {**research_snapshot, "segments": segments(research_snapshot)}
    consumer = snapshot["journey"]["icp"]["mode"] == "consumer"
    snapshot["journey"] = {"icp": {**snapshot["journey"]["icp"], "audience_type": "B2C" if consumer else "B2B"}}
    specs = autonomous.select_runnable(snapshot, SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1))
    enabled = is_local() or isinstance(flags, dict) and flags.get("experiments_autopilot_v1") is True
    assert {spec["scan"] for spec in specs} & SCANS == (SCANS if enabled else set())
    base = {"demand", "competitors", "community", "trend", "segments", "postings", "pricing", "workarounds", "redteam"}
    if is_local() and consumer:
        base.discard("postings")
    assert {spec["scan"] for spec in specs if spec.get("runner") == "scan"} - SCANS == base
    assert all(spec.get("state") == "proposed" for spec in specs)
    monkeypatch.setattr(autonomous, "available", lambda vid: specs)
    for spec in specs:
        if spec["scan"] not in SCANS:
            continue
        assert spec["approach"] in {"sensible", "red-pill"}
        assert spec["compliance"] == ["public_sources"] and spec["approval"] == "once"
        assert spec["pages"] == 6
        assert all(1 <= len(arm["params"]["queries"]) <= 3 for arm in spec["variants"])
        assert spec["ceiling_micro"] == spec["scan_ceiling_micro"] + spec["llm_ceiling_micro"]
        expected_llm = estimate_llm_purpose_micro("experiment.anti_pitch", prompt_tokens=6000, max_tokens=768) if spec["scan"] == "anti_pitch" else 0
        assert spec["llm_ceiling_micro"] == expected_llm
        confirmation = autonomous.prepare(spec["venture_id"], spec["id"])
        assert "Hypothesis:" not in json.dumps(confirmation)
        assert f"across {len(spec['variants'])} " in confirmation["body_md"]
    assert "adjacent_swap" not in {spec["scan"] for spec in autonomous.select_runnable(
        research_snapshot, SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1))}


@pytest.mark.parametrize("kind", sorted(SCANS))
def test_new_recipes_read_only_bounded_sourced_evidence(research_snapshot, monkeypatch, kind):
    from app.experiments import autonomous
    from app.signals.research import ScrapedText, SearchResults

    monkeypatch.setattr(autonomous, "get_policy", lambda key: {"experiments_autopilot_v1": True})
    snapshot = {**research_snapshot, "segments": segments(research_snapshot)}
    problem = snapshot["journey"]["icp"]["problem"]
    calls = []
    texts = {
        "paying_for_bad_fix": f"I pay $90 to an agency for a manual workaround for {problem}.",
        "graveyard": f"Our startup for {problem} shut down because acquisition was too expensive.",
        "search_demand": f"How can I handle {problem}?",
        "distribution": f"Customers find our {problem} service through referrals.",
        "audience_owners": f"Our newsletter covers {problem}. We reach 12k subscribers.",
        "anti_pitch": f"I prefer the spreadsheet for {problem} because it is familiar and reliable.",
        "adjacent_swap": f"I struggle with {problem} every week.",
    }

    async def search(query, **kwargs):
        calls.append(("search", query))
        label = next((label for label in segments(snapshot) if label in query), "all")
        hits = []
        for index in range(8):
            text = (f"The {problem} software plan costs USD 50 per month." if index < 3 else
                    f"CPC benchmark: USD 2.00 for {problem}.") if kind == "unit_economics" else texts[kind]
            hits.append({"url": f"https://research.example/{label.replace(' ', '-')}/{index}",
                         "title": f"Public account about {problem}", "snippet": text})
        return SearchResults(hits, provider_rows=8, rejected_shape=0, rejected_missing_url=0,
                             usage={"firecrawl_self_hosted": True})

    async def scrape(url):
        calls.append(("read", url))
        index = int(url.rsplit("/", 1)[1])
        text = (f"The {problem} software plan costs USD 50 per month." if index < 3 else
                f"CPC benchmark: USD 2.00 for {problem}.") if kind == "unit_economics" else texts[kind]
        return ScrapedText(text, metadata={}, usage={"firecrawl_self_hosted": True})

    monkeypatch.setattr(autonomous, "_complaint", lambda icp, hit, text, metadata, now, context:
        {**hit, "quote": text, "published_at": now.isoformat()})
    provider = SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1, search=search, scrape=scrape)
    spec = next(spec for spec in autonomous.select_runnable(snapshot, provider) if spec["scan"] == kind)
    result = asyncio.run(autonomous._scan(spec, provider))
    assert result["sources"] and all(source["quote"] for source in result["sources"])
    assert len([call for call in calls if call[0] == "search"]) == len(spec["queries"])
    assert len([call for call in calls if call[0] == "read"]) == 6 * len(spec["variants"])
    assert result["actual_micro"] == len(spec["queries"]) + 6 * len(spec["variants"])
    assert result["actual_micro"] <= spec["ceiling_micro"] and "—" not in result["insight"]
    assert result["outcome"] == autonomous.outcome(spec, result["measured"])
    assert "Outcome:" not in result["insight"] and "min_events" not in result["insight"]
    if kind == "anti_pitch":
        assert result["outcome"] == "inconclusive" and result["measured"][spec["metric"]] is None
    elif kind == "adjacent_swap":
        assert len(result["by_variant"]) == 3 and result["measured"][spec["metric"]] == 1
        assert result["outcome"] == "refuted"
    else:
        assert result["outcome"] == "supported"
    if kind == "unit_economics":
        assert result["measured"][spec["metric"]] == pytest.approx(4.8)
        assert "estimate" in result["insight"] and "assumes 12 months" in result["insight"]
        incompatible = deepcopy(result["sources"])
        for source in incompatible:
            if source.get("cpc"):
                source["cpc"] = "EUR 2.00"
        assert autonomous._economics_estimates(incompatible) == []
        ambiguous = deepcopy(result["sources"])
        for source in ambiguous:
            for point in source.get("price_points", []):
                point["price"] = "$50"
        assert autonomous._economics_estimates(ambiguous) == []
    if kind == "paying_for_bad_fix":
        hit = result["sources"][0]
        for text in (f"I would pay $90 for an agency workaround for {problem}.",
                     f"I pay $0 for an agency workaround for {problem}."):
            assert autonomous._paid_fix(spec["icp"], hit, text, {}, None, {}) is None
        assert autonomous._paid_fix(spec["icp"], hit,
            f"I spend 8 hours on a spreadsheet workaround for {problem}.", {}, None, {})["spend_kind"] == "time"
    if kind == "audience_owners":
        assert all(source["size_estimate"] == 12000 for source in result["sources"])
        unknown = autonomous._audience_owner(spec["icp"], result["sources"][0],
            f"Our newsletter covers {problem}.", {}, None, {})
        assert unknown["size_estimate"] is None
        unknown_result = autonomous._scan_result(spec, [{"key": "all", "label": spec["icp"]["who"],
            "sources": [{**unknown, "url": f"https://research.example/unknown/{index}"} for index in range(3)],
            "retrieved_sources": 3}], 3, 0)
        assert unknown_result["outcome"] == "inconclusive" and unknown_result["measured"][spec["metric"]] is None
        proposed = autonomous._audience_owner(spec["icp"], result["sources"][0],
            f"Our newsletter about {problem} aims to reach 12k subscribers.", {}, None, {})
        assert proposed["size_estimate"] is None
    if kind == "distribution":
        assert autonomous._distribution(spec["icp"], result["sources"][0],
            f"We could get customers for {problem} through referrals.", {}, None, {}) is None
    empty = autonomous._scan_result(spec, [{"key": arm["key"], "label": arm["label"],
        "sources": [], "retrieved_sources": 0} for arm in spec["variants"]], 0, 0)
    assert empty["outcome"] == "inconclusive"


def test_paid_fix_counts_only_factual_spending(research_snapshot):
    from app.experiments import autonomous

    icp = research_snapshot["journey"]["icp"]
    problem = icp["problem"]
    hit = {"url": "https://research.example/workaround", "title": problem}
    for text in (
        f"If I pay $90 for an agency workaround for {problem}, it could help.",
        f"If I paid $90 for an agency workaround for {problem}, it would help.",
        f"I would pay $90 for an agency workaround for {problem}.",
        f"I might spend 8 hours on a spreadsheet workaround for {problem}.",
        f"Suppose I spend 8 hours on a spreadsheet workaround for {problem}.",
        f"I pay $90 for an agency workaround for {problem} only if it helps.",
        f"I pay $90 for an agency workaround for {problem}?",
        f"I don't pay $90 for an agency workaround for {problem}.",
    ):
        assert autonomous._paid_fix(icp, hit, text, {}, None, {}) is None, text
    for verb, amount, kind in (("pay", "$90", "money"), ("paid", "$90", "money"),
                              ("spend", "8 hours", "time"), ("spent", "8 hours", "time")):
        source = autonomous._paid_fix(icp, hit,
            f"I {verb} {amount} on an agency workaround for {problem}.", {}, None, {})
        assert source["spend"] == amount and source["spend_kind"] == kind


def test_audience_owner_sizes_require_factual_claims(research_snapshot):
    from app.experiments import autonomous

    icp = research_snapshot["journey"]["icp"]
    problem = icp["problem"]
    hit = {"url": "https://research.example/community", "title": problem}
    for claim in (
        "does not have 12k subscribers", "has not even 5k readers",
        "doesn't have 12k subscribers", "doesn’t have 12k subscribers",
        "would have 12k subscribers", "might reach 5k readers",
        "will have 12k subscribers", "is hoping to reach 5k readers",
        "plans to reach 12k subscribers", "has planned 12k subscribers",
        "has 12k subscribers if everyone joins",
        "has 12k subscribers?",
    ):
        text = f"Our newsletter covers {problem}. It {claim}."
        source = autonomous._audience_owner(icp, hit, text, {}, None, {})
        assert source["size_estimate"] is None and source["size_quote"] is None, text
    for claim, estimate in (("has 12k subscribers", 12000), ("had 5,000 readers", 5000)):
        source = autonomous._audience_owner(icp, hit,
            f"Our newsletter covers {problem}. It {claim}.", {}, None, {})
        assert source["size_estimate"] == estimate and source["size_quote"]
    source = autonomous._audience_owner(icp, hit,
        f"Our newsletter covers {problem}. It does not have 12k subscribers. We have 5k readers.", {}, None, {})
    assert source["size_estimate"] == 5000 and source["quote"] == "We have 5k readers."


def test_anti_pitch_metered_model_validates_sources_and_checkpoints(research_snapshot, monkeypatch):
    from app.experiments import autonomous

    monkeypatch.setattr(autonomous, "get_policy", lambda key: {"experiments_autopilot_v1": True})
    spec = next(spec for spec in autonomous.select_runnable(research_snapshot,
        SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1)) if spec["scan"] == "anti_pitch")
    problem = spec["icp"]["problem"]
    result = autonomous._scan_result(spec, [{"key": "all", "label": spec["icp"]["who"],
        "sources": [{"url": f"https://research.example/status/{index}", "title": "Current approach",
                     "quote": f"I prefer the spreadsheet for {problem} because it is familiar."} for index in range(2)],
        "retrieved_sources": 2}], 9, 0)
    saved = []
    content = json.dumps({"arguments": [{"reason": "You may face a switching cost because the current approach is familiar.",
                                         "source_indexes": [0, 1]}]})

    async def llm(messages, **kwargs):
        assert kwargs["purpose"] == "experiment.anti_pitch" and kwargs["max_tokens"] == 768
        assert kwargs["request_id"] == "test:status-quo" and "untrusted" in messages[0]["content"]
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        await kwargs["before_meter_exit"](response, {"actual_micro": 2})

    async def checkpoint(result, receipt):
        saved.append(deepcopy(result))

    monkeypatch.setattr(autonomous.metered, "llm", llm)
    reviewed = asyncio.run(autonomous._steelman_status_quo(spec, result, "test", checkpoint))
    assert reviewed["outcome"] == "supported" and reviewed["actual_micro"] == 11
    assert saved == [reviewed] and len(reviewed["details"][0]["sources"]) == 2
    content = json.dumps({"arguments": [{"reason": "You should abandon the idea.", "source_indexes": [99]}]})
    invalid = asyncio.run(autonomous._steelman_status_quo(spec, result, "test", checkpoint))
    assert invalid["outcome"] == "inconclusive" and invalid["review_error"] and invalid["actual_micro"] == 11
    assert invalid["review_error"] in invalid["insight"]
    for index in (True, -1, 2):
        with pytest.raises(ValueError):
            autonomous._anti_pitch_result(spec, result, json.dumps({"arguments": [{"reason": "You may prefer familiarity.", "source_indexes": [index]}]}))
    with pytest.raises(ValueError):
        autonomous._anti_pitch_result(spec, result, json.dumps({"arguments": [{
            "reason": "You should trust https://unrecorded.example/ instead.", "source_indexes": [0, 1]}]}))


@pytest.mark.parametrize("review_kind", ["status_quo", "approach"])
def test_model_backed_recipe_uses_a_configured_route(research_snapshot, monkeypatch, review_kind):
    from app.experiments import autonomous, methods
    from llm_gateway.routing import resolve_tier

    monkeypatch.setattr(autonomous, "get_policy", lambda key: {"experiments_autopilot_v1": True})
    spec = next(spec for spec in autonomous.select_runnable(research_snapshot,
        SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1)) if spec["scan"] == "anti_pitch")
    result = autonomous._scan_result(spec, [{"key": "all", "label": spec["icp"]["who"],
        "sources": [{"url": "https://research.example/current", "title": "Current approach",
            "quote": f"You prefer your current approach to {spec['icp']['problem']} because it is familiar."}],
        "retrieved_sources": 1}], 1, 0)
    routed = []

    async def llm(messages, **kwargs):
        routed.append(resolve_tier(kwargs["purpose"]))
        content = ({"arguments": [{"reason": "You face a learning cost when switching.", "source_indexes": [0]}]}
            if review_kind == "status_quo" else {"flags": [{"kind": "unsupported",
                "summary": "You still need to test this approach with people.", "source_urls": [result["sources"][0]["url"]]}]})
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))])
        await kwargs["before_meter_exit"](response, {"actual_micro": 1})

    async def checkpoint(result, receipt):
        assert result["sources"] and receipt["actual_micro"] == 1

    monkeypatch.setattr(autonomous.metered, "llm", llm)
    review = autonomous._steelman_status_quo if review_kind == "status_quo" else methods.review_approach
    reviewed = asyncio.run(review(spec, result, "route-check", checkpoint))
    assert routed == ["flash"] and reviewed["actual_micro"] == 2


@pytest.mark.parametrize("case", ["max", "same", "cooldown", "changed", "expired", "none", "repeat", "parallel", "off"])
def test_run_admission_enforces_caps_without_breaking_idempotency(research_snapshot, monkeypatch, case):
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db
    from core.clock import utcnow

    flags = {"experiments_autopilot_v1": True}
    monkeypatch.setattr(autonomous, "get_policy", lambda key: flags)
    monkeypatch.setattr(autonomous, "_snapshot", lambda vid: deepcopy(research_snapshot))
    monkeypatch.setattr(autonomous, "_provider", lambda: SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1))
    kind = "demand" if case in {"max", "none", "repeat", "parallel", "off"} else "paying_for_bad_fix"
    spec = next(spec for spec in autonomous.available(research_snapshot["venture_id"]) if spec["scan"] == kind)
    payload = {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]}
    rows = 3 if case in {"max", "off"} else 2 if case in {"repeat", "parallel"} else 1
    with db()() as session:
        for index in range(rows):
            previous = {**spec, "id": f"exp_admission_{index}", "state": "proposed"}
            if case == "changed":
                previous["icp"] = {**spec["icp"], "problem": "different saved inputs"}
            concluded = case in {"cooldown", "changed", "expired"}
            session.add(Experiment(id=previous["id"], venture_id=spec["venture_id"], workspace_id=spec["workspace_id"],
                kind=ledger.KIND, hypothesis=spec["hypothesis"], action=spec["method"],
                state="concluded" if concluded else "running",
                ended_at=utcnow() - timedelta(days=14 if case == "expired" else 13) if concluded else None,
                context=json.dumps({"runner": "system", "spec": previous,
                    **({"outreach_state": "waiting_data"} if case == "same" else {})})))
        session.commit()
    if case == "off":
        flags["experiments_autopilot_v1"] = False
        set_enabled(False)
    if case == "parallel":
        other = next(item for item in autonomous.available(spec["venture_id"]) if item["scan"] == "pricing")
        other_payload = {"experiment_id": other["id"], "fingerprint": other["fingerprint"], "ceiling_micro": other["ceiling_micro"]}

        async def run_parallel():
            return await asyncio.gather(autonomous.run(spec["venture_id"], payload), autonomous.run(spec["venture_id"], other_payload))

        replies = asyncio.run(run_parallel())
        assert sorted(item["kind"] for item in replies) == ["experiment_blocked", "experiment_running"]
        with db()() as session:
            assert len(list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))) == 1
        return
    reply = asyncio.run(autonomous.run(spec["venture_id"], payload))
    from app.core.local_mode import is_local
    blocked = case in {"max", "same", "cooldown"} or case == "off" and is_local()
    assert reply["kind"] == ("experiment_blocked" if blocked else "experiment_running")
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))
        assert len(jobs) == (0 if blocked else 1)
        assert session.get(Experiment, spec["id"]).state == ("proposed" if blocked else "running")
    if case == "repeat":
        assert asyncio.run(autonomous.run(spec["venture_id"], payload))["run_id"] == reply["run_id"]
        with db()() as session:
            assert len(list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))) == 1


def test_disabled_scan_cannot_admit_or_charge_queued_work(research_snapshot, monkeypatch):
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db

    flags = {"experiments_autopilot_v1": True}
    charged = []

    async def unexpected_charge(*args, **kwargs):
        charged.append(kwargs)
        raise AssertionError("A disabled scan must not make a metered call")

    monkeypatch.setattr(autonomous.metered, "call", unexpected_charge)
    monkeypatch.setattr(autonomous, "get_policy", lambda key: flags)
    monkeypatch.setattr(autonomous, "_snapshot", lambda vid: deepcopy(research_snapshot))
    monkeypatch.setattr(autonomous, "_provider", lambda: SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1))
    spec = next(spec for spec in autonomous.available(research_snapshot["venture_id"]) if spec["scan"] == "paying_for_bad_fix")
    monkeypatch.setattr(autonomous, "available", lambda vid: [spec])
    payload = {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]}

    def enable_scan(on):
        flags["experiments_autopilot_v1"] = on
        if is_local():
            update_settings("capabilities", {"research": on}, {})

    enable_scan(False)
    assert asyncio.run(autonomous.run(spec["venture_id"], payload))["kind"] == "experiment_blocked"
    if not is_local():
        with pytest.raises(autonomous.CreditsUnavailable):
            asyncio.run(autonomous._scan(spec, SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1)))
    enable_scan(True)
    assert asyncio.run(autonomous.run(spec["venture_id"], payload))["kind"] == "experiment_running"
    enable_scan(False)
    monkeypatch.setattr(autonomous, "_scan", lambda *_args: pytest.fail("Disabled queued work must not start a scan"))
    run_jobs()
    assert charged == []
    with db()() as session:
        context = json.loads(session.get(Experiment, spec["id"]).context)
        assert context["retryable"] is True and context["error"] and not context.get("result")
        assert (autonomous._local_blocked_message("research") in context["error"]
            if is_local() else "switched off" in context["error"])


def test_adjacent_cooldown_tracks_reviewed_segments(research_snapshot, monkeypatch):
    from app.experiments import autonomous, engine
    from core.clock import utcnow

    monkeypatch.setattr(autonomous, "get_policy", lambda key: {"experiments_autopilot_v1": True})
    snapshot = {**research_snapshot, "segments": segments(research_snapshot)}
    spec = next(spec for spec in autonomous.select_runnable(snapshot, SimpleNamespace(name="firecrawl", search_micro=1, scrape_micro=1)) if spec["scan"] == "adjacent_swap")
    done = {**spec, "state": "concluded", "ended_at": utcnow() - timedelta(days=1)}
    assert engine.blocked_reason(spec, [done], utcnow())
    changed = deepcopy(spec)
    changed["variants"][0]["params"]["who"] += " in another region"
    assert not engine.blocked_reason(changed, [done], utcnow())
