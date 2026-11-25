"""Fictional ventures exercise experiment copy, price results, search location and grounded answer options."""
import json

import pytest
from tests.company_os_harness import load_company_os_app

load_company_os_app()

from tests.unit.gtm.test_consumer_dogfood import _leased_payload
from tests.unit.gtm.test_known import journey_db as journey_db  # noqa: F401

CASES = [
    pytest.param({"who": "Regional freight brokers who work alone", "head": "Regional freight brokers",
                  "problem": "Regional freight brokers need help with reconciling carrier invoices", "place": "Lisbon",
                  "idea": "An invoice matching tool for regional freight brokers", "product": "Carrier ledger"}, id="b2b"),
    pytest.param({"who": "Retired neighbours who garden at home", "head": "Retired neighbours",
                  "problem": "Retired neighbours struggle with planning watering schedules", "place": "Valencia",
                  "idea": "A watering planner app for retired gardeners", "product": "Garden diary"}, id="consumer"),
]


def _spec(case, index):
    return {"id": f"scan_{index}", "title": f"Public check {index}", "state": "proposed", "result": None, "scan": "demand",
            "icp": {"who": case["who"], "problem": case["problem"]},
            "hypothesis": f"{case['who']} describe {case['problem']} in recent public discussions.",
            "method": "Read up to twelve public pages and keep only dated first-person complaints. " * 3}


@pytest.mark.parametrize("case", CASES)
def test_hypotheses_never_join_an_audience_with_a_sentence_about_it(case):
    from app.experiments import autonomous

    text = autonomous._hypothesis(autonomous.RECIPES["demand"]["hypothesis_template"], case)
    assert text.casefold().count(case["head"].casefold()) == 1, text


@pytest.mark.parametrize("case", CASES)
def test_experiment_list_is_short_plain_and_has_one_next_step(case, monkeypatch):
    from app.chat import experiments
    from app.experiments import autonomous, engine, ledger

    specs = [_spec(case, index) for index in range(9)]
    monkeypatch.setattr(experiments, "_idea_gate", lambda venture_id: None)
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda venture_id: {
        "journey": {"icp": case}, "waitlist": {"enabled": True}, "app_id": "app"})
    monkeypatch.setattr(autonomous, "available", lambda venture_id: specs)
    monkeypatch.setattr(autonomous, "prepare", lambda *args: pytest.fail("Listing must not prepare or start work"))
    monkeypatch.setattr(ledger, "list_for_venture", lambda venture_id: [])
    monkeypatch.setattr(ledger, "ranking_inputs", lambda venture_id: {})
    monkeypatch.setattr(engine, "blocked_reason", lambda *args: None)
    monkeypatch.setattr(engine, "score", lambda item, *args: -int(item["id"].split("_")[1]))
    monkeypatch.setattr(engine, "reason", lambda *args: "Biggest open risk: whether this problem is real.")

    reply = experiments.list_reply("venture")
    body = reply["body_md"]
    assert len(body) < 900, body
    assert "Method:" not in body and "Read up to twelve" not in body and "—" not in body
    # 0c78ac8ba includes the saved audience and problem above the grounded recommendation.
    assert body.casefold().count(case["who"].casefold()) <= 2, body
    assert "Recommended: Public check 0" in body
    assert body.count("Next:") == 1 and body.splitlines()[-1].startswith("Next:")
    primary = [action for action in reply["actions"] if action.get("style") == "primary"]
    assert [action["payload"]["experiment_id"] for action in primary] == ["scan_0"]
    specs[5:] = [{**spec, "state": "concluded", "result": {"insight": "Saved reading.", "sources": []}} for spec in specs[5:]]
    capped = experiments.list_reply("venture")
    assert capped["body_md"].count("\n- ") == experiments.SHOWN_EXPERIMENTS and "other saved tests" in capped["body_md"]
    assert "Saved reading." not in capped["body_md"]
    assert primary[0]["action"] == "experiment.prepare" and primary[0]["label"] == "Run it for me"
    assert not any(action.get("confirmation") or action.get("action") == "experiment.run" for action in reply["actions"])


def _price_scan(case):
    url = f"https://prices.example/{case['product'].replace(' ', '-').lower()}"
    point = {"product": case["product"], "price": "$25", "billing_period": "billing period not stated",
             "billing_unit": "not stated", "source": url}
    source = {"url": url, "title": case["product"], "quote": "Plans start at $25. Team costs $40 per month per seat.", "price_point": point,
              "price_points": [point, point, {**point, "price": "$40", "billing_period": "month", "billing_unit": "seat"},
                               {**point, "billing_unit": "entity"}]}
    spec = {"id": "price_scan", "scan": "pricing", "extractor": "price", "title": "Your price comparison", "metric": "priced_products",
            "icp": case, "success_threshold": {"op": ">=", "value": 2, "min_events": 2},
            "failure_threshold": {"op": "<", "value": 1, "min_events": 2}}
    return spec, source, url


@pytest.mark.parametrize("case", CASES)
def test_price_results_read_plainly_and_group_by_source(case):
    from app.experiments import autonomous

    spec, source, url = _price_scan(case)
    fresh = autonomous._scan_result(spec, [{"sources": [source, source], "retrieved_sources": 2}], 0, 0)
    saved = {**fresh, "sources": [source] * 4, "insight": (
        f"Your price comparison: you have 1 qualifying public sources across 2 retrieved results.\n"
        f"- [{case['product']}]({url}): $25 (billing period not stated; billing unit: entity).\n"
        f"- [{case['product']}]({url}): $25 (billing period not stated; billing unit: not stated).\n"
        f"- \"Plans start at $25.\" [{case['product']}]({url})\nOutcome: inconclusive. Next: check it.")}
    rendered = autonomous.result_reply({**spec, "result": saved})
    for body in (fresh["insight"], rendered["body_md"]):
        assert "billing period" not in body and "billing unit" not in body and "not stated" not in body, body
        assert body.count(url) == 1, body
        assert "$25" in body and "$40 per month per seat" in body
    assert "Next: check it." in rendered["body_md"]
    assert len(rendered["citations"]) == 1
    # The saved evidence itself is never rewritten.
    assert fresh["sources"][0]["price_points"][0]["billing_period"] == "billing period not stated"


@pytest.mark.parametrize("case", CASES)
def test_search_card_keeps_the_place_the_founder_asked_for(case, monkeypatch):
    from app.chat import customers

    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"items": []})
    request = f"find me {case['head'].lower()} in {case['place']}"
    base = {"icp": {"who": case["who"], "problem": case["problem"]}, "can_prepare": True, "discovery_note": request}
    running = customers.render("venture", {**base, "state": "running", "job_id": "job"})
    assert case["place"] in running["body_md"]
    assert running["citations"][0]["gtm_search_progress"]["audience"].endswith(f"in {case['place']}")
    ready = customers.render("venture", {**base, "state": "idle", "search_request": request})
    assert case["place"] in ready["body_md"]
    library, _ = customers._library_section("venture", base["icp"] | {"location": case["place"]})
    assert case["place"] in library
    changed = customers.render("venture", {**base, "icp": base["icp"] | {"location": "Another region"},
        "state": "idle", "search_request": request})
    assert case["place"] in changed["body_md"] and "Another region" not in changed["body_md"]
    assert next(action for action in ready["actions"] if action.get("action") == "gtm.find")["payload"]["note"] == request
    assert customers.render("venture", {**base, "discovery_note": "", "state": "running", "job_id": "job"})["body_md"].count(case["place"]) == 0


@pytest.mark.parametrize("case", CASES)
def test_search_queries_keep_the_place_the_founder_asked_for(case, journey_db, monkeypatch):  # noqa: F811
    from app.gtm import journey

    seen = {}

    async def model(payload, stage, context):
        seen.update(context)
        raise journey.FatalError("gtm_test_stop")

    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], case["who"], problem=case["problem"], recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    with pytest.raises(journey.FatalError):
        import asyncio
        asyncio.run(journey._discover(case["who"], problem=case["problem"], note=f"only people in {case['place']}", payload=payload))
    assert case["place"] in seen["audience"]
    assert seen["founder_note"] == f"only people in {case['place']}"
    queries = journey._search_queries(seen["audience"], case["problem"], context=seen)
    assert queries and all(case["place"] in query for query in queries)
    assert journey.search_audience(case["who"], "") == case["who"]
    assert journey.search_audience(case["who"], "only solo operators") == case["who"]


@pytest.mark.parametrize("case", CASES)
def test_answer_options_must_be_grounded_in_what_the_founder_said(case):
    from app.gstack import office_hours as oh

    snapshot = {"idea": case["idea"], "audience": case["who"]}
    tools = ["You keep it in a shared spreadsheet.", "You ask a friend to help.", "You use a phone reminder."]
    invented = ["You use the small business tax scheme.", "You follow the sector regulator's filing rules.", "You rely on a subsidy."]

    def raw(options):
        return json.dumps({"questions": [{"id": "Q2", "question": "What do you use to handle this problem today?", "options": options},
                                         {"id": "Q3", "question": "What makes this problem urgent for you?", "options": tools}]})

    assert oh.parse_picks(raw(invented), snapshot) == oh.default_picks(snapshot)
    assert tools[0] in json.dumps(oh.parse_picks(raw(tools), snapshot))
    for option in invented:
        assert oh._unsupported_claim(option, snapshot)
    assert not any(oh._unsupported_claim(option, snapshot) for option in tools)
    said = {**snapshot, "idea": case["idea"] + " for the small business tax scheme"}
    assert not oh._unsupported_claim(invented[0], said)
    from_website = {**snapshot, "founder_facts": {"problem": {"source": "website", "quote": invented[0]}}}
    assert oh._unsupported_claim(invented[0], from_website)
    assert "tax" in oh.messages_for(snapshot)[0]["content"]
