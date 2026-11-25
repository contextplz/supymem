"""Founder-reported experiment faults, over two unrelated startup domains."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.company_os_harness import local_owner_clients
from tests.unit.test_autonomous_experiments import (  # noqa: F401
    credit_meter,
    market,
    run_jobs,
    venture,
)


@pytest.fixture(autouse=True)
def _app(local_owner_clients, venture):  # noqa: F811
    return venture


DOMAINS = [
    ("Software that continuously audits clinic supplier invoices and access logs for billing rules, for finance leads at dental groups. "
     "We replace the spreadsheet checks they do before each insurer review.",
     "finance leads at dental groups", "the spreadsheet checks they do before each insurer review", "audit clinic supplier invoices access logs"),
    ("A platform that detects forged shipping documents in trade finance applications for risk managers at regional banks.",
     "risk managers at regional banks", "trouble detecting forged shipping documents in trade finance applications", "detect forged shipping documents trade finance"),
]


@pytest.mark.parametrize("idea,who,problem,offer", DOMAINS)
def test_problem_already_in_the_idea_is_derived_not_asked(idea, who, problem, offer):
    from app.experiments import autonomous

    icp = autonomous._research_icp({"who": who, "problem": ""}, idea, idea)
    assert icp["problem"] == problem and not icp["needs_input"]
    assert autonomous._offer_terms(idea).startswith(offer)


def test_help_ideas_use_the_audience_and_an_unclear_idea_stays_a_question():
    from app.experiments import autonomous

    assert autonomous._stated_problem("A tool that helps satellite operators schedule ground station passes.",
                                      "satellite operators") == "trouble scheduling ground station passes"
    assert autonomous._stated_problem("A tool that helps satellite operators schedule ground station passes.", "airlines") == ""
    assert autonomous._stated_problem("Something great for everyone", "airlines") == ""


@pytest.mark.parametrize("sentence,topic,audience", [
    ("They spend weeks on manual chart reviews before each payer audit and still miss expired authorizations.",
     "spending weeks on manual chart reviews before each payer audit", "revenue leads at clinics"),
    ("They lose track of calibration records across plants.", "losing track of calibration records across plants", "quality managers"),
])
def test_founder_sentence_reads_as_a_topic_and_searches_as_key_words(sentence, topic, audience):
    from app.experiments import autonomous

    slots = autonomous._hypothesis_slots({"who": audience, "problem": sentence})
    assert slots["problem"] == topic
    assert "They " not in autonomous._hypothesis(autonomous.RECIPES["demand"]["hypothesis_template"], {"who": audience, "problem": sentence})
    assert len(autonomous._search_phrase(slots["problem"]).split()) <= 6
    assert "before" not in autonomous._search_phrase(slots["problem"])


def _scan_spec(who, problem):
    from app.experiments import autonomous

    provider = SimpleNamespace(name="firecrawl", search_micro=10, scrape_micro=10)
    autonomous.metered.price_micro = autonomous.metered.price_micro
    snapshot = {"venture_id": "ven_x", "workspace_id": "wsp_x", "direction": "d", "stage": "ideating", "waitlist": {}, "app_id": None,
                "journey": {"icp": {"who": who, "problem": problem, "mode": "company", "needs_input": False}}}
    return autonomous.select_runnable(snapshot, provider)


@pytest.mark.parametrize("who,problem", [("revenue leads at clinics", "They spend weeks on manual chart reviews before each payer audit"),
                                         ("quality managers at plants", "tracking calibration records across plants")])
def test_weak_demand_scan_widens_once_inside_its_ceiling_and_reports_plainly(who, problem, monkeypatch):
    from app.experiments import autonomous
    from app.signals.research import ScrapedText, SearchResults

    monkeypatch.setattr(autonomous.metered, "price_micro", lambda *args: 10)
    monkeypatch.setattr(autonomous.metered, "firecrawl_actual_micro", lambda response, price: price)
    monkeypatch.setattr(autonomous.activity, "track", _no_track)
    spec = next(item for item in _scan_spec(who, problem) if item["scan"] == "demand")
    assert spec["ceiling_micro"] == 10 * (3 + 2) + 10 * (6 + 3)
    assert all(len(query.split()) <= 12 for query in spec["queries"]) and "They" not in " ".join(spec["queries"])
    searches, pages = [], []

    async def search(query, **kwargs):
        searches.append((query, kwargs))
        return SearchResults([{"url": f"https://example.com/{len(searches)}", "title": "A page"}], provider_rows=1, rejected_shape=0,
                             rejected_missing_url=0, usage={"firecrawl_self_hosted": True})

    async def scrape(url):
        pages.append(url)
        return ScrapedText("Nothing relevant here.", metadata={}, usage={"firecrawl_self_hosted": True})

    result = asyncio.run(autonomous._scan(spec, SimpleNamespace(name="firecrawl", search=search, scrape=scrape)))
    assert [kwargs.get("tbs") for _query, kwargs in searches] == ["qdr:m3"] * 3 + ["qdr:y"] * 2
    assert result["actual_micro"] <= spec["ceiling_micro"]
    insight = result["insight"]
    assert "last 12 months" in insight and "no first-person complaints about this problem" in insight
    assert problem not in insight and "qualifying complaints" not in insight and "source check" not in insight
    assert insight.count("does not tell you") == 1 and "evidence of no demand" not in insight
    assert 'Say "find customers"' in insight


class _no_track:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return {}

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("offer,page", [("reconcile freight invoices", "Reconcile freight invoices: pricing"),
                                        ("detect forged shipping documents", "Detect forged shipping documents | Pricing")])
def test_price_needs_a_seller_in_the_category(offer, page):
    from app.experiments import autonomous
    from core.clock import utcnow

    icp = {"who": "buyers", "problem": "manual checks", "offer_terms": offer}
    now = utcnow()
    words = " ".join(offer.split()[:2])
    roundup = {"url": "https://reviews.example/best", "title": f"12 Best {words} Tools Compared for 2026"}
    assert autonomous._price(icp, roundup, f"{offer} costs $1 per use and $499/month for {words}.", {}, now, {}) is None
    other = {"url": "https://other.example/pricing", "title": "Contract tracking pricing"}
    assert autonomous._price(icp, other, "Contract tracking is $450 per month.", {}, now, {}) is None
    seller = {"url": "https://seller.example/pricing", "title": page}
    found = autonomous._price(icp, seller, f"{offer} plans. Team plan is $49/month per seat for growing teams / finalize / maximize.", {}, now, {})
    assert found["price_points"][0]["billing_unit"] == "seat"
    silent = autonomous._price(icp, {**seller, "url": "https://quiet.example/"}, f"{offer} for large teams. Talk to sales.", {}, now, {})
    assert silent["price_points"] == [] and silent["price_point"] is None
    assert "no public price found" in autonomous._price_lines([silent])
    named = {**icp, "competitors": [{"name": "Acme Audit"}]}
    listed = autonomous._price(named, roundup, "Acme Audit costs $99 per month. Another option costs $5.", {}, now, {})
    assert [point["product"] for point in listed["price_points"]] == ["Acme Audit"]


@pytest.mark.parametrize("text,scan", [
    ("Run an experiment for me: check what competitors offer and where they fall short for our buyers.", "competitors"),
    ("Scan competitor reviews: what do teams complain about in tools like Foo and Bar?", "reviews"),
    ("check pricing of the alternatives", "pricing"),
    ("please run a demand scan", "demand"),
    ("run the red team on my idea", "redteam"),
])
def test_direct_scan_requests_are_recognised(text, scan):
    from app.chat.experiments import scan_request

    assert scan_request(text) == scan


@pytest.mark.parametrize("text", ["what do competitors charge?", "do competitors offer this?", "check out https://example.com/pricing",
                                  "our pricing is too low", "find me customers", "run my idea past you"])
def test_questions_and_other_messages_are_not_scan_requests(text):
    from app.chat.experiments import scan_request

    assert scan_request(text) is None


def test_typed_scan_request_starts_that_scan_with_no_card(market):  # noqa: F811
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db

    client, venture_id, *_rest = market
    competitors = next(item for item in autonomous.available(venture_id) if item["scan"] == "competitors")
    reply = client.post(f"/ventures/{venture_id}/chat", json={"message": "Run an experiment for me: check what competitors offer and where they fall short."})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["kind"] == "experiment_running" and competitors["title"].lower() in body["body_md"].lower()
    assert not any(action.get("confirmation") for action in body["actions"])
    with db()() as session:
        assert session.query(Job).filter(Job.kind == autonomous.KIND, Job.venture_id == venture_id).count() == 1
        assert json.loads(session.get(Experiment, competitors["id"]).context)["claimed"] is True


def _belief_set():
    types = ("who", "pain", "price", "channel", "message", "competitor_gap", "switching", "who")
    return json.dumps({"beliefs": [{"type": kind, "statement": f"Specific market statement number {index}", "confidence": 0.5}
                                   for index, kind in enumerate(types)]})


def _llm(replies, seen):
    async def llm(messages, **kwargs):
        seen.append((messages, kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=replies.pop(0)))])
    return llm


def test_cut_off_belief_reply_is_repaired_once_with_room_for_reasoning(venture, monkeypatch):
    from app.experiments import beliefs

    _client, venture_id, _workspace = venture
    seen = []
    monkeypatch.setattr(beliefs.metered, "llm", _llm(['{"beliefs": [{"type": "who"', _belief_set()], seen))
    assert len(asyncio.run(beliefs._draft(venture_id, {}))) == 8
    assert len(seen) == 2 and all(kwargs["max_tokens"] >= 8000 for _messages, kwargs in seen)
    assert seen[1][1]["request_id"] == seen[0][1]["request_id"] + ":repair"
    monkeypatch.setattr(beliefs.metered, "llm", _llm(["nope", "still nope"], seen := []))
    with pytest.raises(ValueError):
        asyncio.run(beliefs._draft(venture_id, {}))
    assert len(seen) == 2


@pytest.mark.parametrize("audience", ["Parts inventory managers", "Families sharing garden space"])
def test_failed_day_one_card_stays_quiet_but_an_asked_card_explains(venture, monkeypatch, audience):
    from app.chat import market_beliefs

    _client, venture_id, _workspace = venture

    async def broken(venture_id, payload):
        raise ValueError("bad model output")

    monkeypatch.setattr(market_beliefs, "_prepare", broken)
    assert asyncio.run(market_beliefs.prepare(venture_id, {"day_one": True})) is None
    reply = asyncio.run(market_beliefs.prepare(venture_id, {"audience": audience}))
    assert "Choose Retry to check your saved work." in reply["body_md"]
    assert "bad model output" not in reply["body_md"]
    assert any(action["label"] == "Retry" for action in reply["actions"])
    assert "Your idea is saved and nothing was launched" in reply["body_md"]
    assert any(action.get("retry") is True for action in reply["actions"])


def _tool_response():
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="web_search", arguments="{}"))
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call], content=None), finish_reason="tool_calls")])


def _turn_ctx(sources=True):
    return SimpleNamespace(workspace_id="wsp", venture_id="ven", budget_micro=1000, spent_micro=0, paid_search_outcome_unknown=False,
                           sources=[{"url": "https://example.com/a"}] if sources else [], actions=[])


def test_out_of_time_answers_from_what_it_read(monkeypatch):
    from app import tools
    from app.chat import agent, assistant
    from llm_gateway import messages as gateway_messages

    calls = []

    async def complete(messages, **kwargs):
        calls.append(kwargs.get("tools"))
        return _tool_response() if len(calls) == 1 else "Here is what the pages say."

    async def execute(*args):
        return "page text"

    times = iter([0, 0, 0, 100, 100, 100, 100, 100, 100])
    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(tools, "execute_tool", execute)
    monkeypatch.setattr(tools, "tool_specs", lambda: [{"name": "web_search"}])
    monkeypatch.setattr(agent, "_estimate", lambda *args, **kwargs: 1)
    monkeypatch.setattr(gateway_messages, "assistant_message_preserving_reasoning", lambda response: {"role": "assistant", "content": ""})
    turn = asyncio.run(agent.run_turn([{"role": "system", "content": "s"}], _turn_ctx(), clock=lambda: next(times)))
    assert turn["body"] == "Here is what the pages say." and not turn["partial"]
    assert calls[0] and not calls[-1]  # the last call has no tools


def test_partial_answer_can_really_be_continued(venture, monkeypatch):
    import time

    from app.chat import agent
    from app.chat.router import INPUT
    from app.state import MEM

    _client, venture_id, _workspace = venture

    async def partial(*args, **kwargs):
        return {"body": "stopped", "partial": True}

    monkeypatch.setattr(agent, "run_turn", partial)
    INPUT.set({"venture_id": venture_id, "original": "Compare what rivals offer"})
    asyncio.run(agent.answer([{"role": "system", "content": "s"}], venture_id=venture_id, workspace_id="wsp"))
    assert agent.resumed_question(venture_id, "tell me more") is None
    assert agent.resumed_question(venture_id, "continue") == "Compare what rivals offer"
    assert agent.resumed_question(venture_id, "continue") is None  # used once
    MEM[agent._RESUME.format(venture_id)] = {"question": "old", "at": time.time() - timedelta(hours=1).total_seconds()}
    assert agent.resumed_question(venture_id, "continue") is None
    assert "Ask me to continue" not in agent._partial(_turn_ctx(), "time")["body"]
