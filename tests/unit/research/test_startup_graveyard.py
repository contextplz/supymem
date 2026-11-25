"""Offline catalogue pages and model answers; real research receipts and jobs."""
from concurrent.futures import Future
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_research_boundary import confirmed, dossier, job
from tests.security.test_durable_journey_boundary import run_queued
from tests.research_fixtures import install_research
from tests.idea_fixtures import install_provider

FIXTURES = Path(__file__).with_name("fixtures")
URL = "https://www.loot-drop.io/startup/42-crumbqueue"
SNAPSHOT = {"problem": "Bakery owners lose customer orders", "customer": "Independent bakery owners",
            "offer": "Order intake software"}


def judgment(**changes):
    return {"page": 0, "same_problem": True, "similar_customer": True, "name": "CrumbQueue",
            "founded": "2018", "ended": "2022", "cash_burned": "$2 million",
            "cause_evidence": "Too few shops paid enough to cover onboarding costs.",
            "why_failed": "Insufficient paying bakeries made onboarding uneconomical.",
            "suggestion": "Test paid order intake with independent bakery owners before investing in onboarding.",
            **changes}


@pytest.fixture
def graveyard(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.prior_art import graveyard
    return graveyard


def test_similar_match_keeps_only_short_verified_facts(graveyard):
    text = (FIXTURES / "graveyard_similar.txt").read_text()
    result = graveyard.parse_matches(json.dumps({"matches": [judgment()]}), [{"url": URL, "text": text}])
    assert result["startups"] == [{"name": "CrumbQueue", "founded": "2018", "ended": "2022",
        "cash_burned": "$2 million", "why_failed": judgment()["why_failed"],
        "suggestion": judgment()["suggestion"], "entry_url": URL}]
    assert "cause_evidence" not in json.dumps(result) and "rebuild plans" not in json.dumps(result)


def test_sector_overlap_is_not_a_match(graveyard):
    text = (FIXTURES / "graveyard_sector_only.txt").read_text()
    answer = judgment(same_problem=False, similar_customer=False, name="OvenFleet", founded="2016", ended="2020",
                      cash_burned="$4 million", cause_evidence="Enterprise contracts did not cover hardware support costs.")
    assert graveyard.parse_matches(json.dumps({"matches": [answer]}), [{"url": URL, "text": text}]) == {"startups": []}


def test_five_decisions_allow_null_nonmatches_and_at_most_three_cards(graveyard):
    text = (FIXTURES / "graveyard_similar.txt").read_text()
    rejected = judgment(page=4, same_problem=False, similar_customer=True, **{key: None for key in
        ("name", "founded", "ended", "cash_burned", "cause_evidence", "why_failed", "suggestion")})
    pages = [{"url": URL.replace("42", str(index)), "text": text} for index in range(5)]
    answer = {"matches": [judgment(page=index) for index in range(4)] + [rejected]}
    assert len(graveyard.parse_matches(json.dumps(answer), pages)["startups"]) == 3
    assert graveyard.parse_matches(json.dumps({"matches": [rejected]}), pages) == {"startups": []}


def test_malformed_candidate_cannot_discard_valid_batch_decisions(graveyard):
    from pydantic import ValidationError
    text = (FIXTURES / "graveyard_similar.txt").read_text()
    answer = {"matches": [judgment(page=0, founded=2018, ended=2022, explanation="extra unused field"),
        {"page": 1, "same_problem": False, "similar_customer": True, "why_failed": None},
        judgment(page=2, why_failed="x" * 241)]}
    raw = json.dumps(answer)
    with pytest.raises(ValidationError):
        graveyard.Judgment.model_validate_json(raw)
    result = graveyard.parse_matches("```json\n" + raw + "\n```", [{"url": URL, "text": text}] * 3,
                                     include_decisions=True)
    assert result["startups"][0]["founded"] == "2018"
    assert result["decisions"] == [{"page": 0, "same_problem": True, "similar_customer": True},
                                   {"page": 1, "same_problem": False, "similar_customer": True}]
    assert result["retry_pages"] == [2]


@pytest.mark.parametrize("change", [{"same_problem": "true"}, {"similar_customer": 1}, {"page": "0"},
                                    {"name": "Imaginary"}, {"cause_evidence": "Invented cause"}])
def test_tolerant_parser_never_coerces_similarity_or_invents_evidence(graveyard, change):
    pages = [{"url": URL, "text": (FIXTURES / "graveyard_similar.txt").read_text()}]
    assert not graveyard.parse_matches(json.dumps({"matches": [judgment(**change)]}), pages)["startups"]


def test_missing_facts_are_dropped_and_invalid_required_evidence_rejects(graveyard):
    pages = [{"url": URL, "text": (FIXTURES / "graveyard_similar.txt").read_text()}]
    result = graveyard.parse_matches(json.dumps({"matches": [judgment(founded="1901", cash_burned="$99 million")]}), pages)
    assert result["startups"][0]["founded"] is None and result["startups"][0]["cash_burned"] is None
    for change in ({"name": "Imaginary"}, {"cause_evidence": "It failed because of fraud."}, {"page": 2},
                   {"similar_customer": False}, {"why_failed": judgment()["cause_evidence"]},
                   {"why_failed": "Too few customers paid. Onboarding was costly."},
                   {"suggestion": "Test bakery orders. Then expand."}):
        assert not graveyard.parse_matches(json.dumps({"matches": [judgment(**change)]}), pages)["startups"]


def test_url_scope(graveyard):
    assert graveyard.entry_url(URL) == URL
    for invalid in ("https://www.loot-drop.io/startup.html", "https://evil.example/startup/42-name",
                    "https://loot-drop.io.evil.example/startup/42-name", "https://user@loot-drop.io/startup/42-name",
                    "http://loot-drop.io/startup/42-name", "https://loot-drop.io/startup/42-name/../../startup.html"):
        assert graveyard.entry_url(invalid) is None


def test_catalogue_prose_cannot_enter_the_normal_scrape_path(graveyard, monkeypatch):
    from app.research import deep
    from app.core.jobs import FatalError
    monkeypatch.setattr(deep, "perform", lambda *args, **kwargs: pytest.fail("must reject before credit reservation"))
    with pytest.raises(FatalError, match="research_provider_rejected"):
        deep._scrape("dossier", {}, None, {"id": "startup", "url": URL})


@pytest.mark.parametrize("failure", [None, "fetch", "judge", "budget", "raw_budget"])
def test_bounded_run_never_retains_catalogue_prose(graveyard, monkeypatch, failure):
    from app.core.jobs import RetryableError
    reads, receipts = [], []
    monkeypatch.setattr(graveyard.ex, "read", lambda did: SimpleNamespace(budget_calls=24, calls_used=24 if failure == "budget" else 1))
    runtime = {"cost_ceiling_micro": 2_000_000, "raw_calls_reserved": 55 if failure == "raw_budget" else 0}
    row = graveyard.ex.read("dossier")
    monkeypatch.setattr(graveyard.ex, "mutate", lambda did, payload, callback:
        callback(row, {"_runtime": runtime}, None))
    class Reader:
        def __init__(self, stopped):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            reads.append(url)
            if failure == "fetch":
                raise TimeoutError()
            return SimpleNamespace(text=(FIXTURES / "graveyard_similar.html").read_text())
    def synthesize(did, payload, **options):
        assert options["repair"] is False and options["provider_timeout_seconds"] == 90 and options["max_tokens"] == 4096
        prompt_schema = json.loads(options["messages"][0]["content"].split("Required JSON schema: ", 1)[1])
        assert prompt_schema == options["schema"]
        assert prompt_schema["$defs"]["Match"]["properties"]["name"]["anyOf"][1] == {"type": "null"}
        if failure == "judge":
            raise RetryableError("research_settlement_pending")
        supplied = json.loads(options["messages"][-1]["content"])["pages"]
        result = options["parse"](json.dumps({"matches": [judgment()] + [
            {"page": index, "same_problem": False, "similar_customer": False} for index in range(1, len(supplied))]}))
        receipts.append((options["key"], result))
        return result
    monkeypatch.setattr(graveyard.loot_drop, "entries", lambda: [
        {"url": URL.replace("42", str(index)), "name": "CrumbQueue", "description": "Bakery orders intake"}
        for index in range(8)])
    monkeypatch.setattr(graveyard.loot_drop, "Reader", Reader)
    monkeypatch.setattr(graveyard, "synthesize", synthesize)
    result = graveyard.run("dossier", {}, SNAPSHOT, None, Event())
    assert bool(result["startups"]) is (failure is None)
    assert len(reads) <= 5 and len(receipts) <= 1
    assert runtime["raw_calls_reserved"] == (55 if failure == "raw_budget" else 0 if failure == "budget" else 6)
    assert row.calls_used == (24 if failure == "budget" else 1 if failure == "raw_budget" else 7)
    assert "rebuild plans" not in json.dumps(receipts) and "cause_evidence" not in json.dumps(receipts)


def test_unfinished_step_does_not_wait_or_start_more_calls(graveyard, monkeypatch):
    task = Future(), Event()
    monkeypatch.setattr(graveyard.ex, "mutate", lambda *args: pytest.fail("must not store incomplete results"))
    graveyard.collect("dossier", {}, task)
    assert task[1].is_set()


@pytest.mark.parametrize("failure", ["research_invalid_output", "research_output_truncated",
                                    "research_outcome_uncertain", "research_cost_budget_exhausted"])
def test_failed_batch_falls_back_once_per_candidate_only_for_known_rejections(graveyard, monkeypatch, failure):
    from app.core.jobs import FatalError
    keys = []
    row = SimpleNamespace(budget_calls=60, calls_used=0)
    monkeypatch.setattr(graveyard.ex, "read", lambda did: row)
    monkeypatch.setattr(graveyard.ex, "mutate", lambda did, payload, callback: callback(row, {"_runtime": {}}, None))
    monkeypatch.setattr(graveyard.loot_drop, "entries", lambda: [{"url": URL.replace("42", str(index)),
        "name": "CrumbQueue", "description": "Bakery orders intake"} for index in range(5)])
    class Reader:
        def __init__(self, stopped):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            return SimpleNamespace(text=(FIXTURES / "graveyard_similar.html").read_text())
    monkeypatch.setattr(graveyard.loot_drop, "Reader", Reader)
    def synthesize(did, payload, **options):
        keys.append(options["key"])
        row.calls_used += 1
        if options["key"] == "graveyard-judge":
            raise FatalError(failure)
        pages = json.loads(options["messages"][-1]["content"])["pages"]
        assert len(pages) == 1 and options["require_stop"]
        return options["parse"](json.dumps({"matches": [judgment()]}))
    monkeypatch.setattr(graveyard, "synthesize", synthesize)
    result = graveyard.run("dossier", {}, SNAPSHOT, None, Event())
    safe = failure in {"research_invalid_output", "research_output_truncated"}
    assert keys == ["graveyard-judge"] + ([f"graveyard-judge-{index}" for index in range(5)] if safe else [])
    assert len(result["startups"]) == (3 if safe else 0)


@pytest.mark.parametrize("stop", ["cancel", "budget"])
def test_partial_batch_retries_only_invalid_candidate_and_respects_limits(graveyard, monkeypatch, stop):
    from app.core.jobs import FatalError
    keys = []
    row = SimpleNamespace(budget_calls=24, calls_used=0)
    stopped = Event()
    monkeypatch.setattr(graveyard.ex, "read", lambda did: row)
    monkeypatch.setattr(graveyard.ex, "mutate", lambda did, payload, callback: callback(row, {"_runtime": {}}, None))
    monkeypatch.setattr(graveyard.loot_drop, "entries", lambda: [{"url": URL.replace("42", str(index)),
        "name": "CrumbQueue", "description": "Bakery orders intake"} for index in range(3)])
    class Reader:
        def __init__(self, stopped):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            return SimpleNamespace(text=(FIXTURES / "graveyard_similar.html").read_text())
    monkeypatch.setattr(graveyard.loot_drop, "Reader", Reader)
    def synthesize(did, payload, **options):
        keys.append(options["key"])
        if options["key"] == "graveyard-judge":
            return options["parse"](json.dumps({"matches": [judgment(), judgment(page=1, same_problem="true")]}))
        if stop == "cancel":
            stopped.set()
        else:
            row.calls_used = row.budget_calls - 3
        raise FatalError("research_invalid_output")
    monkeypatch.setattr(graveyard, "synthesize", synthesize)
    result = graveyard.run("dossier", {}, SNAPSHOT, None, stopped)
    assert keys == ["graveyard-judge", "graveyard-judge-1"]
    assert len(result["startups"]) == 1


def test_same_dossier_cannot_restart_catalogue_reads(graveyard, monkeypatch):
    doc = {"_runtime": {}}
    monkeypatch.setattr(graveyard.ex, "mutate", lambda did, payload, callback: callback(None, doc, None))
    monkeypatch.setattr(graveyard, "run", lambda *args: {"startups": []})
    first = graveyard.start("dossier", {}, SNAPSHOT, None)
    assert first is not None
    first[0].result(timeout=5)
    assert graveyard.start("dossier", {}, SNAPSHOT, None) is None


@pytest.mark.parametrize("batch_fails", [False, True])
def test_similar_startup_reaches_api_with_settled_receipts(client, monkeypatch, batch_fails):
    from openai.types import CompletionUsage
    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    from app.research import deep, execution as ex
    from app.research.prior_art import graveyard
    reads, judge_calls = [], []
    class Reader:
        def __init__(self, stopped):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            reads.append(url)
            return SimpleNamespace(text=(FIXTURES / "graveyard_similar.html").read_text())
    monkeypatch.setattr(graveyard.loot_drop, "entries", lambda: [{"url": URL, "name": "CrumbQueue", "description": "Bakery orders"}])
    monkeypatch.setattr(graveyard.loot_drop, "Reader", Reader)
    def completion(messages, options):
        body = json.loads(messages[-1]["content"])
        if "pages" in body:
            judge_calls.append(options)
            answer = {"matches": [judgment()]}
        else:
            answer = {"overview": "Saved public sources.", "observations": [], "assessments": [], "service_checks": [],
                      "coverage_gaps": [], "proposed_tests": []}
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="not JSON" if batch_fails and len(judge_calls) == 1 and "pages" in body else json.dumps(answer)))],
            usage=CompletionUsage(prompt_tokens=500, completion_tokens=250, total_tokens=750,
                prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))
    install_provider(monkeypatch, completion)
    original_start = graveyard.start
    def completed(*args):
        task = original_start(*args)
        task[0].result(timeout=5)
        return task
    monkeypatch.setattr(graveyard, "start", completed)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    summary = dossier(client, vid)["summary"]
    assert summary["startup_graveyard"][0]["name"] == "CrumbQueue"
    assert len(reads) == 1 and len(judge_calls) == (2 if batch_fails else 1)
    receipt = ex.operation(ex.identifier(jid), "graveyard-judge")
    assert receipt["state"] == "completed" and receipt["usage"]["settled"]
    assert "cause_evidence" not in json.dumps(receipt)
    if batch_fails:
        assert receipt["error_code"] == "research_invalid_output"
        fallback = ex.operation(ex.identifier(jid), "graveyard-judge-0")
        assert fallback["state"] == "completed" and fallback["usage"]["settled"]
    runtime = json.loads(ex.read(ex.identifier(jid)).summary)["_runtime"]
    assert all(operation["response"] == {} for key, operation in runtime["operations"].items() if key.startswith("scrape_"))


@pytest.mark.parametrize("failure", ["network", "budget", "judge"])
def test_failed_graveyard_leaves_research_successful(client, monkeypatch, failure):
    vid, _, _, jid = confirmed(client, monkeypatch)
    install_research(monkeypatch)
    from app.research import deep
    from app.research.prior_art import graveyard
    from app.core.jobs import FatalError
    monkeypatch.setattr(graveyard.loot_drop, "entries", lambda: [{"url": URL, "name": "CrumbQueue", "description": "Bakery orders"}])
    class Reader:
        def __init__(self, stopped):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url):
            if failure == "network":
                raise TimeoutError()
            return SimpleNamespace(text=(FIXTURES / "graveyard_similar.html").read_text())
    monkeypatch.setattr(graveyard.loot_drop, "Reader", Reader)
    def failed(*args, **kwargs):
        raise FatalError("research_cost_budget_exhausted" if failure == "budget" else "research_invalid_output")
    monkeypatch.setattr(graveyard, "synthesize", failed)
    original_start = graveyard.start
    def completed(*args):
        task = original_start(*args)
        task[0].result(timeout=5)
        return task
    monkeypatch.setattr(graveyard, "start", completed)
    run_queued("research.deep")
    assert job(jid).status == "succeeded", job(jid).last_error
    assert "startup_graveyard" not in dossier(client, vid)["summary"]
