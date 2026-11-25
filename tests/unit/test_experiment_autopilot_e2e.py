"""The scheduled learning loop uses real confirmations, saved results and credit receipts."""
import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.company_os_harness import local_owner_clients

from tests.unit.test_autonomous_experiments import venture
from tests.unit.test_market_beliefs import base_market, market
from tests.unit.test_page_experiments import connected_coding_tool


@pytest.fixture
def learning_loop(market, monkeypatch, connected_coding_tool):
    from app.chat import assistant
    from app.credits import metered
    from app.credits.policy import set_policy
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.experiments import beliefs
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from core.clock import utcnow

    client, venture_id, workspace_id, view, landing, provider, calls, hits, app_id = market
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    set_policy("outbound_provider_rates", {"version": "test", "rates": {
        "gtm_email_send:composio_gmail": {"action": "gtm_email_send", "provider": "composio_gmail",
            "price_usd": "0.000001", "source": "Test provider receipt"}}}, updated_by="test")
    view.update(allow_sending=True, auto_approve_first_emails=True, playbooks=[{"key": "value_first"}])
    now = utcnow()
    with db()() as session:
        session.get(Venture, venture_id).stage = "operating"
        from tests.company_os_harness import append_idea_revision
        idea = session.query(IdeaSession).filter_by(venture_id=venture_id, workspace_id=workspace_id).one()
        revision = append_idea_revision(session, idea.current_revision_id,
            summary=view["icp"]["problem"],
            structured=json.dumps({"audience": view["icp"]["who"], "problem": view["icp"]["problem"],
                "_questions_skipped": True}))
        revision_id = revision.id
        idea.confirmed_at, idea.confirmed_by_person_id = now, "local"
        session.commit()
    landing["enabled"] = True
    adjacent_who = "Instructors supporting " + view["icp"]["who"]
    adjacent_problem = "delivering practical workshops"
    adjacent_quote = f"{adjacent_who} need help with {adjacent_problem}."
    for hit in hits[:2]:
        hit["snippet"] = hit["snippet"].rstrip(".") + "; " + adjacent_quote
    unsaved = {"url": "https://query-only.example/unread", "title": "Unread search result",
        "snippet": "Independent translators need help with collecting overdue invoices."}
    hits.append(unsaved)
    model_calls = []
    original_call = metered.call

    async def model(messages, **kwargs):
        inputs = (json.loads(messages[-1]["content"]) if kwargs["purpose"] == "experiment.beliefs"
                  else {"prompt": messages[-1]["content"]})
        model_calls.append((deepcopy(inputs), deepcopy(kwargs)))
        if kwargs["purpose"] == "experiment.beliefs":
            parent = inputs.get("parent")
            if parent:
                output = {"beliefs": [{"type": parent["type"], "confidence": 0.2,
                    "statement": f"You could test a narrower {parent['type']} for {view['icp']['who']}.",
                    "audience": adjacent_who, "problem": adjacent_problem}]}
            else:
                output = {"beliefs": [{"type": kind, "confidence": 0.2,
                    "statement": ("A different idea to try: " if index == 7 else "You could test: ")
                        + f"{view['icp']['who']} have a {kind} question about {view['icp']['problem']} ({index}).",
                    **({"audience": view["icp"]["who"], "problem": view["icp"]["problem"]}
                       if kind in {"who", "pain"} else {})}
                    for index, kind in enumerate((*beliefs.TYPES, "pain"))]}
        else:
            output = {"directions": [{"kind": "narrower_wedge", "title": "Test your narrower task",
                "text": f"You could focus on {view['icp']['problem']} for {view['icp']['who']}."}]}
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))])

        async def respond():
            return response

        return await original_call(respond, purpose=kwargs["purpose"], estimate_micro=1,
            actual_micro=lambda _response: 1, provider="firecrawl", workspace_id=workspace_id,
            venture_id=venture_id, request_id=kwargs.get("request_id"))

    async def review_model(messages, **_kwargs):
        response = await model(messages, purpose="experiment.review", request_id="review:" + revision_id)
        return response.choices[0].message.content

    def unexpected_contact(*_args, **_kwargs):
        pytest.fail("Your zero-email standing budget must not call an email provider")

    monkeypatch.setattr(metered, "llm", model)
    monkeypatch.setattr(assistant, "_complete", review_model)
    monkeypatch.setattr(composio_gmail, "send_gmail_email", unexpected_contact)
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"run_for_me_experiments": True, "background_work": True}, {})
        original_search, original_scrape = provider.search, provider.scrape

        async def search(query, **options):
            return await original_call(lambda: original_search(query, **options), purpose="experiment.search",
                estimate_micro=0, actual_micro=lambda _result: 0, provider=provider.name,
                workspace_id=workspace_id, venture_id=venture_id)

        async def scrape(url):
            return await original_call(lambda: original_scrape(url), purpose="experiment.scrape",
                estimate_micro=0, actual_micro=lambda _result: 0, provider=provider.name,
                workspace_id=workspace_id, venture_id=venture_id)

        monkeypatch.setattr(provider, "search", search)
        monkeypatch.setattr(provider, "scrape", scrape)
    return {"market": market, "model_calls": model_calls, "adjacent_quote": adjacent_quote,
        "adjacent_who": adjacent_who, "adjacent_problem": adjacent_problem, "unsaved": unsaved,
        "now": now}


def run_worker(kind):
    from app.chat.conversations import bind_job_conversation
    from app.core.jobs import complete_job, lease
    from app.state import db
    from app.worker import handlers

    handlers.install_default_handlers()
    with db()() as session:
        jobs = lease(session, "experiment-loop-test", kinds=[kind], n=1)
        session.commit()
    if not jobs:
        return None
    job = jobs[0]
    payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    with bind_job_conversation(payload, venture_id=job.venture_id, workspace_id=job.workspace_id):
        asyncio.run(handlers.dispatch(kind, payload))
    with db()() as session:
        complete_job(session, job.id, worker_id=job.worker_id, attempt=job.attempts)
        session.commit()
    return job


def five_minute_tick(moment, *, expect_experiments=True):
    from app.tasks.scheduler import dispatch_tick

    scheduled = dispatch_tick("5m", now=moment, all_workers=True)
    if not expect_experiments:
        assert "experiment.beliefs.tick" not in scheduled["enqueued"]
        assert run_worker("experiment.beliefs.tick") is None
        assert dispatch_tick("5m", now=moment, all_workers=True) == scheduled
        return scheduled
    assert "experiment.beliefs.tick" in scheduled["enqueued"]
    job = run_worker("experiment.beliefs.tick")
    assert job is not None
    assert job.id == scheduled["ids"][scheduled["enqueued"].index(job.kind)]
    assert dispatch_tick("5m", now=moment, all_workers=True) == scheduled
    assert run_worker("experiment.beliefs.tick") is None
    return scheduled


def prepare_budget(loop):
    from app.db.supymem_models import AppEvent
    from app.experiments import page_tests
    from app.state import db
    from core.clock import utcnow

    client, venture_id, workspace_id, view, _landing, *_rest = loop["market"]
    prepared = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": "experiment.page.prepare", "payload": {"kind": "pricing", "variants": [
            {"key": "first", "label": "Your proposed price", "price": "USD 12 per month"}]}})
    assert prepared.status_code == 200, prepared.text
    page = next(spec for spec in page_tests.available({"venture_id": venture_id,
        "workspace_id": workspace_id, "direction": loop_direction(venture_id)}) if spec["page_kind"] == "pricing")
    with db()() as session:
        for _index in range(page["min_sample"]):
            session.add(AppEvent(app_id=page["app_id"], workspace_id=workspace_id,
                name="landing_view", ts=utcnow(), props=json.dumps({"experiment_id": page["id"], "path": "/"})))
        session.commit()
    options = {"weekly_budget_micro": 5_000_000, "max_emails_per_day": 0}
    prepared = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": "experiment.beliefs.prepare", "payload": options})
    assert prepared.status_code == 200, prepared.text
    actions = [action for action in prepared.json()["actions"] if action.get("action") == "experiment.beliefs.approve"]
    assert actions, prepared.json()
    action = actions[0]
    card = action.get("confirmation") or {"action": action["action"], "resulting_state": action["payload"]}
    return prepared.json(), card, page


def loop_direction(venture_id):
    from app.experiments import ledger
    from app.state import db

    with db()() as session:
        return ledger.current_direction(session, venture_id)


def durable_state(venture_id):
    from app.core.local_mode import is_local
    from app.db.models import ActivityLine, ChatMessage, CreditLedgerEntry, CreditReservation, GtmTouch, Job, VentureDocument
    from app.db.supymem_models import Experiment
    from app.state import db

    with db()() as session:
        state = {
            "experiments": {row.id: (row.state, row.metric, row.result, row.outcome, row.context)
                for row in session.query(Experiment).filter_by(venture_id=venture_id)},
            "messages": {row.id: (row.kind, row.body_md, row.actions_json)
                for row in session.query(ChatMessage).filter_by(venture_id=venture_id)},
            "jobs": {row.id: (row.status, row.payload_json)
                for row in session.query(Job).filter_by(venture_id=venture_id, kind="experiment.run")},
            "spends": {row.id: (row.request_id, row.reservation_id, row.amount_micro, row.idempotency_key)
                for row in session.query(CreditLedgerEntry).filter_by(kind="spend")},
            "holds": {row.id: (row.status, row.settled_micro)
                for row in session.query(CreditReservation).filter_by(venture_id=venture_id)},
            "touches": session.query(GtmTouch).filter_by(venture_id=venture_id).count(),
            "readouts": {row.seq: row.detail_json for row in session.query(ActivityLine)
                .filter_by(venture_id=venture_id, kind="experiment_readout")},
            "documents": {row.id: row.provenance_json for row in session.query(VentureDocument)
                .filter_by(venture_id=venture_id, kind="experiment_spinoff_proposals")},
        }
    if is_local():
        from app.credits import local_meter
        with local_meter._database() as connection:
            state["local_calls"] = {row["call_id"]: (row["request_id"], row["cost_usd_micro"], row["status"])
                for row in connection.execute("SELECT call_id, request_id, cost_usd_micro, status FROM calls WHERE venture_id = ?", (venture_id,))}
    return state


def approve_budget(loop, message, card):
    from app.core.local_mode import is_local
    client, venture_id, *_rest = loop["market"]
    approved = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": card["action"], "payload": card["resulting_state"],
        **({"idempotency_key": f"test:{message['id']}:{card['action']}"} if is_local() else {"message_id": message["id"]})})
    assert approved.status_code == 200, approved.text
    return approved.json()


def test_portfolio_preserves_beliefs_needed_by_later_lanes(market):
    from app.credits.policy import set_policy
    from app.experiments import autonomous, belief_planner, page_tests

    _client, venture_id, _workspace_id, view, landing, provider, *_rest = market
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    landing["enabled"] = True
    items = [{"id": f"{index:02d}_{kind}", "type": kind, "confidence": 0.0, "status": "to_test",
        "statement": f"You could test {kind} for {view['icp']['who']}."}
        for index, kind in enumerate(("price", "pain", "who"))]
    specs = {spec["scan"]: spec for spec in autonomous.select_runnable(autonomous._snapshot(venture_id), provider)}
    specs["pricing"]["belief_ids"] = [items[0]["id"]]
    specs["redteam"]["belief_ids"] = [items[1]["id"]]
    specs["demand"]["belief_ids"] = [items[1]["id"], items[2]["id"]]
    response = page_tests._spec(venture_id, {"kind": "pricing", "variants": [
        {"key": "first", "label": "Your proposed price", "price": "USD 12 per month"}]})
    response.update(state="running", belief_ids=[items[0]["id"]])
    plan = belief_planner.build(items, [specs["pricing"], specs["demand"], specs["redteam"], response], {},
        max_emails_per_day=0)
    assert [test["runner"] for test in plan["tests"]] == ["demand", "page_pricing", "redteam"]
    assert [test["belief_id"] for test in plan["tests"]] == [items[2]["id"], items[0]["id"], items[1]["id"]]


def test_local_followup_ignores_a_regenerated_plan_entry(market, monkeypatch):
    from app.experiments import belief_autopilot, belief_planner, beliefs
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    _, venture_id, _, view, *_ = market
    approved = {"id": "approved", "catalog_key": "public_sources", "hypothesis": "Your audience needs help.",
        "icp": view["icp"]}
    regenerated = approved | {"id": "regenerated", "queries": []}
    changed = approved | {"id": "changed", "icp": view["icp"] | {"problem": "A different workflow"}}
    with db()() as session:
        saved = beliefs.row(session, venture_id, create=True)
        saved.context = json.dumps({"beliefs": [], "plan": {"state": "active", "ceiling_micro": 100,
            "reserved_micro": 0, "max_emails_per_day": 0, "tests": [{"experiment_id": approved["id"],
                "belief_id": "first", "state": "running"}]}})
        session.commit()

    def build(items, experiments, inputs, **kwargs):
        assert experiments == [changed]
        return {"tests": []}

    monkeypatch.setattr(belief_planner, "build", build)
    belief_autopilot._plan_followups(venture_id, [regenerated, changed], {"experiments": [approved]}, utcnow())


def test_experiment_autopilot_end_to_end(learning_loop):
    from app.core.local_mode import is_local
    from app.chat import next_moves
    from app.db.models import ChatMessage
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, belief_planner, beliefs, ledger
    from app.state import db

    loop = learning_loop
    client, venture_id, workspace_id, view, _landing, _provider, calls, hits, _app_id = loop["market"]
    message, card, page = prepare_budget(loop)
    initial = beliefs.load(venture_id)
    plan = initial["plan"]
    assert len(initial["beliefs"]) == 8
    assert all(item["status"] == "to_test" and item["evidence"] == [] for item in initial["beliefs"])
    assert any(item["statement"].startswith("A different idea to try:") for item in initial["beliefs"])
    assert plan["renewal"] == "weekly_until_revoked" and plan["expires_at"] is None
    assert plan["weekly_budget_micro"] == 5_000_000 and plan["max_emails_per_day"] == 0
    assert plan["allowed_audiences"] == [view["icp"]["who"]]
    if is_local():
        assert page["state"] == "proposed"
        assert all(test["experiment_id"] != page["id"] for test in plan["tests"])
        assert plan["tests"][2]["runner"] == "redteam"
    else:
        assert [test["runner"] for test in plan["tests"][:3]][1:] == ["page_pricing", "redteam"]
    assert plan["tests"][0]["runner"] not in {"page_pricing", "redteam", "outreach_split"}
    assert all(test["runner"] not in {"outreach_split", "booking"} for test in plan["tests"])
    message_belief = next(item for item in initial["beliefs"] if item["type"] == "message")
    email_plan = belief_planner.build([message_belief], belief_planner.available(venture_id, initial["beliefs"]),
        ledger.ranking_inputs(venture_id), max_emails_per_day=15)
    assert email_plan["tests"] and email_plan["tests"][0]["runner"] == "outreach_split"
    assert card["resulting_state"] == {"plan_id": plan["id"], "fingerprint": plan["fingerprint"]}
    before = durable_state(venture_id)
    five_minute_tick(loop["now"])
    assert durable_state(venture_id) == before and calls == []
    approved = approve_budget(loop, message, card)
    assert beliefs.load(venture_id)["plan"]["state"] == "active"
    assert approve_budget(loop, message, card)["id"] == approved["id"]

    for tick_index in range(1, 20):
        five_minute_tick(loop["now"] + timedelta(minutes=5 * tick_index))
        worker = run_worker(autonomous.KIND)
        current = beliefs.load(venture_id)
        assert not any(test["state"] == "error" for test in current["plan"]["tests"]), current
        if worker is None and all(test["state"] == "done" for test in current["plan"]["tests"]):
            break
    else:
        states = {test["runner"]: test["state"] for test in current["plan"]["tests"]}
        pytest.fail(f"Your approved experiment portfolio did not finish within 19 scheduled ticks: {states}")
    current = beliefs.load(venture_id)
    if is_local():
        assert sum(test["runner"] == "community" for test in current["plan"]["tests"]) == 1
    assert calls and all(call[0] in {"search", "scrape"} for call in calls)
    assert any(item["evidence"] for item in current["beliefs"])
    assert any(item["status"] in {"confirmed", "weak", "disproven"} for item in current["beliefs"])
    saved_sources = {}
    with db()() as session:
        for test in current["plan"]["tests"]:
            row = session.get(Experiment, test["experiment_id"])
            context = json.loads(row.context)
            result = context["result"]
            spec = context["spec"]
            assert row.state == "concluded" and row.ended_at is not None
            assert row.outcome == result["outcome"] == autonomous.outcome(spec, result["measured"])
            assert row.result == result["measured"].get(spec["metric"])
            assert test["result"] == result
            if row.id == page["id"]:
                assert result["measured"] == {"events": page["min_sample"], "clicks": 0,
                    "cta_click_rate": 0.0, "higher_price_share": None}
                assert result["sources"] == [{"url": f"/v/{venture_id}/analytics", "title": "Your recorded event counts"}]
            else:
                for source in result.get("sources", []):
                    hit = next(hit for hit in hits if hit["url"] == source["url"])
                    if "quote" in source:
                        assert source["quote"] in hit["snippet"]
                        saved_sources[(row.id, source["url"])] = source
                    else:
                        assert spec["scan"] == "competitors" and source["title"] == hit["title"]
                        assert source in result["by_variant"][0]["alternatives"]
                if not spec.get("reused_demand"):
                    assert test["actual_micro"] == result["actual_micro"]
        assert len({row.id for row in session.query(Experiment) if row.venture_id == venture_id
            and row.state == "concluded"}) == len(current["plan"]["tests"])
    assert all(url != loop["unsaved"]["url"] for _ident, url in saved_sources)
    state = durable_state(venture_id)
    assert state["touches"] == 0 and state["readouts"]
    if is_local():
        assert state["local_calls"] and not state["spends"] and not state["holds"]
        assert all(type(cost) is int and cost >= 0 and status in {"confirmed", "unspent"}
            for _request_id, cost, status in state["local_calls"].values())
    else:
        assert state["spends"]
    assert len({spend[3] for spend in state["spends"].values()}) == len(state["spends"])
    assert all(status == "settled" for status, _actual in state["holds"].values())
    assert current["plan"]["reserved_micro"] == sum(test.get("actual_micro", 0)
        for test in current["plan"]["tests"]) + sum(call["actual_micro"] for call in current["plan"]["calls"])
    assert current["plan"]["reserved_micro"] <= current["plan"]["weekly_budget_micro"]
    assert state["documents"]
    for raw in state["documents"].values():
        proposals = json.loads(raw)["proposals"]
        assert len(proposals) == 1
        proposal = proposals[0]
        assert (proposal["who"], proposal["problem"]) == (loop["adjacent_who"], loop["adjacent_problem"])
        assert proposal["who"] != view["icp"]["who"]
        for evidence in proposal["evidence"]:
            source = saved_sources[(evidence["experiment_id"], evidence["url"])]
            assert evidence["quote"] == loop["adjacent_quote"] and evidence["quote"] in source["quote"]

    learning = next_moves.experiment_learning(venture_id)
    assert isinstance(learning["recommendation"], dict)
    assert learning["recommendation"]["id"].startswith(("test:", "pivot:", "persevere:"))
    review = client.post(f"/ventures/{venture_id}/chat", json={"message": "What have we learned from experiments?"})
    assert review.status_code == 200, review.text
    with db()() as session:
        recommendations = [item["next_moves"] for row in session.query(ChatMessage).filter_by(venture_id=venture_id)
            for item in json.loads(row.citations_json or "[]") if
            item.get("next_moves", {}).get("recommendation", {}).get("id") == learning["recommendation"]["id"]]
    if is_local():
        assert not recommendations and "next_moves" not in review.json()
    else:
        assert len(recommendations) == 1
        assert recommendations[0]["recommendation"] == learning["recommendation"]
        assert sum(action["label"] in {"Test next", "Plan your next test", "Consider a pivot", "Keep testing your direction"} for action in recommendations[0]["actions"]) == 1
        assert "recommendation" not in next_moves.compose(venture_id)
    action = next(action for action in review.json()["actions"] if action.get("action") == "experiment.persevere")
    decision = ({"action": action["action"], "resulting_state": action["payload"]}
        if is_local() else action["confirmation"])
    assert {item["experiment_id"] for item in decision["resulting_state"]["evidence"]} == {
        test["experiment_id"] for test in current["plan"]["tests"]}
    before = durable_state(venture_id)
    refused = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": decision["action"], "payload": ({**decision["resulting_state"], "evidence": []}
            if is_local() else decision["resulting_state"])})
    assert refused.status_code == 400, refused.text
    assert refused.json()["detail"]["error"]["code"] == ("experiment_decision_invalid"
        if is_local() else "confirmation_card_required")
    assert durable_state(venture_id) == before
    decided = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": decision["action"],
        "payload": decision["resulting_state"], **({} if is_local() else {"message_id": review.json()["id"]})})
    assert decided.status_code == 200, decided.text
    assert ledger.loop_state(venture_id)["done"]
    before = durable_state(venture_id)
    provider_calls, model_calls = deepcopy(calls), deepcopy(loop["model_calls"])
    five_minute_tick(loop["now"] + timedelta(minutes=5 * (tick_index + 1)))
    assert run_worker(autonomous.KIND) is None
    assert durable_state(venture_id) == before
    assert calls == provider_calls and loop["model_calls"] == model_calls


def test_experiment_autopilot_flag_off_preserves_the_legacy_path(learning_loop):
    from app.chat import next_moves
    from app.credits.policy import set_policy
    from app.experiments import autonomous, beliefs, spinoffs
    from app.state import db

    loop = learning_loop
    client, venture_id, workspace_id, _view, _landing, _provider, calls, *_rest = loop["market"]
    message, card, page = prepare_budget(loop)
    approve_budget(loop, message, card)
    set_policy("feature_flags", {"experiments_autopilot_v1": False}, updated_by="test")
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"run_for_me_experiments": False}, {})
    before = durable_state(venture_id)
    model_calls = deepcopy(loop["model_calls"])
    for tick_index in range(2):
        five_minute_tick(loop["now"] + timedelta(minutes=5 * tick_index), expect_experiments=not is_local())
        assert run_worker(autonomous.KIND) is None
    assert durable_state(venture_id) == before and calls == [] and loop["model_calls"] == model_calls
    assert next_moves.experiment_learning(venture_id) is None
    next_move = next_moves.compose(venture_id).get("recommendation")
    if is_local() and next_move:
        assert not (next_move.get("action") or "").startswith("experiment.")
    else:
        assert next_move is None
    assert spinoffs.consume(venture_id, workspace_id, page["id"]) == []
    assert durable_state(venture_id) == before
    with db()() as session:
        saved = beliefs.row(session, venture_id)
        context = json.loads(saved.context)
        context["plan"] = None
        saved.context = json.dumps(context)
        session.commit()
    legacy = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.beliefs.prepare",
        "payload": {"weekly_budget_micro": 5_000_000, "max_emails_per_day": 0}})
    assert legacy.status_code == 200, legacy.text
    plan = beliefs.load(venture_id)["plan"]
    if is_local():
        from app.core.local_capabilities import blocked_message

        assert plan is None and calls == []
        assert blocked_message("run_for_me_experiments") in legacy.json()["body_md"]
        return
    assert "renewal" not in plan and "weekly_budget_micro" not in plan
    assert plan["days"] == 7 and plan["max_emails_per_day"] == 15 and plan["state"] == "draft"
    assert all(test["runner"] != "redteam" for test in plan["tests"])
    assert calls == []
