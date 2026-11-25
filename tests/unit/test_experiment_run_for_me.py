import asyncio
import json
import re
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select
from tests.company_os_harness import append_idea_revision, local_owner_clients
from core.clock import utcnow

from tests.unit.test_autonomous_experiments import credit_meter, market, run_jobs, save_market_context, venture as venture_fixture
from tests.unit.test_market_beliefs import set_local_experiments

venture = venture_fixture


def test_explicit_design_saves_with_automatic_experiments_off(market, monkeypatch):
    from app.chat import experiments
    from app.core.local_secrets import update_settings
    from app.core.local_worker import blocked_reason
    from app.experiments import ledger

    client, venture_id, _, view, *_ = market
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("capabilities", {"run_for_me_experiments": False, "background_work": False}, {})
    plan = {"hypothesis": f"You can test demand from {view['icp']['who']}.",
        "action": f"Interview eight consenting {view['icp']['who']} about {view['icp']['problem']}.",
        "success_measure": "Pass if three agree to a dated trial; fail if all eight decline; otherwise inconclusive."}

    async def design(*args, **kwargs):
        return plan

    monkeypatch.setattr(experiments, "_llm_json", design)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message":
        "Design a customer interview experiment for my saved idea and save its hypothesis, action and success measure"})
    assert response.status_code == 200, response.text
    rows = ledger.list_for_venture(venture_id)
    assert len(rows) == 1, response.json()["body_md"]
    saved = rows[0]
    assert saved["hypothesis"] == plan["hypothesis"], response.json()["body_md"]
    assert saved["action"] == plan["action"] and saved["measure"] == plan["success_measure"]
    assert saved["state"] == "proposed"
    assert not any(action.get("confirmation") for action in response.json()["actions"])
    assert blocked_reason("experiment.beliefs.tick")


def test_explicit_scan_finishes_with_automatic_experiments_off(market, credit_meter, monkeypatch):
    from app.core.local_secrets import update_settings
    from app.core.local_worker import blocked_reason
    from app.experiments import autonomous

    client, venture_id, *_ = market
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("capabilities", {"run_for_me_experiments": False, "background_work": False,
        "research": True}, {})
    response = client.post(f"/ventures/{venture_id}/chat", json={"message":
        "Run for me one public-source demand scan for my saved idea and save the findings"})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "experiment_running", response.json()["body_md"]
    assert not any(action.get("confirmation") for action in response.json()["actions"])
    assert blocked_reason(autonomous.KIND) is None
    run_jobs()
    saved = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    assert saved["state"] == "concluded", saved.get("error")
    assert saved["result"]["sources"]
    assert all(source["url"] and source["quote"] for source in saved["result"]["sources"])
    assert blocked_reason("experiment.beliefs.tick")


@pytest.mark.parametrize("known_status", ["missing", "confirm"])
def test_requested_scan_asks_for_missing_context_instead_of_running_an_interview(market, credit_meter, monkeypatch, known_status):
    from app.chat import experiments
    from app.core.local_secrets import update_settings
    from app.experiments import autonomous, ledger
    from app.gtm import journey

    client, venture_id, *_ = market
    update_settings("capabilities", {"run_for_me_experiments": False, "research": True}, {})
    snapshot = autonomous._snapshot(venture_id)
    snapshot["journey"]["icp"].update(problem="", needs_input=True,
        known_checks={"problem": {"status": known_status, "value": snapshot.get("idea_offer"), "source": "Your idea description"}})
    monkeypatch.setattr(autonomous, "_snapshot", lambda _venture: deepcopy(snapshot))
    identifier, = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": f"You can interview {snapshot['journey']['icp']['who']}.",
        "action": "Interview eight consenting people.", "success_measure": "Pass if three agree to a trial."}])
    unrequested = []

    def interview(*args, **kwargs):
        unrequested.append(True)
        return experiments._reply("Your saved interview is running.")

    monkeypatch.setattr(experiments, "review_test_prepare", interview)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message":
        "Run for me one public-source demand scan for my saved idea and save the findings"})
    assert response.status_code == 200, response.text
    assert not unrequested, response.json()["body_md"]
    assert "problem" in response.json()["body_md"].lower()
    assert response.json()["kind"] == "experiments"
    assert ledger.get(identifier, venture_id)["state"] == "proposed"
    update_icp = journey.update_icp

    def save_problem(*args, **kwargs):
        result = update_icp(*args, **kwargs)
        snapshot["journey"]["icp"].update(problem=kwargs["problem"], needs_input=False,
            known_checks={"problem": {"status": "known", "value": kwargs["problem"]}})
        return result

    monkeypatch.setattr(journey, "update_icp", save_problem)
    answer = client.post(f"/ventures/{venture_id}/chat", json={"message": market[3]["icp"]["problem"]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["kind"] == "experiment_running", answer.json()["body_md"]
    run_jobs()
    saved = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    assert saved["state"] == "concluded" and saved["result"]["sources"]
    assert not unrequested and ledger.get(identifier, venture_id)["state"] == "proposed"


@pytest.mark.parametrize("hypothesis", [
    "Railway maintenance teams would pay for fault alerts",
    "Weekend cyclists would pay for route coaching",
])
@pytest.mark.parametrize("request_text,run", [
    ("Try a quick test to see if {}.", False),
    ("Could you test whether {}?", False),
    ("Try an experiment for {}.", False),
    ("Run a small experiment to test whether {}.", True),
    ("Test whether {}.", True),
])
def test_stated_hypothesis_prepares_only_the_requested_work(venture, monkeypatch, hypothesis, request_text, run):
    from app.chat import experiments
    from app.experiments import ledger, recruitment

    prepared = []
    monkeypatch.setattr(experiments, "_idea_gate", lambda _: None)
    monkeypatch.setattr(ledger, "autopilot_enabled", lambda: True)
    monkeypatch.setattr(ledger, "list_for_venture", lambda _: pytest.fail("A stated hypothesis must not pick a saved test"))

    def prepare(venture_id, payload, *, plan_only=False):
        prepared.append((payload, plan_only))
        return experiments._reply("Your requested plan is ready. Next: review your test.")

    monkeypatch.setattr(recruitment, "prepare", prepare)
    experiments.start_reply("venture", request_text.format(hypothesis))
    assert len(prepared) == 1
    payload, plan_only = prepared[0]
    assert payload["hypothesis"].rstrip(".!?") == hypothesis
    assert payload["run"] is run and plan_only is not run
    assert not payload.get("experiment_id")


def test_shown_payment_plan_runs_by_text_and_button(market, monkeypatch):
    from app.chat import customers, experiments
    from app.experiments import ledger, recruitment

    client, venture_id, _, view, *_ = market
    view["icp"]["who"] = "equipment rental coordinators" if view["icp"]["mode"] == "company" else "hobby astronomers"
    save_market_context(venture_id, view)
    audience = view["icp"]["who"]
    plan = {"hypothesis": f"{audience} will reserve your offer",
        "action": f"Interview eight {audience}. Ask whether they will commit to a conditional pre-order. Record each answer in your interview notes.",
        "success_measure": "Pass: four or more of the eight give a clear yes to a conditional pre-order. Fail: five or more say no. Otherwise inconclusive."}
    calls = []
    searches = []

    async def search(venture, action, payload, actor_subject):
        searches.append(payload)
        return experiments._reply("Your test is running. Next: review the people found for your interview invitations.",
            [{"label": "Review your test", "action": "experiment.list"}])

    async def replay(system, user, venture, **kwargs):
        calls.append(user)
        return plan

    monkeypatch.setattr(experiments, "_llm_json", replay)
    monkeypatch.setattr(customers, "execute", search)
    shown = client.post(f"/ventures/{venture_id}/chat", json={"message": f"Design an experiment to find out whether {audience} would pay for this."}).json()
    runner = next(action for action in shown["actions"] if action["label"] == "Run it for me")
    selected = recruitment.selected(venture_id, runner["payload"]["experiment_id"])
    assert runner["payload"]["experiment_id"] == selected["id"]
    assert ledger.get(selected["id"], venture_id)["state"] == "proposed"
    for request in ({"message": "Run it for me"}, {"action": runner["action"], "payload": runner["payload"]}):
        endpoint = "chat/confirm" if request.get("action") else "chat"
        reply = client.post(f"/ventures/{venture_id}/{endpoint}", json=request)
        assert reply.status_code == 200, reply.text
        assert ledger.get(selected["id"], venture_id)["state"] == "running", reply.json()["body_md"]
        assert "running" in reply.json()["body_md"].lower()
        assert not any(action.get("confirmation") for action in reply.json()["actions"])
        assert not any(action["label"] == "Run it for me" for action in reply.json()["actions"])
    assert len(calls) == 1
    assert all(payload["note"] == plan["hypothesis"] for payload in searches)


def test_saved_payment_criteria_control_reported_verdict(market, monkeypatch):
    from app.chat import experiments
    from app.experiments import ledger, recruitment
    from core.clock import utcnow

    client, venture_id, _, view, *_ = market
    view["icp"]["who"] = "equipment rental coordinators" if view["icp"]["mode"] == "company" else "hobby astronomers"
    save_market_context(venture_id, view)
    audience = view["icp"]["who"]
    measure = ("Pass: four or more of the eight give a clear yes to a conditional pre-order. Fail: five or more say no. Otherwise inconclusive."
        if view["icp"]["mode"] == "company" else
        "Interview eight people. The test succeeds when at least half commit to a conditional pre-order; it fails when a majority decline; otherwise the evidence is inconclusive.")
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"{audience} will reserve your offer",
        "action": f"Interview eight {audience} about a conditional pre-order.", "success_measure": measure}])
    recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True}, plan_only=True)
    seen = []

    async def replay(system, user, venture, **kwargs):
        seen.append(user)
        partial = "talked to 3" in user
        return {"ran": True, "outcome": "inconclusive" if partial else "supported", "criteria_met": not partial,
            "criteria_failed": False, "complete": False, "sample_size": 3 if partial else 8, "planned_sample_size": 8,
            "provenance": {"source": "Your anonymized interview notes", "method": "Interviews", "observed_at": utcnow().isoformat()},
            "learning": "You reported two commitments; five interviews remain." if partial else "You reported five commitments against your agreed minimum of four."}

    monkeypatch.setattr(experiments, "_llm_json", replay)
    for count, yes, state in ((3, 2, "running"), (8, 5, "concluded")):
        report = (f"I talked to {count} {audience} and {yes} said they would commit to a conditional pre-order. "
            "I kept anonymized interview notes today.")
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": report})
        assert response.status_code == 200, response.text
        reply = response.json()
        saved = ledger.get(identifier, venture_id)
        assert saved["state"] == state, reply["body_md"]
        assert measure in seen[-1]
        assert "not described" not in reply["body_md"] and "Kind words" not in reply["body_md"]
        assert "Next time" not in reply["body_md"] and "no agreed" not in reply["body_md"]
        assert not any(action.get("confirmation") for action in reply["actions"])
        if state == "running":
            assert ("five" in reply["body_md"].lower() or "5" in reply["body_md"]) and "remain" in reply["body_md"].lower()
        else:
            assert saved["outcome"] == "supported" and saved["result_text"] == report
            assert "reported" in reply["body_md"] and "passed" in reply["body_md"] and "not proof" in reply["body_md"]
            assert not any(action.get("action") == "experiment.revise" for action in reply["actions"])


def test_other_experiments_have_named_run_buttons(market):
    from app.chat import experiments, next_moves
    from app.experiments import ledger

    _, venture_id, _, view, *_ = market
    view["icp"]["who"] = "equipment rental coordinators" if view["icp"]["mode"] == "company" else "hobby astronomers"
    save_market_context(venture_id, view)
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"{view['icp']['who']} will book a trial",
        "action": "Interview five people about a trial.", "success_measure": "Three bookings from five interviews."}])
    for reply in (experiments.list_reply(venture_id), next_moves.compose(venture_id)):
        runners = [action for action in reply["actions"] if action.get("action") in {"experiment.prepare", "experiment.recommendation.prepare"}]
        if reply.get("kind") == "experiments":
            assert runners
            primary = [action for action in runners if action.get("style") == "primary"]
            assert [action["label"] for action in primary] == ["Run it for me"]
        assert all(action["label"] != "Run it for me" for action in runners if action.get("style") != "primary")
        assert all(action.get("payload", {}).get("experiment_id") for action in runners)
    assert ledger.get(identifier, venture_id)["state"] == "proposed"


@pytest.mark.parametrize("audience,problem", [
    ("aquaculture hatchery managers", "planning feed deliveries"),
    ("urban cyclists", "finding secure overnight parking"),
])
def test_typed_interview_invitations_keep_the_selected_test_and_send_route(interview_plan, audience, problem):
    from app.autopilot import outreach
    from app.chat import customers, offers
    from app.experiments import recruitment

    market, identifier = interview_plan
    market[3]["icp"].update(who=audience, problem=problem)
    save_market_context(market[1], market[3])
    recruitment.prepare(market[1], {"experiment_id": identifier, "run": True})
    reply = offers.draft_prepare(market[1], request=f"Draft interview invitations for {audience} about {problem}. Do not send them yet.")
    payload = reply["actions"][0]["confirmation"]["resulting_state"]
    assert payload.get("batch") is True
    outreach._save_draft(market[1], {"to": "invite@local.example", "channel": "email", "day": 1,
        "state": "draft", "requires_batch_approval": True, "subject": "Your recent experience",
        "body": f"Could you discuss {problem} in a 15-minute call?"})
    market[3]["drafts"] = outreach.drafts(market[1])
    assert customers.command(market[1], "Send the invitations.")[0] == "send"
    assert customers.command(market[1], "Do not send the invitations.") is None


@pytest.mark.parametrize("audience,problem", [
    ("commercial aquarium operators", "detecting filtration failures"),
    ("home music students", "keeping a practice routine"),
])
def test_next_private_plan_receives_the_actual_previous_result(interview_plan, monkeypatch, audience, problem):
    from app.chat import experiments
    from app.experiments import ledger

    market, identifier = interview_plan
    market[3]["icp"].update(who=audience, problem=problem)
    save_market_context(market[1], market[3])
    report = f"I interviewed five {audience}. Nobody agreed to a dated trial. I saved my notes today."
    learning = "You did not establish willingness to commit."
    ledger.record_result(identifier, market[1], True, report, "refuted", learning, sample_size=5,
        provenance={"source": "Your anonymized interview notes", "method": "Interviews", "observed_at": utcnow().isoformat()})

    async def design(system, user, target, **kwargs):
        assert target == market[1] and report in user and learning in user and "refuted" in user
        return {"hypothesis": f"You can learn the barrier to committing for {audience}.",
            "action": f"Interview five {audience} about {problem} and the barrier to committing.",
            "success_measure": "Pass if three name the same barrier; fail if none do; otherwise inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", design)
    reply = asyncio.run(experiments.execute(market[1], "experiment.start", {"text": "Design the next experiment."}, None))
    assert "barrier to committing" in reply["body_md"]


@pytest.mark.parametrize("audience", ["municipal lift engineers", "backyard telescope owners"])
def test_private_followup_preserves_unsent_invitations(interview_plan, monkeypatch, audience):
    from app.autopilot import outreach
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, recruitment
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    market, first = interview_plan
    vid = market[1]
    recruitment.prepare(vid, {"experiment_id": first, "run": True})
    draft = outreach._save_draft(vid, {"to": "interview@trial.invalid", "channel": "email", "day": 1,
        "state": "draft", "requires_batch_approval": True, "subject": "Your recent experience",
        "body": f"Could we learn how {audience} work today?"})
    with db()() as session:
        row = session.get(Experiment, first)
        row.context = json.dumps({**json.loads(row.context), "outreach_draft_ids": [draft]})
        session.commit()
    before = outreach.drafts(vid, "draft")
    second, = ledger.create_from_tests(vid, "chat", [{"hypothesis": f"{audience} would try a second approach",
        "action": f"Interview five {audience}."}])
    recruitment.prepare(vid, {"experiment_id": second, "run": True}, plan_only=True)
    assert before and outreach.drafts(vid, "draft") == before
    assert not outreach.drafts(vid, "sent")


@pytest.mark.parametrize("audience", ["commercial kiln technicians", "neighborhood jigsaw clubs"])
def test_followup_planner_receives_current_sourced_scan(interview_plan, monkeypatch, audience):
    from app.chat import experiments
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    market, _identifier = interview_plan
    vid, workspace = market[1:3]
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    source = "https://observations.invalid/current"
    with db()() as session:
        current = ledger.current_direction(session, vid)
        for label, direction in (("current", current), ("retired", "rev_retired")):
            session.add(Experiment(id=f"exp_scan_{label}", venture_id=vid, workspace_id=workspace, kind=ledger.KIND,
                hypothesis=f"{audience} need a reminder", state="concluded", outcome="refuted",
                context=json.dumps({"runner": "system", "spec": {"direction": direction},
                    "result": {"insight": f"{label} scan found workarounds", "sources": [{"url": source}]}})))
        session.commit()

    async def design(_system, user, _target, **_kwargs):
        assert "current scan found workarounds" in user and "refuted" in user and source in user
        assert "retired scan found workarounds" not in user
        return {"hypothesis": f"You can test whether {audience} pay for fewer workarounds.",
                "action": f"Interview five {audience}.", "success_measure": "Pass if three agree to try it."}

    monkeypatch.setattr(experiments, "_llm_json", design)
    reply = asyncio.run(experiments.execute(vid, "experiment.start", {"text": "Design another experiment."}, None))
    assert "fewer workarounds" in reply["body_md"]


@pytest.mark.parametrize("audience,problem", [
    ("industrial oven maintenance managers", "preventing heat drift"),
    ("family gardeners", "keeping seedlings watered"),
])
@pytest.mark.parametrize("concluded", [False, True])
@pytest.mark.parametrize("button", [False, True])
def test_requested_next_test_does_not_reuse_the_same_hypothesis(interview_plan, monkeypatch, audience, problem, concluded, button):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.chat.store import CLIENT_REQUEST_ID
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, recruitment
    from app.state import db

    market, identifier = interview_plan
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {
        "journey": {"icp": {"who": audience, "problem": problem}}})
    recruitment.prepare(market[1], {"run": True, "experiment_id": identifier})
    draft_id = outreach._save_draft(market[1], {"to": "invite@local.example", "channel": "email", "day": 1,
        "state": "draft", "subject": "Your experience", "body": f"You can discuss {problem}."})
    with db()() as session:
        row = session.get(Experiment, identifier)
        row.context = json.dumps({**ledger._ctx(row), "outreach_draft_ids": [draft_id]})
        session.commit()
    if concluded:
        ledger.record_result(identifier, market[1], True, f"I interviewed five {audience}; all declined.",
            "refuted", "Your original test did not pass.", sample_size=5,
            provenance={"source": "Your anonymized interview notes", "method": "Interviews", "observed_at": utcnow().isoformat()})
    original = ledger.get(identifier, market[1])
    plan = {"hypothesis": original["hypothesis"],
        "action": f"Interview five new {audience} about {problem} and the barrier to committing.",
        "success_measure": "Pass if three name the same barrier; fail if none do; otherwise inconclusive."}

    async def design(*args, **kwargs):
        return plan

    monkeypatch.setattr(experiments, "_llm_json", design)
    token = CLIENT_REQUEST_ID.set("requested-next-test")
    try:
        payload = {} if button else {"text": "Design the next experiment."}
        reply = asyncio.run(experiments.execute(market[1], "experiment.start", payload, None))
        replay = asyncio.run(experiments.execute(market[1], "experiment.start", payload, None))
    finally:
        CLIENT_REQUEST_ID.reset(token)
    runs = [action for action in reply["actions"] if action.get("action") == "experiment.recommendation.prepare"]
    assert len(runs) == 1 and runs[0]["payload"]["experiment_id"] != identifier
    next_id = runs[0]["payload"]["experiment_id"]
    assert next_id in {action.get("payload", {}).get("experiment_id") for action in replay["actions"]}
    assert len(ledger.list_for_venture(market[1])) == 2
    assert ledger.get(identifier, market[1]) == original
    assert recruitment.selected(market[1])["id"] == next_id
    assert next(item for item in outreach.drafts(market[1]) if item["doc_id"] == draft_id)["state"] == "draft"
    recruitment.prepare(market[1], {"run": True, "experiment_id": next_id})
    assert next(item for item in outreach.drafts(market[1]) if item["doc_id"] == draft_id)["state"] == "review_abandoned"
    assert not market[6]


@pytest.mark.parametrize("audience,problem", [
    ("archive digitization supervisors", "scheduling fragile scans"),
    ("home bakers", "reducing ingredient waste"),
])
@pytest.mark.parametrize("outcome,objective", [
    ("supported", "completed trials"), ("refuted", "barrier"), ("inconclusive", "missing evidence"),
])
def test_next_plan_fallback_changes_with_your_reported_verdict(interview_plan, monkeypatch, audience, problem, outcome, objective):
    from app.chat import experiments
    from app.experiments import ledger, recruitment

    market, identifier = interview_plan
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {
        "journey": {"icp": {"who": audience, "problem": problem}}})
    ledger.record_result(identifier, market[1], True, f"I interviewed five {audience}; I saved anonymous notes today.",
        outcome, "Your next test needs to address what this report established or left unknown.", sample_size=5,
        provenance={"source": "Your anonymized interview notes", "method": "Interviews", "observed_at": utcnow().isoformat()})
    original = ledger.get(identifier, market[1])
    calls = []

    async def invalid(system, user, target, **kwargs):
        calls.append(user)
        return {}

    monkeypatch.setattr(experiments, "_llm_json", invalid)
    reply = asyncio.run(experiments.execute(market[1], "experiment.start", {"text": "Design the next experiment."}, None))
    selected = recruitment.selected(market[1])
    assert len(calls) == 2 and all(outcome in user for user in calls)
    assert objective in selected["hypothesis"].lower() and objective in reply["body_md"].lower()
    assert selected["action"] and selected["measure"] and selected["id"] != identifier
    assert "inconclusive" in selected["measure"].lower()
    assert ledger.get(identifier, market[1]) == original and not market[6]


@pytest.mark.parametrize("audience,problem", [
    ("museum collection managers", "tracking loan returns"),
    ("amateur stargazers", "planning clear-sky outings"),
])
@pytest.mark.parametrize("autopilot", [False, True])
def test_catalog_offers_private_design_alongside_existing_tests(interview_plan, monkeypatch, audience, problem, autopilot):
    from app.chat import experiments
    from app.credits import policy
    from app.experiments import ledger

    market, _identifier = interview_plan
    market[3]["icp"].update(who=audience, problem=problem)
    save_market_context(market[1], market[3])
    snapshot = experiments._experiment_snapshot(market[1])
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {
        **snapshot, "journey": {"icp": market[3]["icp"]},
        "confirmed_scope": {"customer": audience, "problem": problem}})
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": autopilot}, "test")
    before = ledger.list_for_venture(market[1])
    reply = experiments.list_reply(market[1])
    designs = [action for action in reply["actions"] if action.get("action") == "experiment.start"]
    assert len(designs) == 1 and designs[0].get("payload") == {}
    assert not designs[0].get("confirmation")
    assert ledger.list_for_venture(market[1]) == before
    assert not market[6]


@pytest.mark.parametrize("audience,problem", [
    ("museum collection managers", "tracking loan returns"),
    ("amateur stargazers", "planning clear-sky outings"),
])
def test_design_button_prepares_a_complete_private_plan(interview_plan, monkeypatch, audience, problem):
    from app.chat import experiments
    from app.experiments import ledger

    market, _identifier = interview_plan
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {
        "journey": {"icp": {"who": audience, "problem": problem}},
        "confirmed_scope": {"customer": audience, "problem": problem},
    })
    calls = []
    plan = {"hypothesis": f"You can help {audience} with {problem}.",
        "action": "Hold five consenting 25-minute interviews this week. Keep anonymous notes.",
        "success_measure": "Pass if three make dated paid commitments. Fail if all decline; otherwise inconclusive."}

    async def design(system, user, target, **kwargs):
        calls.append(user)
        assert audience in user and problem in user and target == market[1]
        return plan

    monkeypatch.setattr(experiments, "_llm_json", design)
    reply = asyncio.run(experiments.execute(market[1], "experiment.start", {}, None))
    assert len(calls) == 1 and all(value in reply["body_md"] for value in plan.values())
    assert any(item["hypothesis"] == plan["hypothesis"] for item in ledger.list_for_venture(market[1]))
    assert not any(item.get("confirmation") for item in reply["actions"])


@pytest.mark.parametrize("audience,problem", [
    ("freight depot managers", "scheduling loading bays"),
    ("recreational swimmers", "finding uncrowded lanes"),
])
def test_prepared_private_plan_offers_its_own_requested_run(interview_plan, monkeypatch, audience, problem):
    from app.chat import experiments
    from app.experiments import ledger, recruitment

    market, _identifier = interview_plan
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {
        "journey": {"icp": {"who": audience, "problem": problem}}})
    plan = {"hypothesis": f"You can help {audience} with {problem}.",
        "action": f"Interview five {audience} for 25 minutes about {problem}. Keep notes private.",
        "success_measure": "Pass if three make dated commitments. Fail if all five decline; otherwise inconclusive."}

    async def design(*args, **kwargs):
        return plan

    monkeypatch.setattr(experiments, "_llm_json", design)
    reply = asyncio.run(experiments.execute(market[1], "experiment.start", {}, None))
    identifier = next(row["id"] for row in ledger.list_for_venture(market[1]) if row["hypothesis"] == plan["hypothesis"])
    runs = [action for action in reply["actions"] if action.get("action") == "experiment.recommendation.prepare"]
    assert len(runs) == 1 and runs[0]["payload"]["experiment_id"] == identifier
    assert len(reply["actions"]) == 1
    assert not runs[0].get("confirmation") and runs[0].get("style") == "primary"
    assert ledger.get(identifier, market[1])["state"] == "proposed"
    assert recruitment.selected(market[1])["id"] == identifier and not market[6]


@pytest.mark.parametrize("audience,problem", [
    ("freight depot managers", "scheduling loading bays"),
    ("recreational swimmers", "finding uncrowded lanes"),
])
@pytest.mark.parametrize("model_minutes", ["15", "25"], ids=["model-mismatch", "model-matches"])
def test_invitation_drafts_replace_an_incompatible_discovery_call_length(interview_plan, monkeypatch, audience, problem, model_minutes):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.db.models import GtmContact
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, recruitment
    from app.state import db
    from core.clock import utcnow

    market, identifier = interview_plan
    venture_id, workspace_id = market[1:3]
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {
        "journey": {"icp": {"who": audience, "problem": problem}},
    })
    with db()() as session:
        session.get(Experiment, identifier).action = "Hold five 25-minute interviews this week. Keep anonymous notes."
        session.add(GtmContact(id="invitation_person", workspace_id=workspace_id, email="invite@local.example",
            source=f"venture:{venture_id}:public", recipient_geo="US", email_confidence="risky",
            enrichment_json=json.dumps({"contact_source_url": "https://local.example/team"}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    previous = outreach._save_draft(venture_id, {"contact_id": "invitation_person", "to": "invite@local.example",
        "channel": "email", "day": 1, "state": "draft", "requires_batch_approval": True,
        "subject": "Your experience", "body": f"Could you discuss {problem} in a 15-minute call?"})
    recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True})
    calls = []

    async def invitations(target, recipients, request):
        calls.append(request)
        assert target == venture_id and audience in request and problem in request and "25-minute" in request
        return {recipient["to"].lower(): {"subject": "Your recent experience", "body":
            f"Could you discuss {problem} in a {model_minutes}-minute call?"} for recipient in recipients}

    monkeypatch.setattr(outreach, "_llm_drafts", invitations)
    reply = asyncio.run(recruitment.draft(venture_id, ledger.get(identifier, venture_id)))
    waiting = outreach.drafts(venture_id, "draft")
    assert len(calls) == 1 and len(waiting) == 1 and waiting[0]["doc_id"] != previous
    assert outreach._interview_minutes(waiting[0]["body"]) == "25" and waiting[0]["requires_batch_approval"] is True
    assert "Your email address is listed publicly on local.example." in waiting[0]["body"]
    assert outreach.OPT_OUT in waiting[0]["body"] and not any(action.get("confirmation") for action in reply["actions"])
    assert next(item for item in outreach.drafts(venture_id) if item["doc_id"] == previous)["state"] == "review_abandoned"


@pytest.mark.parametrize("audience", ["commercial theatre operators", "novice birdwatchers"])
def test_next_experiment_retires_only_the_previous_unsent_invitations(interview_plan, audience):
    from app.autopilot import outreach
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, recruitment
    from app.state import db

    market, identifier = interview_plan
    venture_id = market[1]
    recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True})
    drafts = [outreach._save_draft(venture_id, {"to": "invite@local.example", "channel": "email", "day": 1,
        "state": state, "requires_batch_approval": True, "subject": "Your recent experience",
        "body": f"Could you share your experience as one of the {audience}?"}) for state in ("draft", "sent", "delivery_unconfirmed")]
    unrelated = outreach._save_draft(venture_id, {"to": "other@local.example", "channel": "email", "day": 2,
        "state": "draft", "subject": "Your separate question", "body": "You can reply with your recent experience."})
    with db()() as session:
        row = session.get(Experiment, identifier)
        row.context = json.dumps({**json.loads(row.context), "outreach_draft_ids": drafts})
        session.commit()
    next_id, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"You can test a different need of {audience}.",
        "action": "Hold five interviews about a different need.", "success_measure": "Pass if three describe that need."}])
    recruitment.prepare(venture_id, {"experiment_id": next_id, "run": True})
    states = {item["doc_id"]: item["state"] for item in outreach.drafts(venture_id)}
    assert states[drafts[0]] == "review_abandoned"
    assert states[drafts[1]] == "sent" and states[drafts[2]] == "delivery_unconfirmed" and states[unrelated] == "draft"
    assert recruitment.selected(venture_id)["id"] == next_id


@pytest.mark.parametrize("audience", ["recycling depot supervisors", "weekend canoeists"])
def test_coached_result_keeps_the_plan_verdict_and_missing_evidence(interview_plan, monkeypatch, audience):
    from app.core.local_mode import is_local
    from app.chat import experiments
    from app.experiments import ledger

    market, identifier = interview_plan
    report = f"I interviewed five {audience}. All five declined a paid trial. I saved notes today."
    explanation = "Your five completed interviews meet the agreed fail criterion because all five declined."

    async def classify(*args, **kwargs):
        return {"ran": True, "outcome": "refuted", "learning": explanation,
            "criteria_met": False, "criteria_failed": True}

    monkeypatch.setattr(experiments, "_llm_json", classify)
    extracted = asyncio.run(experiments.extract(market[1], report, "You can obtain paid commitments.",
        "Interview five matched people.", "Pass if three commit; fail if all five decline; otherwise inconclusive."))
    assert extracted["outcome"] == "refuted" and explanation in extracted["learning"]
    reply = asyncio.run(experiments.record_run(market[1], {**extracted, "experiment_id": identifier, "result_text": report}))
    assert explanation in reply["body_md"] and ("Add evidence" if is_local() else "did not pass") in reply["body_md"]
    saved = ledger.get(identifier, market[1])
    if is_local():
        assert saved["state"] == "running" and saved["outcome"] is None and saved["needs_evidence"]
        assert saved["evidence_type"] == "founder_reported" and not saved["provenance"]
    else:
        assert saved["state"] == "concluded" and saved["outcome"] == "refuted"


@pytest.mark.parametrize("audience", ["commercial laundry supervisors", "community chess players"])
def test_shown_invitations_have_send_receipts_without_authorizing_unseen_drafts(interview_plan, monkeypatch, audience):
    from app.autopilot import outreach
    from app.chat import customers
    from app.db.models import GtmContact
    from app.experiments import ledger, recruitment
    from app.state import MEM, db
    from core.clock import utcnow

    market, identifier = interview_plan
    venture_id, workspace_id = market[1:3]
    recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True})
    with db()() as session:
        for index in range(3):
            session.add(GtmContact(id=f"receipt_person_{index}", workspace_id=workspace_id,
                email=f"reader{index}@local.example", source=f"venture:{venture_id}:public", recipient_geo="US",
                email_confidence="risky", enrichment_json=json.dumps({"contact_source_url": "https://local.example/team"}),
                created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    shown = [outreach._save_draft(venture_id, {"contact_id": f"receipt_person_{index}",
        "to": f"reader{index}@local.example", "channel": "email", "day": 1, "state": "draft",
        "requires_batch_approval": True, "subject": "Your recent experience",
        "body": f"Could you share your experience with {audience} in a 15-minute call?"}) for index in range(3)]
    unseen = outreach._save_draft(venture_id, {"to": "separate@local.example", "channel": "email", "day": 2,
        "state": "draft", "subject": "Your separate question", "body": "You can reply with your experience."})
    reply = asyncio.run(recruitment.draft(venture_id, ledger.get(identifier, venture_id)))
    review = MEM.get(customers._draft_review_key(venture_id)) or {}
    assert set(review.get("doc_ids") or []) == set(shown) and unseen not in review["doc_ids"]
    assert all(f"To: reader{index}@local.example" in reply["body_md"] for index in range(3))
    send = next(action for action in reply["actions"] if action.get("action") == "gtm.send")
    assert set(send["payload"]["doc_ids"]) == set(shown) and not send.get("confirmation")


@pytest.mark.parametrize("audience", ["industrial seed suppliers", "family history hobbyists"])
def test_invitation_model_receives_the_complete_plan_and_its_final_limits(interview_plan, monkeypatch, audience):
    from app.autopilot import outreach
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, recruitment
    from app.state import db

    market, identifier = interview_plan
    plan = (f"Interview five {audience} in 25-minute calls this week. "
        + "Keep anonymous notes on recent incidents, workarounds and the reasons someone might decline. " * 17
        + "Exclude minors and private records. Do not request identity documents or proof of payment.")
    assert 1500 < len(plan) < 2000
    with db()() as session:
        session.get(Experiment, identifier).action = plan
        session.commit()
    recruitment.prepare(market[1], {"experiment_id": identifier, "run": True})
    calls = []

    async def model(target, purpose, system, prompt, schema, max_tokens):
        calls.append(prompt)
        assert target == market[1] and purpose == "outreach.draft_batch"
        assert plan in prompt and "Do not request identity documents or proof of payment." in prompt
        return {"drafts": []}

    async def batch(target, request, **kwargs):
        await outreach._llm_drafts(target, [{"to": "invite@local.example", "pitch": f"A planning aid for {audience}."}], request)

    monkeypatch.setattr(outreach.llm, "ask_json", model)
    monkeypatch.setattr(outreach, "draft_batch", batch)
    asyncio.run(recruitment.draft(market[1], ledger.get(identifier, market[1])))
    assert len(calls) == 1
    with pytest.raises(ValueError, match="shorter"):
        asyncio.run(outreach._llm_drafts(market[1], [], "Keep your limits. " * 250))


@pytest.mark.parametrize("audience,problem,criteria,boundary", [
    ("freight depot managers", "scheduling loading bays",
     "Only managers who recently scheduled loading bays. Exclude vendors and people without direct scheduling responsibility.",
     "Do not request shipment records or confidential documents. Take anonymous notes with consent."),
    ("recreational swimmers", "finding uncrowded lanes",
     "Only adults who swim recreationally. Exclude instructors and people who do not choose their own swim times.",
     "Do not collect home addresses or identifying documents. Take anonymous notes with consent."),
])
def test_invitation_budget_excludes_private_plan_logistics(venture, audience, problem, criteria, boundary):
    from app.autopilot import outreach
    from app.gtm import journey

    plan = ("Keep this as your private proposed plan. "
        "Proposed schedule: prepare on December 14, 2026, using UTC until you confirm your timezone. "
        "If voluntary participation is not available, extend the schedule rather than replace missing interviews with assumptions. "
        "Recruiting approach for your later approval: seek volunteers through existing conversations without searching for contacts or posting publicly. "
        f"{criteria} {boundary} Do not substitute participants outside these criteria to fill the sample.")
    request = (f"Your ask: a 25-minute call. Interview invitations for {audience} about {problem}. "
        f"Test: Learn about recent workarounds. Plan: {plan} What to record: Anonymous accounts.")
    footer = ("Your email address is listed publicly on local.example.\n\n" + outreach.OPT_OUT
        + "\n\nReply STOP to stop these emails.\n\nFrom: you@local.example\n\nBusiness outreach · 8 Local Lane, Boston, MA 02110")
    _, body = outreach._template("a planning notebook", "Reader", "", request=request,
        word_budget=120 - len(footer.split()))
    assert problem in body and criteria in body and boundary in body
    assert outreach._interview_minutes(body) == "25"
    assert not any(text in body for text in ("proposed plan", "Proposed schedule", "Recruiting approach", "extend the schedule", "fill the sample"))
    journey.validate_draft_copy(body + "\n\n" + footer)
    _, scheduled = outreach._template("a planning notebook", "Reader", "", request=request.replace(
        "prepare on December 14, 2026, using UTC until you confirm your timezone",
        "private 25-minute calls on December 18, 2026"))
    assert "December 18, 2026" in scheduled and "private" in scheduled
    _, screened = outreach._template("a planning notebook", "Reader", "", request=request.replace(
        "seek volunteers through existing conversations without searching for contacts or posting publicly",
        "invite only consenting adults without collecting private records"))
    assert "Only consenting adults without collecting private records." in screened


@pytest.mark.parametrize("path", ["template", "budgeted-template", "missing-copy", "wrong-duration",
    "invalid-copy", "unsupported-copy", "saved-plan", "run", "review", "grounded", "manual"])
def test_procedural_plan_invitation_paths_keep_limits_and_complete_footer(market, monkeypatch, path):
    from app.autopilot import outreach
    from app.chat import customers
    from app.db.models import GtmContact, GtmOutreachSettings
    from app.experiments import ledger, recruitment
    from app.gtm import journey
    from app.planning import execute
    from app.state import db
    from core.clock import utcnow

    audience, problem, boundary = (("print shop production managers", "unclear order specifications",
        "Leave production equipment untouched.") if market[3]["icp"]["mode"] == "company" else
        ("weekend paddleboarders", "finding sheltered launch spots", "Do not collect home addresses."))
    client, venture_id, workspace_id = market[:3]
    market[3]["icp"].update(who=audience, problem=problem)
    save_market_context(market[1], market[3])
    procedure = ("Your preparation: write a detailed sampling checklist and review each recruitment source before arranging the interviews. "
        "Your proposed recruiting approach uses introductions to invite five participants and screen their recent experience. "
        "If you cannot recruit five eligible people, record the shortfall; do not broaden the sample. "
        "Your interview script: 1. Describe your latest incident and the time spent on it. 2. What did you try? "
        "You will ask about past behavior, obtain consent for notes, and compare the accounts after all interviews. "
        "Do not treat praise as commitment. Your summary must distinguish participant reports from verified evidence. ")
    limits = f"Only consenting adults. Keep notes anonymous. Do not request account passwords. You will not share recordings. {boundary}"
    plan = f"Arrange 25-minute calls between December 14 and December 18, 2026. {procedure * 2}{limits}"
    assert 1400 < len(plan) < 2000
    identifier, = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": f"You can learn how {audience} handle {problem}.", "action": plan,
        "success_measure": "Pass if three describe recent workarounds; fail if none do; otherwise inconclusive."}])
    request = (f"Your ask: a 25-minute call. Interview invitations for {audience} about {problem}. "
        f"Test: Learn about recent workarounds. Plan: {plan} What to record: Anonymous accounts.")
    monkeypatch.setattr(outreach, "saved_pitch", lambda _: "a planning notebook")
    monkeypatch.setattr(outreach, "venture_description", lambda _: "a planning notebook")
    monkeypatch.setattr(outreach, "site_url", lambda _: None)
    monkeypatch.setattr(outreach.mailboxes, "resolve_send_mailbox", lambda *args, **kwargs: {"email": "you@local.example"})
    with db()() as session:
        session.add(GtmOutreachSettings(workspace_id=workspace_id, sender_postal_address="8 Local Lane, Boston, MA 02110",
            created_at=utcnow(), updated_at=utcnow()))
        session.add(GtmContact(id="procedural_invitation_person", workspace_id=workspace_id, email="reader@local.example",
            source=f"venture:{venture_id}:public", recipient_geo="US", email_confidence="risky",
            enrichment_json=json.dumps({"contact_source_url": "https://local.example/team"}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()

    async def modeled(*args, **kwargs):
        body = "Could you share your experience in a 25-minute call?"
        if path == "wrong-duration":
            body = body.replace("25", "10")
        elif path == "invalid-copy":
            body = "You can skip any topic. " * 30 + body
        elif path == "unsupported-copy":
            body = "Our app automatically sends invoices for you. " + body
        else:
            return {}
        return {"reader@local.example": {"subject": "Your experience", "body": body}}

    monkeypatch.setattr(outreach, "_llm_drafts", modeled)
    sender = {"workspace_id": workspace_id, "email": "From: you@local.example"}
    draft = {"contact_id": "procedural_invitation_person", "to": "reader@local.example", "channel": "email"}
    with db()() as session:
        footer = outreach.complete_sender_body(session, venture_id, {**draft, "body": outreach._finish("", None)}, sender)
    if path == "manual":
        with db()() as session:
            contact = session.get(GtmContact, draft["contact_id"])
            contact.email = None
            contact.enrichment_json = json.dumps({"instagram": "research_reader"})
            session.commit()
        asyncio.run(outreach.draft_batch(venture_id, request=request, review_only=True))
        saved, = outreach.drafts(venture_id, "manual")
        assert saved["requires_batch_approval"] is True
        body = saved["body"]
    elif path in {"template", "budgeted-template", "grounded"}:
        _, body = outreach._template("a planning notebook", "Reader", "", request=request,
            word_budget=120 - len(footer.split()) if path == "budgeted-template" else None)
        if path == "grounded":
            body = customers._grounded_draft(venture_id, {**draft, "body": "Hi,\n\nYour old draft.\n\n" + footer}, request)
        else:
            with db()() as session:
                body = outreach.complete_sender_body(session, venture_id, {**draft, "body": outreach._finish(body, None)}, sender)
    elif path == "review":
        body = asyncio.run(execute._review_drafts(venture_id, "Draft one interview email for me to review",
            "Founder request: Draft one email to reader@local.example. " + request))
        body = body.split("Subject:", 1)[1].split("\n\n", 1)[1]
    else:
        if path in {"saved-plan", "run"}:
            if path == "run":
                prepared = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.run_review",
                    "payload": {"experiment_id": identifier, "run": False}})
                assert prepared.status_code == 200
                reply = client.post(f"/ventures/{venture_id}/chat", json={"message": "Run it for me."}).json()
            else:
                recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True}, plan_only=True)
                reply = asyncio.run(recruitment.draft(venture_id, ledger.get(identifier, venture_id)))
            assert not any(action.get("confirmation") for action in reply["actions"])
        else:
            asyncio.run(outreach.draft_batch(venture_id, request=request, review_only=True))
        saved, = outreach.drafts(venture_id, "draft")
        assert saved["requires_batch_approval"] is True
        body = saved["body"]
    assert boundary in body and "Only consenting adults." in body and "notes anonymous" in body
    assert "consent for notes" in body and "account passwords" in body
    assert "Do not share recordings." in body
    assert "December 14" in body and "December 18" in body
    assert "private" not in body or "call" in body
    assert not any(text in body for text in ("sampling checklist", "Your preparation", "Your interview script", "record the shortfall", "verified evidence"))
    assert outreach._URL.sub("", body).count("?") == 1
    if path != "manual":
        assert len(body.split()) <= 120
        assert all(paragraph.removeprefix("From: ") in body for paragraph in footer.split("\n\n"))
        journey.validate_draft_copy(body, venture_id=venture_id, generated=True)
    assert ledger.get(identifier, venture_id)["action"] == plan
    assert not market[6]


def test_model_invitation_budget_includes_sender_and_compliance_footer(market, monkeypatch):
    from app.autopilot import llm, outreach
    from app.core import local_mode
    from app.db.models import GtmContact, GtmOutreachSettings
    from app.state import db

    _, venture_id, workspace_id, *_ = market
    postal = "18 Maple Street Suite 14 Unit 9 Harbor Town Washington State 98100 United States"
    if local_mode.is_local():
        from app.core.local_secrets import update_settings

        update_settings("smtp", {"postal_address": postal}, {})
    email = "budget-reader@local.example"
    monkeypatch.setattr(outreach, "saved_pitch", lambda _: "a planning notebook")
    monkeypatch.setattr(outreach, "venture_description", lambda _: "a planning notebook")
    monkeypatch.setattr(outreach, "site_url", lambda _: None)
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    monkeypatch.setattr(outreach.mailboxes, "resolve_send_mailbox", lambda *args, **kwargs: {"email": "you@local.example"})
    with db()() as session:
        session.add(GtmOutreachSettings(workspace_id=workspace_id, sender_postal_address=postal,
            created_at=utcnow(), updated_at=utcnow()))
        session.add(GtmContact(id="budget_reader", workspace_id=workspace_id, email=email,
            source=f"venture:{venture_id}:public", recipient_geo="US", email_confidence="risky",
            enrichment_json=json.dumps({"contact_source_url": "https://local.example/team"}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    captured = []
    async def modeled(target, action, system, prompt, *args):
        captured.append((system, prompt))
        return {"drafts": [{"to": email, "subject": "Your experience", "body": "Could you share your experience in a 25-minute call?"}]}
    monkeypatch.setattr(llm, "ask_json", modeled)
    asyncio.run(outreach.draft_batch(venture_id, request="Your ask: a 25-minute call.", review_only=True))
    saved, = outreach.drafts(venture_id, "draft")
    with db()() as session:
        footer = outreach.complete_sender_body(session, venture_id, {**saved, "body": outreach._finish("", None)},
            {"workspace_id": workspace_id, "email": "From: you@local.example"})
    assert f"under {120 - len(footer.split())} words" in captured[0][0]
    assert "word_budget" not in captured[0][1]
    assert postal in saved["body"] and outreach.OPT_OUT in saved["body"]
    assert not market[6]


@pytest.mark.parametrize("schedule_style", ["activities-first", "dated-clauses", "planned-calls", "targeted-calls", "scheduled-steps"])
def test_invitation_budget_excludes_activity_first_preparation_schedule(market, schedule_style):
    from app.autopilot import outreach
    from app.gtm import journey

    audience, problem = market[3]["icp"]["who"], market[3]["icp"]["problem"]
    plan = ("Keep this plan private. Schedule preparation for December 7, 2026, and six 30-minute workflow "
        "interviews for December 14-18, using UTC until you confirm your timezone. "
        "Participation must be voluntary. Exclude people without recent firsthand experience. "
        "Use this script: 'Discuss your workflow only. Do not share personal identifiers, confidential records, "
        "credentials or live screens. You may skip questions or stop.' "
        "Keep anonymous notes with role, workflow steps, frequency, estimated time and uncertainty.")
    if schedule_style == "dated-clauses":
        plan = ("Proposed schedule, using UTC until you confirm your timezone: December 7, prepare consent wording and interview questions; "
            "December 14-18, conduct six 30-minute interviews if you have willing participants; "
            "December 21, review anonymized notes. Use only people who already agreed to participate; "
            "do not identify or contact anyone now. Exclude people without recent firsthand experience and minors. "
            "Do not select only people who already express interest in your idea. Participation must be voluntary. "
            "Please avoid names, account details and exact amounts. Do not request confidential records, credentials or live screens. "
            "Record only anonymous summaries of methods, recent incidents, consequences, workarounds and privacy needs.")
    elif schedule_style == "planned-calls":
        plan = ("Keep your plan private; do not send emails or invitations, find contacts, publish or build anything. "
            "No specific problem is confirmed: explore habits without assuming pain or demand. "
            "Schedule: Use UTC until you confirm your timezone. Plan six individual 30-minute interviews for December 14-18, 2026; "
            "privately summarize on December 21. Recruiting, only if you later authorize it: "
            f"Use existing, permission-based relationships to seek six {audience}. "
            "Exclude people without recent firsthand experience, minors and anyone seeking urgent assistance. "
            "Participation is voluntary; participants may skip questions or stop. "
            "Script: Explain that you study habits, not sell or offer professional advice. "
            "Ask permission for anonymous notes; do not record. Ask: How do you currently plan your day? "
            "Only afterward ask what, if anything, an optional view of your progress would change about your habits. "
            "Do not request account access, confidential records, credentials, exact balances or identifying details. "
            "Use approximate or hypothetical amounts if needed. Keep de-identified notes private; delete them within 30 days of the summary.")
    elif schedule_style == "targeted-calls":
        plan = ("Keep your plan and notes private. Plan six participants across at least three groups. "
            "Exclude people without recent firsthand experience. Do not find contacts, send emails, publish or build a solution. "
            "For later recruiting, only with your separate approval, use existing relationships and permission-based introductions. "
            "Use UTC until you confirm your timezone. Prepare a private screening checklist and consent script December 7-11, 2026. "
            "Target six 30-minute interviews December 14-18, only if recruiting is separately authorized and participants are available. "
            "Review anonymized notes December 21. Dates are proposed, not booked. "
            "Open: 'You are helping me understand a workflow, not assess individual performance. Participation is voluntary. "
            "Do not share personal names, identifiers, confidential records or credentials. You may skip questions or stop.' "
            "Do not request live screens, documents or recordings. Take anonymized notes only with consent. "
            "Ask: 'What is your role? Walk through your most recent task without personal details. What workarounds do you use?' "
            "Do not pitch or give professional advice. Compare frequency and workarounds across groups. "
            "Treat accounts as self-reported, not independently verified evidence.")
    elif schedule_style == "scheduled-steps":
        plan = ("Keep your first interview test private. Your saved problem is blank; explore difficulties, not assume them. "
            "Do not send emails, publish, build or find contacts. Schedule: Prepare December 7, 2026; "
            "hold five 30-minute interviews December 14-18; review anonymized notes December 21. "
            "Use UTC because your timezone is unconfirmed. Recruiting for later execution: "
            "Use only people you already know or an existing opt-in pool; no cold outreach. "
            "Exclude those with only predictable schedules, anyone you manage, and anyone who could feel pressured. "
            "Screen for varying schedules, not agreement that planning is difficult. "
            "Script: Explain you are exploring habits, not selling or giving professional advice. "
            "Ask permission for anonymous notes; allow skipping questions or stopping. "
            "Ask: 'Tell me about your recent routine.' Only at the end ask: 'What privacy concerns would affect participation?' "
            "Do not request exact personal details, account numbers, credentials, employer details or unredacted confidential records. "
            "For voluntary demonstrations, use fictional amounts or redacted examples. Do not record audio or video. "
            "Keep only minimal anonymized notes privately; delete them after review. "
            "Summarize patterns as participant reports, not independently verified evidence.")
    request = (f"Interview invitations for {audience} about {problem}. "
        f"Test: Understand recent workarounds. Plan: {plan} What to record: Anonymous accounts.")
    footer = ("Your email address is listed publicly on local.example.\n\n" + outreach.OPT_OUT
        + "\n\nReply STOP to stop these emails.\n\nFrom: you@local.example\n\nBusiness outreach · 18 Oak Road, Test City, US")
    _, body = outreach._template("a planning notebook", "Reader", "", request=request,
        word_budget=120 - len(footer.split()))
    journey.validate_draft_copy(body + "\n\n" + footer)
    assert "December 14-18" in body and "UTC" in body and outreach._interview_minutes(body) == "30"
    assert "December 7" not in body and "confirm your timezone" not in body
    assert all(text in body.lower() for text in ("voluntary", "confidential records", "credentials")), body
    if schedule_style != "scheduled-steps":
        assert "firsthand experience" in body, body
    assert re.search(r"\banonym\w*\b", body, re.IGNORECASE), body
    if schedule_style in {"dated-clauses", "planned-calls"}:
        _, zoned = outreach._template("a planning notebook", "Reader", "", request=request.replace(
            "30-minute interviews", "30-minute interviews in Europe/Paris"))
        assert "Europe/Paris" in zoned and "UTC" not in zoned
    if schedule_style == "dated-clauses":
        assert "agreed to participate" in body and "minors" in body
        assert "December 21" not in body and "already express interest" not in body and "contact anyone now" not in body
        _, scripted = outreach._template("a planning notebook", "Reader", "", request=request.replace(
            "interview questions", "interview script"), word_budget=120 - len(footer.split()))
        assert "December 7" not in scripted and "December 14-18" in scripted
        _, restricted = outreach._template("a planning notebook", "Reader", "", request=request.replace(
            "conduct six 30-minute interviews", "conduct only private 30-minute interviews").replace(
            "review anonymized notes.", "review anonymized notes and delete them after your review."))
        assert "only private" in restricted and "delete them after your review" in restricted
    elif schedule_style == "planned-calls":
        assert "December 21" not in body and "authorize it" not in body and "assuming pain or demand" not in body
        assert all(text in body.lower() for text in ("permission", "private", "delete", "professional advice", "minors", "skip questions or stop")), body
        assert re.search(r"\b30 days of (?:the )?summary\b", body, re.IGNORECASE)
        assert re.search(r"\b(?:do not|never) record\b", body, re.IGNORECASE)
        _, notes = outreach._template("a planning notebook", "Reader", "", request=request.replace(
            "Keep de-identified notes private; delete them within 30 days of the summary.",
            "Keep anonymous notes private for 30 days; do not record audio."))
        assert "anonymous notes private for 30 days" in notes and "not record audio" in notes
    elif schedule_style == "targeted-calls":
        assert all(text in body.lower() for text in ("consent", "private", "professional advice", "recordings", "live screens", "proposed", "participants are available")), body
        assert "December 21" not in body and "separate approval" not in body and "screening checklist" not in body
    elif schedule_style == "scheduled-steps":
        assert all(text in body.lower() for text in ("private", "professional advice", "audio", "video", "fictional", "redacted", "minimal", "review")), body
        assert re.search(r"\b(?:skip\w* questions?|stop\w*)\b", body, re.IGNORECASE), body
        assert "December 21" not in body and "problem is blank" not in body and "later execution" not in body


def test_invitation_budget_excludes_preparation_dates_and_later_recruiting(market):
    from app.autopilot import outreach
    from app.gtm import journey

    audience = market[3]["icp"]["who"]
    problem = market[3]["icp"]["problem"]
    plan = ("Keep this proposed first test private. Do not send emails, publish anything, build anything or find contacts. "
        "Your founder timezone is unconfirmed, so use UTC: reserve December 7-11, 2026 for preparation "
        "and December 14-18 for six 30-minute workflow interviews, subject to permission and participant availability. "
        f"For later recruiting, propose using existing, permission-based relationships to invite six {audience}. "
        "Do not recruit minors or substitute unrelated roles. Participation must be voluntary; obtain consent before taking notes. "
        "Use anonymous role labels and group codes. Do not request personal identifiers, records, screenshots, credentials or confidential documents. "
        "Discuss de-identified examples only, and stop or redirect if sensitive information appears. "
        "This is a workflow interview, not professional advice. "
        "Your interview script: 'Please describe your most recent difficulty, without personal or confidential details. "
        "What triggered it? Who handled each step? How often does this happen?' "
        "Do not pitch a solution or ask participants to endorse your idea. "
        "Record reported frequency, workarounds and uncertainty privately. "
        "Treat interview accounts as participant reports, not independently verified evidence.")
    request = (f"Your ask: a 30-minute call. Interview invitations for {audience} about {problem}. "
        f"Test: Learn about recent workarounds. Plan: {plan} What to record: Anonymous accounts.")
    footer = ("Your email address is listed publicly on local.example.\n\n" + outreach.OPT_OUT
        + "\n\nReply STOP to stop these emails.\n\nFrom: you@local.example\n\nBusiness outreach · 18 Oak Road, Test City, US")
    _, body = outreach._template("a planning notebook", "Reader", "", request=request,
        word_budget=120 - len(footer.split()))
    journey.validate_draft_copy(body + "\n\n" + footer)
    assert outreach._interview_minutes(body) == "30"
    assert "December 14-18" in body and "December 7-11" not in body
    assert "UTC" in body and "participant availability" in body
    assert "later recruiting" not in body and "proposed first test" not in body
    assert "minors" in body and "voluntary" in body and "consent" in body
    assert "anonymous" in body and "credentials" in body and "confidential" in body
    assert "de-identified" in body and "sensitive information" in body and "not professional advice" in body.lower()
    _, screened = outreach._template("a planning notebook", "Reader", "", request=request.replace(
        "Use anonymous role labels and group codes.", "Use anonymous role labels and group codes only with permission."),
        word_budget=120 - len(footer.split()))
    assert "Only with permission." in screened and "anonymous" in screened
    journey.validate_draft_copy(screened + "\n\n" + footer)
    _, restricted = outreach._template("a planning notebook", "Reader", "", request=request.replace(
        "Your founder timezone is unconfirmed,", "Your calls must be private and your timezone is unconfirmed,"))
    assert "calls must be private" in restricted.lower()


def test_invitation_budget_excludes_weekly_planning_and_recruiting_approval(market):
    from app.autopilot import outreach
    from app.gtm import journey

    audience, problem = market[3]["icp"]["who"], market[3]["icp"]["problem"]
    plan = ("Keep this as a private plan. Do not send messages, publish anything, build a product or find contacts. "
        "Proposed schedule: December 7 to 11, 2026, using UTC until you confirm your timezone. "
        "On Monday, prepare your questions and consent statement; reserve Tuesday through Thursday for five 20-minute interviews, "
        "only if you later authorize recruiting; review anonymous notes on Friday. "
        "If recruiting is not authorized or five interviews cannot be completed, defer the schedule rather than claim results. "
        "Recruiting approach for later approval: invite volunteers through an existing, permission-based channel. "
        f"Select five adults who work on {problem}. Exclude people with no direct experience, minors, and people seeking urgent professional assistance. "
        "Participation must be voluntary, with no pressure to disclose sensitive information. "
        "Interview script: Start with, 'You may skip any question or stop. Please avoid names, account details and exact personal amounts. "
        "May I take anonymous notes?' Ask: 'Tell me about the last time you experienced this problem. What did you do first?' "
        "Do not pitch a solution, request screenshots, collect credentials, record interviews or give professional advice. "
        "Keep minimal anonymous notes privately and delete them after your review. "
        "Compare concrete recent examples, existing workarounds and privacy concerns; distinguish reported experiences from independently verified evidence.")
    request = (f"Your ask: a 20-minute call. Interview invitations for {audience} about {problem}. "
        f"Test: Learn about recent workarounds. Plan: {plan} What to record: Anonymous accounts.")
    footer = ("Your email address is listed publicly on local.example.\n\n" + outreach.OPT_OUT
        + "\n\nReply STOP to stop these emails.\n\nFrom: you@local.example\n\nBusiness outreach · 18 Oak Road, Test City, US")
    _, body = outreach._template("a planning notebook", "Reader", "", request=request,
        word_budget=120 - len(footer.split()))
    journey.validate_draft_copy(body + "\n\n" + footer)
    assert outreach._interview_minutes(body) == "20" and "Tuesday through Thursday" in body
    assert "prepare your questions" not in body and "authorize recruiting" not in body and "defer the schedule" not in body
    assert "minors" in body and "direct experience" in body and "voluntary" in body
    assert "screenshots" in body and "credentials" in body and "professional advice" in body
    assert "avoid names" in body.lower() and "delete them after your review" in body
    _, quoted = outreach._template("a planning notebook", "Reader", "", request=request.replace("'", "’"),
        word_budget=120 - len(footer.split()))
    assert "screenshots" in quoted and "credentials" in quoted and "professional advice" in quoted
    journey.validate_draft_copy(quoted + "\n\n" + footer)
    _, recording = outreach._template("a planning notebook", "Reader", "", request=request.replace(
        "request screenshots, collect credentials, record interviews or give professional advice", "record interviews or share credentials"),
        word_budget=120 - len(footer.split()))
    assert "record interviews or share credentials" in recording
    _, private = outreach._template("a planning notebook", "Reader", "", request=request.replace(
        "Do not pitch a solution, request screenshots, collect credentials, record interviews or give professional advice.",
        "Do not record interviews."), word_budget=120 - len(footer.split()))
    assert "record interviews" in private


def test_interview_fallback_keeps_the_planned_ask_not_incidental_questions(market, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, recruitment
    from app.gtm import journey
    from app.state import db
    from core.clock import utcnow

    audience, problem = (("print shop production managers", "redoing orders after unclear specifications")
        if market[3]["icp"]["mode"] == "company" else
        ("weekend paddleboarders", "finding sheltered launch spots"))
    venture_id, workspace_id = market[1:3]
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("smtp", {"user": "you@local.example", "sender_name": outreach.founder_signature(venture_id),
            "postal_address": "18 Oak Road, Test City, US"}, {})
    market[3]["icp"].update(who=audience, problem=problem)
    save_market_context(market[1], market[3])
    identifier, = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": f"You can learn how {audience} handle {problem}.",
        "action": f"Hold five 25-minute interviews about {problem}. Ask where they learned their current approach. Keep notes private.",
        "success_measure": "Pass if three describe a recent workaround; fail if none do; otherwise inconclusive.",
    }])
    monkeypatch.setattr(outreach, "saved_pitch", lambda _: "a planning notebook")
    monkeypatch.setattr(outreach.mailboxes, "resolve_send_mailbox", lambda *args, **kwargs: {"email": "you@local.example"})
    with db()() as session:
        session.add(GtmContact(id="planned_invitation_person", workspace_id=workspace_id, email="reader@local.example",
            source=f"venture:{venture_id}:public", recipient_geo="US", email_confidence="risky",
            enrichment_json=json.dumps({"contact_source_url": "https://local.example/team"}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()

    async def no_model(*args, **kwargs):
        return {}

    monkeypatch.setattr(outreach, "_llm_drafts", no_model)
    recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True})
    reply = asyncio.run(recruitment.draft(venture_id, ledger.get(identifier, venture_id)))
    draft, = outreach.drafts(venture_id, "draft")
    assert draft["source"] == "template"
    assert problem in draft["body"] and "Would a short call this week work?" in draft["body"]
    assert outreach._interview_minutes(draft["body"]) == "25"
    assert "learned their current approach" not in draft["body"]
    assert "learned your current approach" not in draft["body"]
    assert "Your email address is listed publicly on local.example." in draft["body"]
    from app.core.local_mode import is_local
    assert outreach.OPT_OUT in draft["body"] and ("Reply STOP to stop these emails." if is_local() else "Stop these emails:") in draft["body"]
    assert outreach.founder_signature(venture_id) in draft["body"] and "From: you@local.example" in draft["body"]
    assert draft["requires_batch_approval"] is True
    with db()() as session:
        assert draft["doc_id"] in ledger._ctx(session.get(Experiment, identifier))["outreach_draft_ids"]
    assert not market[6] and not any(action.get("confirmation") for action in reply["actions"])
    journey.validate_draft_copy(draft["body"], venture_id=venture_id, generated=True)
    question = f"Where do you look for advice about {problem}?"
    _, direct = outreach._template("a planning notebook", "Reader", "", request=f'Draft an email asking "{question}"')
    assert question in direct and "Would a short call" not in direct


@pytest.mark.parametrize("audience", ["brewery packaging supervisors", "recreational ice skaters"])
def test_generated_invitations_pass_the_complete_email_copy_check(interview_plan, monkeypatch, audience):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmOutreachSettings
    from app.gtm import journey
    from app.state import db
    from core.clock import utcnow

    market, _identifier = interview_plan
    monkeypatch.setattr(outreach.mailboxes, "resolve_send_mailbox", lambda *args, **kwargs: {"email": "you@local.example"})
    with db()() as session:
        settings = session.get(GtmOutreachSettings, market[2])
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=market[2], created_at=utcnow(), updated_at=utcnow())
            session.add(settings)
        settings.sender_postal_address = "8 Local Lane, Boston, MA 02110"
        session.add(GtmContact(id="complete_copy_person", workspace_id=market[2], email="reader@local.example",
            source=f"venture:{market[1]}:public", recipient_geo="US", email_confidence="risky",
            enrichment_json=json.dumps({"contact_source_url": "https://local.example/team"}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()

    async def invitations(target, recipients, request):
        return {recipient["to"].lower(): {"subject": "Your recent experience", "body":
            ("You can skip any topic and keep your personal details private. " * 8)
            + f"Could you share your recent experience as one of {audience} in a 25-minute call?"} for recipient in recipients}

    monkeypatch.setattr(outreach, "_llm_drafts", invitations)
    asyncio.run(outreach.draft_batch(market[1], request=f"Interview {audience} for 25 minutes.", review_only=True))
    draft, = outreach.drafts(market[1], "draft")
    journey.validate_draft_copy(draft["body"], venture_id=market[1], generated=True)
    assert len(draft["body"].split()) <= 120 and draft["source"] == "template"
    assert outreach._interview_minutes(draft["body"]) == "25"
    assert "Your email address is listed publicly on local.example." in draft["body"]
    assert outreach.OPT_OUT in draft["body"] and "From:" in draft["body"]
    assert len(outreach.drafts(market[1])) == 1 and not market[6]


@pytest.mark.parametrize("audience", ["brewery packaging supervisors", "recreational ice skaters"])
def test_new_invitation_review_state_keeps_the_checked_revision_stable(interview_plan, monkeypatch, audience):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.state import db
    from core.clock import utcnow

    market, _identifier = interview_plan
    with db()() as session:
        session.add(GtmContact(id="stable_review_person", workspace_id=market[2], email="reader@local.example",
            source=f"venture:{market[1]}:public", recipient_geo="US", email_confidence="risky",
            enrichment_json=json.dumps({"contact_source_url": "https://local.example/team"}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()

    async def invitations(target, recipients, request):
        return {recipient["to"].lower(): {"subject": "Your recent experience", "body":
            f"Could you share your recent experience with {audience} in a 15-minute call?"} for recipient in recipients}

    monkeypatch.setattr(outreach, "_llm_drafts", invitations)
    asyncio.run(outreach.draft_batch(market[1], request=f"Interview invitations for {audience}.", review_only=True))
    draft, = outreach.drafts(market[1], "draft")
    revision = journey._draft_revision(draft)
    assert draft.get("review_state") == "draft" and draft["requires_batch_approval"] is True
    assert journey._review_revision_matches({**draft, "review_state": "approved"}, revision)
    assert not journey._review_revision_matches({**draft, "review_state": "approved", "body": "Different email."}, revision)


@pytest.mark.parametrize("audience", ["regional timber wholesalers", "weekend sailing enthusiasts"])
@pytest.mark.parametrize("outcome,label", [("supported", "passed"), ("refuted", "did not pass")])
def test_recorded_test_list_uses_your_verdict_not_internal_outcomes(interview_plan, audience, outcome, label):
    from app.chat import experiments
    from app.experiments import ledger

    market, _identifier = interview_plan
    identifier, = ledger.create_from_tests(market[1], "chat", [{"hypothesis": f"You can help {audience}."}])
    ledger.record_result(identifier, market[1], True, f"I interviewed five {audience} and recorded their decisions.",
        outcome, "You reported their decisions.", sample_size=5,
        provenance={"source": "Your anonymized interview notes", "method": "Interviews", "observed_at": utcnow().isoformat()})
    reply = experiments.list_reply(market[1])
    assert f"- {label}: You can help {audience}." in reply["body_md"]
    assert f"- {outcome}:" not in reply["body_md"]


@pytest.mark.parametrize("audience", ["regional timber wholesalers", "weekend sailing enthusiasts"])
def test_next_experiment_finds_new_people_when_previous_people_were_contacted(interview_plan, audience):
    from app.db.models import GtmContact
    from app.experiments import recruitment
    from app.state import db
    from core.clock import utcnow

    market, identifier = interview_plan
    with db()() as session:
        session.add(GtmContact(id="previous_recipient", workspace_id=market[2], email="previous@local.example",
            source=f"venture:{market[1]}:public", recipient_geo="US", times_emailed=1,
            dossier_hook=f"You have experience with {audience}.", created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    reply = recruitment.prepare(market[1], {"experiment_id": identifier, "run": True})
    assert reply["actions"][0]["action"] == "gtm.find"
    assert reply["actions"][0]["payload"]["retry"] is True
    assert "find people" in reply["body_md"].lower()


def test_complete_long_plan_survives_saved_plan_and_review_callers(market):
    from app.chat import experiments
    from app.experiments import ledger

    venture_id, view = market[1], market[3]
    audience, problem = view["icp"]["who"], view["icp"]["problem"]
    plan = (f"You prepare interviews with {audience} about {problem}. "
        + "Keep anonymized notes and record the recent incident, workaround, time spent and a counterexample. " * 16
        + "Do not collect private records or contact anyone without permission.")
    assert 1500 < len(plan) < 2000
    payload = {"run": True, "hypothesis": f"You can help {audience} with {problem}", "action": plan,
        "success_measure": "Pass with four observed incidents. Otherwise keep the result inconclusive."}
    reply = experiments.review_test_run(venture_id, payload)
    saved, = ledger.list_for_venture(venture_id)
    assert saved["action"] == plan and plan in reply["body_md"]
    prepared = experiments.review_test_prepare(venture_id, "", {"experiment_id": saved["id"]})
    assert plan in prepared["body_md"]
    rejected = experiments.review_test_run(venture_id, {**payload, "action": plan * 2})
    assert "shorter plan with complete instructions" in rejected["body_md"]
    assert ledger.get(saved["id"], venture_id)["action"] == plan


@pytest.mark.parametrize("audience,problem", [
    ("independent bakery owners", "unfilled delivery shifts"),
    ("weekend hikers", "choosing unfamiliar trails"),
])
def test_interview_questions_keep_their_scope_and_short_question_clauses(market, audience, problem):
    from app.experiments import coach

    hypothesis = f"You can find {audience} who commit time to addressing {problem}"
    script = coach.interview_script(audience, problem, hypothesis)
    context, *questions = script.splitlines()
    assert len(questions) == 4 and all(question.endswith("?") for question in questions)
    assert all(value in context for value in (audience, problem, hypothesis))
    assert script.count(hypothesis) == 1
    assert all(len(question.split()) <= 12 for question in questions)
    action = "Use voluntary referrals only. Ask: Would you buy this? Do not send invitations or use private records."
    grounded = coach.ground_interview_action(action, script)
    assert questions[0] in grounded
    assert "Use voluntary referrals only." in grounded
    assert "Do not send invitations or use private records." in grounded
    scope = f"For {audience}, ask about {problem}, to check {hypothesis}"
    custom = f"{scope}: What happened last time?\n{scope}: How did you respond?"
    custom_script = coach.interview_script(audience, problem, hypothesis, custom)
    assert custom_script.count(hypothesis) == 1
    assert "What happened last time?\nHow did you respond?" in custom_script


@pytest.mark.parametrize("venture,customer", [
    ("Equipment servicing for dental practices", "dental practice managers"),
    ("Local pottery sessions for retired adults", "retired pottery beginners"),
], indirect=["venture"], ids=["b2b-dental", "consumer-pottery"])
@pytest.mark.parametrize("invalid_field", ["hypothesis", "action", "success_measure", "missing", "provider"])
@pytest.mark.parametrize("repaired", [False, True], ids=["saved-idea-plan", "repaired-plan"])
def test_sparse_onboarding_experiment_invalid_plan_has_a_next_step(venture, monkeypatch, invalid_field, repaired, customer):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.db.models import GtmContact, Venture
    from app.experiments import ledger
    from app.idea.validation_agent import ProposedTest
    from app.state import db
    from app.ventures.current_idea import current_revision
    from core.clock import utcnow

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    _, venture_id, workspace_id = venture
    confirm_saved_idea(venture_id)
    with db()() as session:
        revision = current_revision(session.get(Venture, venture_id), session)
        append_idea_revision(session, revision.id, structured=json.dumps({"customer": customer, "problem": None, "offer": "",
            "pricing_hypothesis": None, "_questions_skipped": True}))
        session.add(GtmContact(id="plan_contact", workspace_id=workspace_id, email="reader@studio.example",
            source=f"venture:{venture_id}:public", recipient_geo="US", times_emailed=1,
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    waiting = outreach._save_draft(venture_id, {"contact_id": "plan_contact", "to": "reader@studio.example",
        "channel": "email", "day": 2, "state": "draft", "subject": "Your recent experience",
        "body": "You can share your recent workaround.", "requires_batch_approval": True})
    before = outreach.drafts(venture_id)
    assert waiting and len(before) == 1
    request = f"Design an experiment to learn whether {customer} will purchase this. Do not contact anyone or use private records."
    plan = {"hypothesis": f"You can learn whether {customer} will purchase your offer.",
        "action": "You prepare five interviews this week. Do not contact anyone or use private records.",
        "success_measure": "Pass: three dated purchase commitments. Fail: none after five interviews. Otherwise inconclusive."}
    invalid = {**plan, invalid_field: None if invalid_field == "hypothesis" else "Your proposed step. " * 150}
    if invalid_field == "missing":
        invalid = {}
    calls = []

    async def design(system, user, target, **kwargs):
        calls.append(user)
        assert target == venture_id and customer in user and request in user
        assert not ledger.list_for_venture(venture_id)
        if repaired and len(calls) == 2:
            return plan
        if invalid_field == "provider":
            ProposedTest.model_validate({"hypothesis": None})
        return invalid

    monkeypatch.setattr(experiments, "_llm_json", design)
    reply = asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": request}, None))
    assert len(calls) == 2
    assert "Something went wrong" not in reply["body_md"]
    assert not any(action.get("retry") or action.get("confirmation") for action in reply["actions"])
    assert outreach.drafts(venture_id) == before
    if repaired:
        saved, = ledger.list_for_venture(venture_id)
        assert saved["action"] == plan["action"] and saved["measure"] == plan["success_measure"]
    else:
        saved, = ledger.list_for_venture(venture_id)
        assert saved["state"] == "proposed" and saved["action"] and saved["measure"]
        assert "Prepared your experiment" in reply["body_md"]
        assert not any(action["action"] == "chat.prefill" for action in reply["actions"] if "action" in action)


def test_run_selected_manual_interviews_keeps_the_saved_plan(market, monkeypatch):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.db.models import GtmContact
    from app.experiments import autonomous, ledger, recruitment

    venture_id, view = market[1], market[3]
    plan = {"hypothesis": f"{view['icp']['who']} need help with {view['icp']['problem']}.",
        "action": "Hold five consenting interviews using synthetic examples only. Exclude private records.",
        "success_measure": "Pass with three costly problems and two dated reviews; fail with none; fewer than five interviews are inconclusive."}
    identifier, = ledger.create_from_tests(venture_id, "chat", [plan])
    recruitment.prepare(venture_id, {"run": True, "experiment_id": identifier, **plan})
    monkeypatch.setattr(outreach, "contacts", lambda _: [GtmContact(id="prepared_person", workspace_id=market[2],
        email="reader@local.example", times_emailed=0, do_not_contact=0)])
    monkeypatch.setattr(autonomous, "available", lambda _: [{"id": "catalog-booking", "catalog_key": "E26"}])
    monkeypatch.setattr(autonomous, "prepare", lambda *_: pytest.fail("Your saved manual test must not become a catalog booking test"))
    reply = experiments.review_test_prepare(venture_id, "Run it for me.", {"experiment_id": identifier})
    saved = ledger.get(identifier, venture_id)
    assert saved["action"] == plan["action"] and saved["measure"] == plan["success_measure"]
    assert all(value in reply["body_md"] for value in plan.values())
    assert reply["actions"][0]["action"] == "outreach.draft"
    assert reply["actions"][0]["payload"]["experiment_id"] == identifier
    assert "booking link" not in reply["body_md"] and not any(action.get("confirmation") for action in reply["actions"])
    assert not any(item.get("runner") == "system" for item in ledger.evidence_for_venture(venture_id))
    assert not market[6]


def test_prepared_outreach_summary_has_one_sentence_ending(interview_plan):
    from app.experiments import recruitment

    market, identifier = interview_plan
    view = market[3]
    view["icp"]["who"] += "."
    view["icp"]["problem"] += ".!?"
    save_market_context(market[1], view)
    reply = recruitment.prepare(market[1], {"run": True, "experiment_id": identifier})
    for label, field in (("Your customer", "who"), ("Your problem to test", "problem")):
        assert f"{label}: {view['icp'][field].rstrip('.!? ')}. " in reply["body_md"]
    assert ".." not in reply["body_md"] and ".!?." not in reply["body_md"]
    assert reply["actions"][0]["action"] == "gtm.find"


def test_private_design_passes_the_bounded_contract_to_the_model(market, monkeypatch):
    from app.api.experiments_api import ProposedTest as ApiProposedTest
    from app.chat import assistant, experiments
    from app.experiments import ledger

    venture_id, view = market[1], market[3]
    plan = {"hypothesis": f"{view['icp']['who']} need help with {view['icp']['problem']}.",
        "action": "Interview five consenting adults this week. " + "You record their latest workaround and what it cost. " * 20
            + "Use synthetic examples only; exclude private records.",
        "success_measure": "Pass with three recurring problems; fail with none; fewer than five interviews are inconclusive."}
    calls = []
    assert 600 < len(plan["action"]) <= 2000
    assert ApiProposedTest.model_validate(plan).action == plan["action"]

    async def complete(messages, **kwargs):
        calls.append(kwargs)
        schema = kwargs.get("response_schema")
        assert schema and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(plan)
        assert schema["properties"]["hypothesis"]["maxLength"] == 320
        assert schema["properties"]["action"]["maxLength"] == 2000
        assert schema["properties"]["success_measure"]["maxLength"] == 600
        assert kwargs["venture_id"] == venture_id and kwargs["workspace_id"] == market[2]
        return json.dumps(plan)

    monkeypatch.setattr(assistant, "_complete", complete)
    reply = asyncio.run(experiments.dispatch(venture_id, "experiment_start", "Design an experiment."))
    saved, = ledger.list_for_venture(venture_id)
    assert saved["action"] == plan["action"] and saved["measure"] == plan["success_measure"]
    assert plan["action"] in reply["body_md"] and saved["action"] in saved["how_to_run"]
    assert len(calls) == 1 and "Prepared your experiment" in reply["body_md"] and not market[6]


def test_release_results_require_the_selected_tests_agreed_criteria(market, monkeypatch):
    from app.chat import experiments
    from app.core.local_mode import is_local
    from app.experiments import ledger, recruitment

    venture_id, view = market[1], market[3]
    plan = {"hypothesis": f"{view['icp']['who']} need {view['icp']['problem']}.",
        "action": "Interview ten consenting people. Do not send messages or test product safety.",
        "success_measure": "Pass only after ten interviews, seven costly problems and four dated reviews. Fewer interviews are inconclusive."}
    identifier, = ledger.create_from_tests(venture_id, "chat", [plan])
    selected = ledger.get(identifier, venture_id)
    monkeypatch.setattr(recruitment, "selected", lambda *_: selected)
    message = f"I interviewed six {view['icp']['who']}. Four described costly problems. Two offered reviews. Nobody paid."
    evidence = {"criteria_met": False, "criteria_failed": False}

    async def extract(system, user, venture):
        assert plan["action"] in user and plan["success_measure"] in user
        assert "every" in system.lower() and "inconclusive" in system
        return {"ran": True, "outcome": "supported", "learning": "Some people described a costly problem.", **evidence}

    monkeypatch.setattr(experiments, "_llm_json", extract)
    for flags, expected in [({}, "inconclusive"), ({"criteria_met": False, "criteria_failed": False}, "inconclusive"),
            ({"criteria_met": True, "criteria_failed": True}, "inconclusive"),
            ({"criteria_met": True, "criteria_failed": False}, "supported")]:
        evidence.clear()
        evidence.update(flags)
        report = (f"I interviewed ten {view['icp']['who']}. Seven described costly problems and four agreed dated reviews."
                  if expected == "supported" else message)
        result = asyncio.run(experiments.extract(venture_id, report, selected["hypothesis"],
            plan["action"], plan["success_measure"]))
        assert result["outcome"] == expected
    evidence.update(criteria_met=False, criteria_failed=False)
    result = asyncio.run(experiments.extract(venture_id, message, selected["hypothesis"],
        plan["action"], plan["success_measure"]))
    asyncio.run(experiments.record_run(venture_id, {**result, "experiment_id": identifier, "result_text": message}))
    saved = ledger.get(identifier, venture_id)
    assert saved["result_text"] == message
    if is_local():
        assert saved["state"] == "running" and saved["outcome"] is None and saved["needs_evidence"]
    else:
        assert saved["outcome"] == "inconclusive"


def test_release_design_repairs_overlong_plans_without_cutting_safety(market, monkeypatch):
    from app.chat import experiments
    from app.experiments import ledger

    venture_id, view = market[1], market[3]
    plan = {"hypothesis": f"{view['icp']['who']} need help with {view['icp']['problem']}.",
        "action": "You interview ten consenting adults this week. Do not send, publish or test product safety.",
        "success_measure": "Pass: ten interviews, seven costly problems and four dated reviews. Fail: ten interviews and no reviews. Otherwise inconclusive."}
    invalid = {**plan, "action": "You record their recent workaround. " * 100 + plan["action"]}
    calls = []

    async def design(system, user, venture, **kwargs):
        calls.append((system, user))
        if len(calls) == 1:
            return invalid
        assert invalid["action"] in user and "safety" in system.lower()
        assert '"loc": ["action"]' in user and '"maxLength": 2000' in user
        assert not ledger.list_for_venture(venture_id)
        return plan

    monkeypatch.setattr(experiments, "_llm_json", design)
    reply = asyncio.run(experiments.dispatch(venture_id, "experiment_start", "Design an experiment."))
    assert "Prepared your experiment" in reply["body_md"] and len(calls) == 2
    saved, = ledger.list_for_venture(venture_id)
    assert saved["action"] == plan["action"] and saved["measure"] == plan["success_measure"]
    assert not any(action.get("confirmation") for action in reply["actions"]) and not market[6]


def test_release_typed_run_resolves_the_selected_prepared_test(interview_plan, monkeypatch):
    from app.chat import experiments, router
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent
    from app.db.models import Workspace
    from app.experiments import ledger, recruitment
    from app.state import db
    from app.tenancy import bind

    market, identifier = interview_plan
    venture_id = market[1]
    recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True})
    other, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "A different unanswered question", "action": "Interview other people."}])
    assert other != identifier
    requested = []

    def prepare(venture, text, payload=None):
        requested.append(payload["experiment_id"])
        return {"kind": "text", "body_md": "You can review your saved test. Nothing was sent.", "actions": [], "citations": []}

    monkeypatch.setattr(experiments, "review_test_prepare", prepare)
    with db()() as session:
        actor = session.get(Workspace, market[2]).owner_id
    text = "Run it for me."
    assert classify_intent(text) == "experiment_start"
    with bind(workspace_id=market[2], venture_id=venture_id, actor=actor):
        reply = asyncio.run(router._dispatch_intent(venture_id,
            ChatContext(venture={"id": venture_id, "workspace_id": market[2]}), "experiment_start", text, None))
    assert requested == [identifier] and reply["kind"] != "confirmation" and not market[6]
    requested.clear()
    monkeypatch.setattr(recruitment, "selected", lambda *_: None)
    with bind(workspace_id=market[2], venture_id=venture_id, actor=actor):
        asyncio.run(router._dispatch_intent(venture_id,
            ChatContext(venture={"id": venture_id, "workspace_id": market[2]}), "experiment_start", text, None))
    assert not requested and not market[6]


@pytest.mark.parametrize("venture", ["Inventory reconciliation for warehouses", "Weekend walking groups for families"], indirect=True)
def test_exp4_experiment_json_keeps_chat_model_admission(venture, monkeypatch):
    from app.chat import assistant, experiments
    from app.chat.store import CLIENT_REQUEST_ID

    _, venture_id, workspace_id = venture
    calls = []
    async def complete(messages, **kwargs):
        calls.append(kwargs)
        assert kwargs.get("venture_id") == venture_id
        assert kwargs.get("workspace_id") == workspace_id
        assert kwargs.get("request_id", "").startswith("founder-experiment-request:")
        return '{"hypothesis": "You will learn from this test"}'
    monkeypatch.setattr(assistant, "_complete", complete)
    token = CLIENT_REQUEST_ID.set("founder-experiment-request")
    try:
        assert asyncio.run(experiments._llm_json("Prepare a private test", "Your saved audience", venture_id))["hypothesis"]
        asyncio.run(experiments._llm_json("Extract your reported result", "Your saved audience", venture_id))
    finally:
        CLIENT_REQUEST_ID.reset(token)
    assert len(calls) == 2 and calls[0]["request_id"] != calls[1]["request_id"]


@pytest.mark.parametrize("venture", ["Inventory reconciliation for warehouses", "Weekend walking groups for families"], indirect=True)
def test_exp4_page_object_is_not_an_experiment_plan(venture, monkeypatch):
    from app.core.local_mode import is_local
    from app.chat import experiments
    from app.chat.intent import _EXP_PLAN_RE, classify_intent
    from app.experiments import ledger
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    client, venture_id, _ = venture
    confirm_saved_idea(venture_id)
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    if is_local():
        from app.core.local_capabilities import blocked_message
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"landing_pages": False}, {})
    ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "You can recruit interview participants",
        "action": "Interview five participants", "success_measure": "Three agree a dated interview"}])
    async def no_plan(*args):
        pytest.fail("A page request must not ask the experiment planner")
    monkeypatch.setattr(experiments, "_llm_json", no_plan)
    for message in ("Prepare a landing page for this experiment.", "Please draft the landing page for our test."):
        assert not _EXP_PLAN_RE.search(message)
        assert classify_intent(message) == ("pagebuilder.build" if is_local() else "start_product_build")
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
        assert response.status_code == 200
        if is_local():
            assert blocked_message("landing_pages") in response.json()["body_md"]
        else:
            assert experiments.page_unavailable_reply()["body_md"] in response.json()["body_md"]
            assert response.json()["kind"] == "needs_you"
            assert response.json()["actions"] == experiments.page_unavailable_reply()["actions"]
        assert "experiment plan" not in response.json()["body_md"].lower()
        assert "booking experiment" not in response.json()["body_md"].lower()
    assert _EXP_PLAN_RE.search("Prepare a landing page test.")
    assert classify_intent("Design an experiment.") == "experiment_start"


@pytest.mark.parametrize("venture", ["Commercial greenhouse maintenance", "Family pottery lessons"], indirect=True)
def test_drafted_page_requests_keep_the_build_route(venture, monkeypatch):
    from app.core.local_mode import is_local
    from app.chat import experiments

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    client, venture_id, _ = venture
    confirm_saved_idea(venture_id)
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ENABLED", "0")
    if is_local():
        from app.core.local_capabilities import blocked_message
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"landing_pages": False}, {})
    response = client.post(f"/ventures/{venture_id}/chat", json={
        "message": "Please draft the landing page for our test."})
    assert response.status_code == 200
    if is_local():
        assert blocked_message("landing_pages") in response.json()["body_md"]
        update_settings("capabilities", {"landing_pages": True}, {})
        monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [])
        disconnected = client.post(f"/ventures/{venture_id}/chat", json={
            "message": "Please draft the landing page for our test."})
        assert disconnected.status_code == 200
        assert any(action.get("href") == "/settings/local#coding-tools" for action in disconnected.json()["actions"])
    else:
        assert response.json()["actions"] == experiments.page_unavailable_reply()["actions"]
        assert response.json()["kind"] == "needs_you"


@pytest.mark.parametrize("venture", ["Commercial greenhouse maintenance", "Family pottery lessons"], indirect=True)
def test_finished_result_read_does_not_require_a_new_idea_review(venture, monkeypatch):
    from app.chat import experiments

    _, venture_id, _ = venture
    requested = []

    def idea_gate(identifier):
        pytest.fail("Reading your saved result must not require a new idea review")

    def result_reply(identifier, experiment_id):
        requested.append((identifier, experiment_id))
        return {"kind": "experiment_result", "body_md": "Your saved result", "actions": []}

    monkeypatch.setattr(experiments, "_idea_gate", idea_gate)
    monkeypatch.setattr(experiments, "result_reply", result_reply)
    reply = asyncio.run(experiments.execute(venture_id, "experiment.list", {"experiment_id": "saved-result"}, None))
    assert reply["kind"] == "experiment_result"
    assert requested == [(venture_id, "saved-result")]


@pytest.mark.parametrize("venture,audience,problem", [
    ("A portable satellite ground-station kit for amateur satellite watchers",
     "amateur satellite watchers", "Amateur satellite watchers struggle to receive weather-satellite images at home."),
    ("Operating-room scheduling software for hospital theatre managers",
     "hospital theatre managers", "Hospital theatre managers struggle to coordinate operating-room changes."),
], indirect=["venture"], ids=["consumer-space-hardware", "b2b-hospital-software"])
def test_requested_experiment_uses_confirmed_idea_with_unfinished_saved_answers(venture, monkeypatch, audience, problem):
    from app.chat import experiments
    from app.credits.policy import set_policy
    from app.db.models import Job, Venture, VentureDocument
    from app.db.supymem_models import IdeaSession
    from app.experiments import ledger
    from app.state import db
    from app.ventures import startup_review
    from app.ventures.current_idea import current_revision, intake_pending
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    client, venture_id, _workspace_id = venture
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    confirm_saved_idea(venture_id)
    with db()() as session:
        saved = session.get(Venture, venture_id)
        revision = current_revision(saved, session)
        revision = append_idea_revision(session, revision.id,
            structured=json.dumps({"offer": saved.raw_idea, "customer": audience, "problem": problem}))
        idea = session.get(IdeaSession, revision.session_id)
        idea_id, revision_id = idea.id, revision.id
        idea.confirmed_revision_id = None
        startup_review._document(session, venture_id, "internal_new_user_flow", {"enabled": True})
        session.commit()
    assert intake_pending(venture_id)
    blocked = experiments._idea_gate(venture_id)
    from app.core.local_mode import is_local
    if is_local():
        assert blocked is None
    else:
        assert "confirm your idea" in blocked["body_md"].lower() and blocked["actions"]
    assert not ledger.list_for_venture(venture_id)
    with db()() as session:
        session.get(IdeaSession, idea_id).confirmed_revision_id = revision_id
        session.commit()
        jobs_before = set(session.scalars(select(Job.id).where(Job.venture_id == venture_id)))
    assert intake_pending(venture_id)
    hypothesis = f"{audience.capitalize()} would pay for this."
    plan = {"hypothesis": hypothesis,
            "action": "Interview five consenting adults about recent spending and ask for a concrete purchase commitment.",
            "success_measure": "Pass with three purchase commitments; fail with none; missing interviews are inconclusive."}
    prompts = []

    async def design(_system, user, scoped_venture_id, **kwargs):
        assert scoped_venture_id == venture_id
        prompts.append(user)
        return plan

    monkeypatch.setattr(experiments, "_llm_json", design)
    response = client.post(f"/ventures/{venture_id}/chat", json={
        "message": f"Design an experiment to test whether {hypothesis}"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert "Prepared your experiment" in reply["body_md"]
    assert len(prompts) == 1 and audience in prompts[0] and problem in prompts[0]
    assert audience in reply["body_md"] and problem in reply["body_md"]
    next_step = reply["body_md"].split("Next step:", 1)[1].split("\n\n", 1)[0]
    assert next_step.count("?") <= 1
    assert "Your outreach step" in reply["body_md"] and "Nothing is sent" in reply["body_md"]
    assert "\u2014" not in reply["body_md"] and "credit" not in reply["body_md"].lower()
    assert not any(action.get("confirmation") for action in reply["actions"])
    rows = ledger.list_for_venture(venture_id)
    assert len(rows) == 1 and rows[0]["hypothesis"] == hypothesis and rows[0]["state"] == "proposed"
    with db()() as session:
        assert set(session.scalars(select(Job.id).where(Job.venture_id == venture_id))) == jobs_before
        if is_local():
            revision = current_revision(session.get(Venture, venture_id), session)
            fields = json.loads(revision.structured)
            assert revision.id == revision_id and fields["customer"] == audience and fields["problem"] == problem
        else:
            checklist = json.loads(session.get(VentureDocument, f"{startup_review.INTERNAL_KIND}:{venture_id}").body_md)["checklist"]
            assert checklist["audience"]["value"] == audience and checklist["problem"]["value"] == problem
    assert intake_pending(venture_id)


@pytest.fixture
def interview_plan(market):
    from app.credits import policy
    from app.db.models import Venture
    from app.experiments import ledger
    from app.state import db
    from app.ventures.current_idea import current_revision

    venture_id, view = market[1], market[3]
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    from app.core.local_mode import is_local
    if not is_local():
        assert policy.wallet_phase() == "person"
    else:
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe
        update_settings("smtp", {"host": "smtp.example", "user": "you@local.example",
            "sender_name": "Your founder", "postal_address": "42 Cedar Street, Springfield, US"},
            {"password": token_urlsafe(24)})
    with db()() as session:
        revision = current_revision(session.get(Venture, venture_id), session)
        append_idea_revision(session, revision.id,
            structured=json.dumps({"customer": view["icp"]["who"], "problem": view["icp"]["problem"]}))
        session.commit()
    identifier = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": "Customers spend time on a recurring problem.",
        "action": "Interview five consenting adults about their recent experience and workarounds.",
        "success_measure": "Pass if three describe a costly workaround; fewer is a fail; missing interviews are inconclusive."
    }])[0]
    return market, identifier


def test_prepared_interview_script_names_the_saved_customer_and_problem(interview_plan):
    from app.chat import experiments
    from app.credits import policy
    from app.db.models import Venture
    from app.experiments import ledger
    from app.state import db
    from app.ventures import founder_facts
    from app.ventures.current_idea import current_revision

    market, identifier = interview_plan
    venture_id, view = market[1], market[3]
    audience, problem = view["icp"]["who"], view["icp"]["problem"]
    view["icp"].update(who="Another outreach audience", problem="Another outreach problem")
    for enabled in (True, False):
        policy.set_policy("feature_flags", {"experiments_autopilot_v1": enabled}, "test")
        reply = experiments.review_test_run(venture_id, {"run": True, "experiment_id": identifier})
        script = reply["body_md"].split("Interview script:", 1)[1].split("\n\n", 1)[0]
        assert audience in script and problem in script
        assert "Another outreach" not in script and "this problem" not in script
        assert "\u2014" not in reply["body_md"]
        assert ledger.get(identifier, venture_id)["state"] == "proposed" and not market[6]
        assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        venture = session.get(Venture, venture_id)
        founder_facts.save(session, venture, {"problem": None})
        append_idea_revision(session, current_revision(venture, session).id, structured="{}")
        session.commit()
    missing = experiments.review_test_run(venture_id, {"run": True, "experiment_id": identifier})
    assert "I need the problem you want to test to write specific interview questions." in missing["body_md"]
    assert missing["body_md"].endswith("Next step: tell me the problem you want to test.")
    assert "tell me your customer" not in missing["body_md"]
    assert ledger.get(identifier, venture_id)["state"] == "proposed" and not market[6]


def test_prepared_interview_reply_keeps_one_next_step_in_saved_chat(interview_plan, monkeypatch):
    from app.chat import experiments, next_moves
    from app.chat.store import append_message, list_messages
    from tests.unit.chat.test_next_moves import _saved_state

    market, identifier = interview_plan
    venture_id, view = market[1], market[3]
    state = _saved_state(view["icp"]["who"], later=True)
    state["idea"]["confirmed"] = True
    monkeypatch.setattr(next_moves, "_snapshot", lambda *_args: deepcopy(state))
    monkeypatch.setattr(next_moves, "_blocked_actions", lambda *_args: set())
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda *_args: {
        "id": identifier, "title": "Test next", "why": "Use your saved test.",
        "action": "experiment.recommendation.prepare", "payload": {"experiment_id": identifier}})
    append_message(venture_id, role="user", body_md="Prepare my interview plan.")
    reply = experiments.review_test_run(venture_id, {"run": True, "experiment_id": identifier})
    saved = append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                           actions=reply["actions"], citations=reply["citations"])
    for message in (saved, list_messages(venture_id)[-1]):
        assert not message.get("next_moves")
        assert "Other steps you can ask me to take:" not in message["body_md"]
        assert "Choose Test next" not in message["body_md"]
        assert message["body_md"].endswith("Next step: who will you interview first?")
    assert len(saved["actions"]) <= 1 and not market[6]


def test_unavailable_experiment_result_gives_you_a_next_step_without_starting_work(market):
    client, venture_id = market[:2]
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        "action": "experiment.list", "payload": {"experiment_id": "unavailable_" + venture_id}})
    assert response.status_code == 200
    reply = response.json()
    assert "Review your experiments" in reply["body_md"]
    assert [{key: action[key] for key in ("label", "action", "payload", "style")}
        for action in reply["actions"]] == [{"label": "Review your experiments", "action": "experiment.list",
        "payload": {}, "style": "primary"}]
    assert reply["actions"][0]["reply_choice"] is True and reply["actions"][0]["state_hash"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert "\u2014" not in reply["body_md"] and not market[6]


def test_requested_market_plan_is_saved_prepared_and_reviewed_without_reconfirmation(market, monkeypatch):
    from app.chat import experiments
    from app.chat.intent import _experiment_intent
    from app.credits import policy
    from app.experiments import autonomous, ledger, wizard_tests

    venture_id, view = market[1], market[3]
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    audience, problem = view["icp"]["who"], view["icp"]["problem"]
    request = (f"Design a nine-day market test for {audience} about {problem}, using twelve interviews. "
               "Give me the schedule, interview script, recruiting approach and pass or fail threshold. "
               "Keep it as a plan. Nothing should be sent or built.")
    assert _experiment_intent(request) == "experiment_start"
    assert _experiment_intent("Review your tests") == "experiment_list"
    plan = {"hypothesis": f"{audience} need help with {problem}.",
            "action": "Days 1 to 2: seek consenting adults through peer introductions. Days 3 to 8: hold twelve interviews. "
                      "Ask about their latest problem, workaround, cost and next step. Day 9: review the evidence.",
            "success_measure": "Pass if seven of twelve report recent costly workarounds and three agree to a trial. Otherwise fail; missing interviews are inconclusive."}
    calls = []

    async def complete(system, user, venture, **kwargs):
        calls.append((system, user, venture))
        assert request in user and audience in user and problem in user
        return deepcopy(plan)

    monkeypatch.setattr(experiments, "_llm_json", complete)
    monkeypatch.setattr(experiments, "_idea_gate", lambda *_args: None)
    monkeypatch.setattr(wizard_tests, "chat", lambda *_args: None)
    monkeypatch.setattr(autonomous, "available", lambda *_args: [])
    ledger.create_from_tests(venture_id, "validation", [{**plan, "action": "Decide the schedule before starting."}])
    prepared = asyncio.run(experiments.dispatch(venture_id, "experiment_start", request))
    assert len(calls) == 1
    assert prepared["kind"] != "confirmation" and not any(action.get("confirmation") for action in prepared["actions"])
    saved = ledger.list_for_venture(venture_id)
    assert len(saved) == 1
    assert saved[0]["hypothesis"] == plan["hypothesis"]
    assert saved[0]["action"] == plan["action"] and saved[0]["measure"] == plan["success_measure"]
    assert saved[0]["state"] == "proposed" and not market[6]
    listed = experiments.list_reply(venture_id)
    for reply in (prepared, listed):
        assert all(value in reply["body_md"] for value in plan.values())
        assert "\u2014" not in reply["body_md"]
    assert not any(action.get("confirmation") for action in listed["actions"])
    assert not any("Decide how many" in step for step in saved[0]["how_to_run"])
    original = deepcopy(saved)
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": False}, "test")
    without_autopilot = asyncio.run(experiments.dispatch(venture_id, "experiment_start", request))
    assert all(value in without_autopilot["body_md"] for value in plan.values())
    assert ledger.list_for_venture(venture_id) == original and not market[6]
    plan["action"] = ""
    rejected = asyncio.run(experiments.dispatch(venture_id, "experiment_start", request))
    assert "Prepared your experiment" in rejected["body_md"] and rejected["actions"]
    after = ledger.list_for_venture(venture_id)
    assert len(after) == len(original) + 1
    assert next(item for item in after if item["id"] == original[0]["id"]) == original[0]
    assert not market[6]
    with monkeypatch.context() as denied:
        denied.setattr("app.authz.roles.role_allows", lambda *_args: False)
        with pytest.raises(PermissionError, match="request_work_required"):
            asyncio.run(experiments.dispatch(venture_id, "experiment_start", request))
    assert len(calls) == 4 and ledger.list_for_venture(venture_id) == after and not market[6]


def test_paid_scan_keeps_its_actual_ceiling_without_starting_unrequested_work(market):
    from app.core.local_mode import is_local
    from app.experiments import autonomous

    market[5].search_micro = market[5].scrape_micro = 1
    spec = next(item for item in autonomous.available(market[1]) if item["ceiling_micro"] > 0)
    action = autonomous.prepare(market[1], spec["id"])["actions"][0]
    if is_local():
        assert not action.get("confirmation")
        assert action["payload"]["ceiling_micro"] == spec["ceiling_micro"]
    else:
        card = action["confirmation"]
        assert not any("Maximum cost:" in consequence for consequence in card["consequences"])
        assert card["resulting_state"]["ceiling_micro"] == spec["ceiling_micro"]
    assert action["style"] == "primary" and not market[6]


def test_requested_paid_scan_starts_once_without_another_confirmation(market, monkeypatch):
    from app.chat.execute import execute_action
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.state import db

    venture_id = market[1]
    spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    assert not market[6]
    with monkeypatch.context() as listing:
        listing.setattr(autonomous, "available", lambda *args: [deepcopy(spec)])
        asyncio.run(execute_action(venture_id, "experiment.start"))
    with db()() as session:
        assert not list(session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == autonomous.KIND)))
    reply = asyncio.run(execute_action(venture_id, "experiment.prepare", payload={"experiment_id": spec["id"]}))
    assert reply["kind"] == "experiment_running"
    assert not any(action.get("confirmation") for action in reply["actions"])
    asyncio.run(execute_action(venture_id, "experiment.prepare", payload={"experiment_id": spec["id"]}))
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == autonomous.KIND)))
        saved = json.loads(session.get(Experiment, spec["id"]).context)
    assert len(jobs) == 1
    assert saved["spec"]["ceiling_micro"] == spec["ceiling_micro"]
    assert not market[6]


def test_run_offer_checks_availability_and_the_same_launch_guards(market, monkeypatch):
    from app.chat import next_moves
    from app.credits import policy
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db
    from core.clock import utcnow
    from tests.unit.chat.test_next_moves import _saved_state

    venture_id, workspace_id = market[1:3]
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "trend")
    with db()() as session:
        for row in session.scalars(select(Experiment).where(Experiment.venture_id == venture_id, Experiment.id != spec["id"])):
            session.delete(row)
        session.commit()
    state = _saved_state(market[3]["icp"]["problem"])
    state["experiment"] = {"id": spec["id"], "state": "proposed"}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    assert autonomous.run_blocked_reason(venture_id, spec["id"]) == ""
    with db()() as session:
        assert [row.id for row in session.scalars(select(Experiment).where(Experiment.venture_id == venture_id))] == [spec["id"]]
    for guard in ("concurrency", "duplicate", "cooldown"):
        history = []
        with db()() as session:
            for index in range(3 if guard == "concurrency" else 1):
                identifier = f"guard_{guard}_{index}"
                previous = {**deepcopy(spec), "id": identifier}
                if guard == "concurrency":
                    previous["variable"] = "other"
                row = Experiment(id=identifier, venture_id=venture_id, workspace_id=workspace_id,
                    kind=ledger.KIND, hypothesis=spec["hypothesis"],
                    state="concluded" if guard == "cooldown" else "proposed",
                    ended_at=utcnow() - timedelta(days=1) if guard == "cooldown" else None,
                    context=json.dumps({"runner": "system", "spec": previous, "claimed": guard != "cooldown"}))
                session.add(row)
                history.append(identifier)
            session.commit()
        reason = autonomous.run_blocked_reason(venture_id, spec["id"])
        assert reason and "—" not in reason
        assert not any(action["label"] == "Run it for me" for action in next_moves.compose(venture_id)["actions"])
        prepared = autonomous.prepare(venture_id, spec["id"])
        assert prepared["kind"] == "experiment_blocked" and prepared["actions"]
        assert not any(action.get("confirmation") for action in prepared["actions"])
        payload = {"experiment_id": spec["id"], "fingerprint": spec["fingerprint"], "ceiling_micro": spec["ceiling_micro"]}
        assert asyncio.run(autonomous.run(venture_id, payload))["kind"] == "experiment_blocked"
        with db()() as session:
            assert session.get(Experiment, spec["id"]).state == "proposed"
            assert not list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))
            for identifier in history:
                session.delete(session.get(Experiment, identifier))
            session.commit()
    assert autonomous.run_blocked_reason("another_venture", spec["id"])
    monkeypatch.setattr(autonomous, "select_runnable", lambda *args: [])
    assert autonomous.run_blocked_reason(venture_id, spec["id"])
    assert autonomous.run_blocked_reason(venture_id, "missing_test")
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        row.context = json.dumps({**context, "belief_plan_id": "saved_plan"})
        session.commit()
    assert autonomous.run_blocked_reason(venture_id, spec["id"])
    assert not any(action["label"] == "Run it for me" for action in next_moves.compose(venture_id)["actions"])
    manual, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": market[3]["icp"]["problem"]}])
    assert autonomous.run_blocked_reason(venture_id, manual)
    assert not any(action["label"] == "Run it for me" for action in next_moves.compose(venture_id)["actions"])


def test_test_next_prepares_the_queued_test_not_the_plan_overview(market):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import experiments, next_moves
    from app.credits import policy
    from app.experiments import autonomous, beliefs
    from app.state import db

    venture_id = market[1]
    set_local_experiments(True)
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "demand")
    with db()() as session:
        saved = beliefs.row(session, venture_id, create=True)
        context = json.loads(saved.context)
        context["plan"] = {"state": "active", "tests": [{"state": "queued", "experiment_id": spec["id"]}]}
        saved.context = json.dumps(context)
        session.commit()
    recommended = next_moves.experiment_learning(venture_id)["recommendation"]
    assert recommended["action"] == "experiment.recommendation.prepare"
    assert recommended["payload"] == {"experiment_id": spec["id"]}
    assert ACTION_SLUG[recommended["action"]] == "experiments"
    reply = asyncio.run(experiments.execute(venture_id, recommended["action"], recommended["payload"], None))
    from app.core.local_mode import is_local
    offer = reply["actions"][0]
    assert not offer.get("confirmation") if is_local() else reply["kind"] == "confirmation"
    assert (offer["payload"] if is_local() else offer["confirmation"]["resulting_state"])["experiment_id"] == spec["id"]


def test_consider_a_pivot_opens_a_review_without_changing_your_direction(market):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import experiments, next_moves
    from app.chat.actions import get_action
    from app.credits import policy
    from app.experiments import ledger
    from app.state import db

    venture_id, icp = market[1], market[3]["icp"]
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": icp["problem"]}])
    ledger.record_result(identifier, venture_id, True, "No responses.", "refuted", "No interest was reported.", sample_size=5,
        provenance={"source": "Your outreach log", "method": "Five invitations", "observed_at": utcnow().isoformat()})
    with db()() as session:
        direction = ledger.current_direction(session, venture_id)
    recommended = next_moves.experiment_learning(venture_id)["recommendation"]
    assert recommended["title"] == "Consider a pivot" and recommended["action"] == "experiment.review"
    assert ACTION_SLUG[recommended["action"]] == "experiments"
    assert get_action(recommended["action"]).required_permission == "read"
    reply = asyncio.run(experiments.execute(venture_id, recommended["action"], recommended["payload"], None))
    from app.core.local_mode import is_local
    if is_local():
        assert not any(action.get("confirmation") for action in reply["actions"])
        assert all(action.get("payload", {}).get("direction") for action in reply["actions"]
            if action.get("action") in {"experiment.pivot", "experiment.kill"})
    else:
        assert any(action.get("confirmation") for action in reply["actions"])
        assert all(action.get("confirmation") for action in reply["actions"]
                   if action.get("action") in {"experiment.pivot", "experiment.kill"})
    with db()() as session:
        assert ledger.current_direction(session, venture_id) == direction


def test_next_move_runs_system_ids_and_recovers_missing_tests_without_a_loop(market, monkeypatch):
    from app.chat import experiments, next_moves
    from app.chat.execute import execute_action
    from app.credits import policy
    from app.db.models import Job
    from app.experiments import autonomous
    from app.state import db
    from tests.unit.chat.test_next_moves import _saved_state

    _client, venture_id, _workspace_id, view, _landing, _provider, calls, _hits, _app_id = market
    set_local_experiments(True)
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True, "goals_v1": True}, "test")
    state = _saved_state(view["icp"]["problem"])
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    assert not experiments.ledger.evidence_for_venture(venture_id)
    design = next(action for action in next_moves.compose(venture_id)["actions"] if action["label"] == "Design an experiment")
    designed = asyncio.run(execute_action(venture_id, design["action"], payload=design["payload"]))
    assert "Prepared your experiment" in designed["body_md"] and experiments.ledger.evidence_for_venture(venture_id)
    assert not any(action.get("confirmation") for action in designed["actions"])
    spec = autonomous.available(venture_id)[0]
    state["experiment"] = {"id": spec["id"], "state": "proposed"}
    run = next(action for action in next_moves.compose(venture_id)["actions"]
        if action["action"] == "experiment.recommendation.prepare" and action["payload"] == {"experiment_id": spec["id"]})
    assert run["label"] != "Run it for me"
    assert run["payload"] == {"experiment_id": spec["id"]}
    prepared = experiments.review_test_prepare(venture_id, "", run["payload"])
    from app.core.local_mode import is_local
    offer = prepared["actions"][0]
    assert (offer["payload"] if is_local() else offer["confirmation"]["resulting_state"])["experiment_id"] == spec["id"]
    started = asyncio.run(execute_action(venture_id, run["action"], payload=run["payload"]))
    assert started["kind"] == "experiment_running", started
    assert not any(action.get("confirmation") for action in started["actions"]) and not calls
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))) == 1
    stale = experiments.review_test_prepare(venture_id, "", {"experiment_id": "missing-test"})
    assert not any(action["label"] == "Run it for me" for action in stale["actions"])
    design = next(action for action in stale["actions"] if action["label"] == "Design an experiment")
    recovered = asyncio.run(execute_action(venture_id, design["action"], payload=design.get("payload")))
    assert "Prepared your experiment" in recovered["body_md"] and recovered["actions"]
    state["experiment"] = None
    fresh = next(action for action in next_moves.compose(venture_id)["actions"] if action["label"] == "Design an experiment")
    assert fresh["action"] == "experiment.start"
    pricing = next(item for item in autonomous.available(venture_id) if item["scan"] == "pricing")
    result = autonomous.result_reply({**pricing, "result": {"insight": "Your public price anchors.", "sources": [
        {"url": "https://alternative.example/pricing", "title": view["icp"]["problem"], "price_points": [
            {"price": "$12", "billing_period": "month"}, {"price": "$24", "billing_period": "month"}]}]}})
    assert result["actions"][0]["label"] == "Design an experiment"
    assert not calls


def test_recommended_experiments_route_to_confirmed_runners_or_prepared_materials(market, credit_meter, monkeypatch):
    from app.core import local_owner
    from app.core.local_mode import is_local
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.approvals import external
    from app.chat import experiments
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger, outreach_tests, page_tests
    from app.state import db

    client, venture_id, workspace_id, view, landing, _provider, calls, *_rest = market
    if is_local():
        monkeypatch.setattr(local_owner, "current_owner", local_owner.ensure_owner)
        monkeypatch.setattr(page_tests, "_connected_provider", lambda _actor: None)
    audience, problem = view["icp"]["who"], view["icp"]["problem"]
    view["playbooks"] = [{"key": "problem_interview"}]
    landing.update(enabled=True, published_site={"url": "https://landing.example/"})
    monkeypatch.setattr(external, "outbound_quote", lambda *args: {"credit_estimate_micro": 1000})
    outreach_tests.configure_booking(venture_id, {"booking_link": "https://appointments.example/interviews"})
    recipes = [("Scan public discussions", "experiment.run", "demand"),
               ("Scan competitor gaps", "experiment.run", "competitors"),
               ("Recruit interviews", "experiment.run", "outreach_split"),
               ("Compare message openings", "experiment.run", "outreach_split"),
               ("Compare send-time windows", "experiment.run", "outreach_split"),
               ("Compare landing page headlines", "experiment.page.configure", "headlines"),
               ("Test pricing", "experiment.run", "pricing"),
               ("Run a disappointment survey", "experiment.page.configure", "survey"),
               ("Run a referral ask", "experiment.channel.configure", "referral"),
               ("Compare channels", "experiment.channel.configure", "channels"),
               ("Prepare LinkedIn connection notes", "experiment.run_review", None)]
    ids = ledger.create_from_tests(venture_id, "validation", [
        {"hypothesis": f"{audience} need help with {problem}: {method.lower()}.", "action": method,
         "success_measure": "Compare observed responses."} for method, *_rest in recipes])
    listed = experiments.list_reply(venture_id)
    assert "Choose the run button for" in listed["body_md"]
    assert "run yourself" not in listed["body_md"] and "—" not in listed["body_md"]
    assert {item["payload"]["experiment_id"] for item in listed["actions"]
            if item.get("action") == "experiment.recommendation.prepare"} == set(ids)
    assert ACTION_SLUG["experiment.recommendation.prepare"] == "experiments"
    confirmations = []
    for experiment_id, (method, action, runner) in zip(ids, recipes):
        reply = experiments.review_test_prepare(venture_id, "", {"experiment_id": experiment_id})
        if is_local() and action == "experiment.page.configure":
            assert reply["actions"][0]["label"] == "Connect your coding tool"
            assert not any(item.get("confirmation") for item in reply["actions"])
            continue
        if runner is None or is_local() and method == "Recruit interviews":
            running = is_local() and method == "Recruit interviews"
            assert ("Your test is running:" if running else "Prepared your experiment:") in reply["body_md"]
            assert not any(item.get("confirmation") for item in reply["actions"])
            assert ledger.get(experiment_id, venture_id)["state"] == ("running" if running else "proposed")
            continue
        offer = reply["actions"][0]
        card = offer.get("confirmation") or {"action": offer["action"], "resulting_state": offer["payload"],
            "interpretation": reply["body_md"], "consequences": []}
        assert card["action"] == action, (method, reply)
        assert offer["label"] == ("Prepare it for me" if runner is None else "Run it for me")
        assert "credits" not in (card["interpretation"] + " ".join(card["consequences"])).lower()
        assert "run it yourself" not in json.dumps(reply) and "—" not in json.dumps(reply, ensure_ascii=False)
        payload = card["resulting_state"]
        if action == "experiment.run":
            spec = next(item for item in autonomous.available(venture_id) if item["id"] == payload["experiment_id"])
            assert spec["scan"] == runner and spec["icp"]["who"] == audience
            assert type(payload["ceiling_micro"]) is int and payload["ceiling_micro"] == spec["ceiling_micro"] >= 0
            assert payload["fingerprint"] == spec["fingerprint"]
        elif runner:
            assert payload["kind"] == runner
        confirmations.append(card)
    assert {item["id"] for item in ledger.list_for_venture(venture_id) if item["state"] != "proposed"} == ({ids[2]} if is_local() else set())
    assert not calls
    for method, action in (("Prepare a Wizard-of-Oz form", "build.prepare"),
                           ("Use my browser to check https://forms.example/intake", "mac.task.prepare")):
        reply = experiments._runner_prepare(venture_id, {"hypothesis": f"{audience}: {problem}", "action": method})
        assert reply["actions"][0]["action"] == action and "ceiling" not in reply["body_md"]
    monkeypatch.setattr(experiments, "_saved_validation_tests", lambda _venture_id: [{
        "hypothesis": f"{audience} choose a proposed price for {problem}", "action": "Test pricing",
        "variants": [{"key": "lower", "label": "Lower", "price": "$12 per month"},
                     {"key": "higher", "label": "Higher", "price": "$29 per month"}]}])
    pricing = experiments.review_test_prepare(venture_id, "", {"hypothesis": f"{audience} choose a proposed price for {problem}"})
    if is_local():
        assert pricing["actions"][0]["label"] == "Connect your coding tool"
        assert not any(item.get("confirmation") for item in pricing["actions"])
    else:
        assert pricing["actions"][0]["confirmation"]["action"] == "experiment.page.configure"
    pricing_spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "pricing")
    source = {"url": "https://alternatives.example/pricing", "title": audience, "price_points": [
        {"price": "Free", "billing_period": "month"}, {"price": "$18", "billing_period": "month"},
        {"price": "$45", "billing_period": "month"}, {"price": "EUR 30", "billing_period": "month"},
        {"price": "$100", "billing_period": "year"}]}
    result = autonomous.result_reply({**pricing_spec, "result": {"insight": f"Public prices for {problem}", "sources": [source]}})
    assert result["actions"][0]["payload"]["variants"] == [
        {"key": "price1", "label": "Price 1", "price": "$18 per month"},
        {"key": "price2", "label": "Price 2", "price": "$45 per month"}]
    assert page_tests.price_variants([{"price_points": [{"price": "$18", "billing_period": "month", "billing_unit": "seat"}]}])[0]["price"] == "$18 per seat per month"
    first = confirmations[0]
    with db()() as session:
        assert not list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))
    for _repeat in range(2):
        response = client.post(f"/ventures/{venture_id}/chat/confirm", json={
            "action": first["action"], "payload": first["resulting_state"]})
        assert response.status_code == 200, response.text
    run_jobs()
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == autonomous.KIND)))) == 1
        context = json.loads(session.get(Experiment, first["resulting_state"]["experiment_id"]).context)
        assert context["result"]["insight"] and calls
    prepared = asyncio.run(experiments.execute(venture_id, "experiment.run_review", {"run": True, "experiment_id": ids[-1]}, None))
    assert "Interview script:" in prepared["body_md"] and "LinkedIn automation is forbidden" in prepared["body_md"]
    assert len(prepared["body_md"].split("Connection note:\n", 1)[1].split("\n\n", 1)[0]) <= 300
    assert "Nothing is sent or posted for you" not in prepared["body_md"]
    assert ledger.get(ids[-1], venture_id)["state"] == "proposed"


@pytest.fixture(params=[("company", "warehouse teams", "tracking inventory shortages", "Stock Ledger"),
                        ("consumer", "home gardeners", "tracking watering schedules", "Plant Journal")],
                ids=["b2b-inventory", "consumer-gardening"])
def chat_experiment_market(tmp_path, monkeypatch, request):
    import os
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from tests.company_os_harness import load_company_os_app

    database = f"sqlite:///{tmp_path}/experiment_chat.db"
    postgres = os.environ.get("TEST_POSTGRES_URL")
    if postgres:
        address = make_url(postgres)
        assert address.host in {"127.0.0.1", "localhost"}
        schema = "m_expflow_" + uuid4().hex
        engine = create_engine(address)
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
        engine.dispose()
        database = address.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", database)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "working.sqlite3"))
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    with TestClient(main.create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": request.param[2]})
        assert created.status_code == 201
        yield market.__wrapped__((client, created.json()["id"], created.json()["workspace_id"]), monkeypatch, request)


@pytest.mark.parametrize("people_found,drafts_found", [(False, False), (True, False), (True, True)],
                         ids=["after-idea", "after-people", "after-drafts"])
def test_payment_experiment_outreach_and_results_stay_in_chat(chat_experiment_market, monkeypatch, people_found, drafts_found):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.credits import policy
    from app.db.models import GtmContact, GtmOutreachSettings, Venture
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.state import db
    from app.ventures.current_idea import current_revision
    from core.clock import utcnow
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    client, venture_id, workspace_id, view, *_rest = chat_experiment_market
    customer, problem = view["icp"]["who"], view["icp"]["problem"]
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    from app.core.local_mode import is_local
    if not is_local():
        assert policy.wallet_phase() == "person"
    else:
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe
        update_settings("smtp", {"host": "smtp.example", "user": "you@local.example",
            "sender_name": "Your founder", "postal_address": "42 Cedar Street, Springfield, US"},
            {"password": token_urlsafe(24)})
    with db()() as session:
        revision = current_revision(session.get(Venture, venture_id), session)
        append_idea_revision(session, revision.id, structured=json.dumps({"customer": customer, "problem": problem}))
        row = journey._row(session, venture_id, create=True)
        row.provenance_json = json.dumps({"icp": view["icp"], "pitch": f"You help {customer} with {problem}."})
        session.add(GtmOutreachSettings(workspace_id=workspace_id, sender_postal_address="42 Cedar Street, Springfield, US",
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    calls = []

    async def no_model(*_args):
        return None

    async def email_drafts(target, recipients, request):
        calls.append((target, request))
        return {recipient["to"].lower(): {"subject": "Your recent experience", "body":
            f"You work on {problem}. Could you share your most recent workaround in a 15-minute interview?"}
            for recipient in recipients}

    monkeypatch.setattr(experiments, "_llm_json", no_model)
    monkeypatch.setattr(outreach, "_llm_drafts", email_drafts)

    def found_people():
        with db()() as session:
            for index in range(3):
                session.add(GtmContact(id=f"found_{index}", workspace_id=workspace_id, email=f"reader{index}@public.example",
                    source=f"venture:{venture_id}:public", recipient_geo="US", email_confidence="risky",
                    do_not_contact=int(index == 2), dossier_hook=f"You have experience with {problem}.",
                    enrichment_json=json.dumps({"contact_source_url": "https://public.example/contact"}),
                    created_at=utcnow(), stage_changed_at=utcnow()))
            session.commit()

    if people_found:
        found_people()
    if drafts_found:
        for index in range(3):
            outreach._save_draft(venture_id, {"contact_id": f"found_{index}", "to": f"reader{index}@public.example",
                "channel": "email", "day": 1, "state": "draft", "batch_id": "discovery",
                "requires_journey_review": True, "requires_batch_approval": True,
                "subject": "Your recent experience", "body": f"Could you share your latest experience with {problem} in a 15-minute call?"})
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Test whether customers would pay."})
    assert response.status_code == 200
    prepared = response.json()
    assert customer in prepared["body_md"] and problem in prepared["body_md"], prepared["body_md"]
    saved = ledger.list_for_venture(venture_id)
    assert len(saved) == 1 and saved[0]["state"] == "running"
    identifier = saved[0]["id"]
    assert not calls and len(outreach.drafts(venture_id)) == (3 if drafts_found else 0)
    assert any(action.get("action") in {"gtm.find", "outreach.draft"} for action in prepared["actions"])
    assert "already found" in prepared["body_md"] if people_found else "find people" in prepared["body_md"].lower()
    if not people_found:
        found_people()
    ledger.create_from_tests(venture_id, "validation", [{"hypothesis": "You can reach people through community posts."}])
    drafted_response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Prepare email drafts for me."})
    assert drafted_response.status_code == 200
    drafted = drafted_response.json()
    expected_calls = 0 if drafts_found else 1
    assert len(calls) == expected_calls, drafted["body_md"]
    if calls:
        assert customer in calls[0][1] and problem in calls[0][1]
    drafts = outreach.drafts(venture_id)
    assert len(drafts) == (3 if drafts_found else 2)
    for draft in drafts:
        assert "Your email address is listed publicly on public.example." in draft["body"]
        assert OPT_OUT in draft["body"] and "42 Cedar Street" in draft["body"]
        assert draft["state"] == "draft" and draft["requires_batch_approval"]
    with db()() as session:
        context = json.loads(session.get(Experiment, identifier).context)
    assert set(context["outreach_draft_ids"]) == {draft["doc_id"] for draft in drafts if draft["contact_id"] in {"found_0", "found_1"}}
    assert "reader2@public.example" not in drafted["body_md"]
    repeated = client.post(f"/ventures/{venture_id}/chat", json={"message": "Prepare email drafts for me."})
    assert repeated.status_code == 200 and len(calls) == expected_calls and len(outreach.drafts(venture_id)) == len(drafts)
    other = client.post("/ventures", json={"raw_idea": f"A different approach to {problem}"})
    assert other.status_code == 201
    confirm_saved_idea(other.json()["id"])
    with db()() as session:
        append_idea_revision(session, current_revision(session.get(Venture, other.json()["id"]), session).id,
            structured=json.dumps({"customer": customer, "problem": problem}))
        session.commit()
    foreign = client.post(f"/ventures/{other.json()['id']}/chat/confirm", json={"action": "outreach.draft",
        "payload": {"run": True, "batch": True, "experiment_id": identifier}})
    assert foreign.status_code == 200 and foreign.json()["actions"], foreign.json()["body_md"]
    assert not outreach.drafts(other.json()["id"]) and len(calls) == expected_calls
    previous = ledger.list_for_venture(venture_id)
    untouched = client.post(f"/ventures/{venture_id}/chat", json={"message": "I haven't run the test yet. Nobody will pay."})
    assert untouched.status_code == 200
    assert ledger.list_for_venture(venture_id) == previous
    report = "I emailed two people. Both declined and nobody paid."
    message = report
    if is_local():
        ambiguous = client.post(f"/ventures/{venture_id}/chat", json={"message": report})
        assert ambiguous.status_code == 200 and "Which test did you run?" in ambiguous.json()["body_md"]
        assert ledger.list_for_venture(venture_id) == previous
        message = f"Test results: {identifier}\nWhat happened: {report}"
    reported = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
    assert reported.status_code == 200
    final = {row["id"]: row for row in ledger.list_for_venture(venture_id)}
    assert final[identifier]["result_text"] == report
    assert all(not row["result_text"] for key, row in final.items() if key != identifier)
    if is_local():
        assert final[identifier]["state"] == "running" and final[identifier]["outcome"] is None
        assert final[identifier]["needs_evidence"] and "Add evidence" in reported.json()["body_md"]
        assert not any(row["state"] == "concluded" for row in final.values())
    else:
        assert final[identifier]["state"] == "concluded" and final[identifier]["outcome"] == "inconclusive"
        assert sum(row["state"] == "concluded" for row in final.values()) == 1
    assert not chat_experiment_market[6]
    history = client.get(f"/ventures/{venture_id}/chat")
    assert history.status_code == 200
    assert any(item["body_md"] == reported.json()["body_md"] for item in history.json()["items"])
    for reply in (prepared, drafted, untouched.json(), reported.json()):
        assert reply["kind"] != "confirmation" and not any(action.get("confirmation") for action in reply["actions"])
        assert "credit" not in json.dumps(reply).lower() and "\u2014" not in reply["body_md"]
        assert reply["actions"]


def test_reselected_chat_plan_not_newest_created_plan_gets_the_result(market):
    from app.experiments import ledger, recruitment

    venture_id, view = market[1], market[3]
    identifiers = ledger.create_from_tests(venture_id, "chat", [
        {"hypothesis": f"{view['icp']['who']} have paid for {view['icp']['problem']}.",
         "action": "Interview five people about recent spending."},
        {"hypothesis": f"{view['icp']['who']} spend time on {view['icp']['problem']}.",
         "action": "Interview five people about time spent."},
    ])
    for identifier in [*identifiers, identifiers[0]]:
        recruitment.prepare(venture_id, {"experiment_id": identifier, "run": True})
    assert recruitment.selected(venture_id)["id"] == identifiers[0]
    assert all(row["state"] == "running" for row in ledger.list_for_venture(venture_id))
    assert not market[6]


def test_email_draft_noun_routes_to_review_only_outreach(market):
    from app.chat.intent import classify_intent

    customer, problem = market[3]["icp"]["who"], market[3]["icp"]["problem"]
    assert classify_intent("Prepare email drafts for me.") == "outreach_draft"
    assert classify_intent(f"Prepare email drafts for {customer} about {problem}.") == "outreach_draft"
    assert classify_intent(f"Send emails to {customer} about {problem}.") != "outreach_draft"


def test_current_test_retry_runs_the_request_instead_of_prefilling_it(interview_plan, monkeypatch):
    from app.chat import experiments
    from app.chat.execute import execute_action

    market, _identifier = interview_plan
    venture_id, view = market[1], market[3]
    current = view["icp"]["who"]
    previous = "people outside your current customer group"
    snapshot = experiments._experiment_snapshot(venture_id)
    snapshot["superseded_customers"] = [previous]
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: snapshot)
    corrected = False

    async def plan(system, user, venture, **kwargs):
        customer = current if corrected else previous
        return {"hypothesis": f"You can help {customer} avoid recurring problems.",
            "action": f"Interview five consenting {customer} about recent workarounds. Do not send anything.",
            "success_measure": "Pass with three costly workarounds; fail with fewer; missing interviews are inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", plan)
    request = f"Prepare a test for {current}."
    blocked = asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": request}, None))
    button = next(item for item in blocked["actions"] if item["label"] == "Prepare your current test")
    assert button["action"] == "experiment.start" and button["payload"]["text"] == request
    corrected = True
    ready = asyncio.run(execute_action(venture_id, button["action"], payload=button["payload"]))
    assert "Prepared your experiment" in ready["body_md"] and current in ready["body_md"]
    assert not any(item.get("confirmation") for item in ready["actions"])

    async def unavailable(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(experiments, "_llm_json", unavailable)
    timed_out = asyncio.run(execute_action(venture_id, button["action"], payload=button["payload"]))
    assert "too long" in timed_out["body_md"] and "Nothing was started" in timed_out["body_md"]
    assert timed_out["actions"][0]["action"] == "experiment.start"


@pytest.mark.parametrize("provider_validation", [False, True], ids=["returned-json", "provider-validation"])
def test_rejected_design_prepares_saved_customer_plan(interview_plan, monkeypatch, provider_validation):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.experiments import ledger
    from app.idea.validation_agent import ProposedTest

    market, _identifier = interview_plan
    venture_id, view = market[1], market[3]
    request = f"Design an experiment for {view['icp']['who']} about {view['icp']['problem']}."
    plan = {"hypothesis": f"{view['icp']['who']} need help with {view['icp']['problem']}.",
        "action": "Interview five consenting adults this week about their latest workaround.",
        "success_measure": "Pass with three costly workarounds; fail with fewer; missing interviews are inconclusive."}
    calls = []
    invalid = {**plan, "action": "You record their recent workaround. " * 100}

    async def design(system, user, venture, **kwargs):
        calls.append((system, user, venture))
        if provider_validation:
            ProposedTest.model_validate(invalid)
        return invalid

    monkeypatch.setattr(experiments, "_llm_json", design)
    original = ledger.list_for_venture(venture_id)
    drafts = outreach.drafts(venture_id)
    prepared = asyncio.run(experiments.dispatch(venture_id, "experiment_start", request))
    assert "Prepared your experiment" in prepared["body_md"]
    assert view["icp"]["who"] in prepared["body_md"] and view["icp"]["problem"] in prepared["body_md"]
    assert "Interview script:" in prepared["body_md"] and "Readout:" in prepared["body_md"]
    script = prepared["body_md"].split("Interview script:", 1)[1].split("\n\n", 1)[0]
    assert view["icp"]["who"] in script and view["icp"]["problem"] in script and "?" in script
    assert "I need your customer" not in prepared["body_md"]
    assert "Next step" in prepared["body_md"]
    assert "Add your answer" not in prepared["body_md"]
    assert all(word in prepared["body_md"].lower() for word in ("pass", "fail", "inconclusive"))
    saved = ledger.list_for_venture(venture_id)
    assert len(saved) == len(original) + 1
    ProposedTest.model_validate({"hypothesis": saved[-1]["hypothesis"], "action": saved[-1]["action"],
        "success_measure": saved[-1]["measure"]})
    assert not any(action.get("confirmation") for action in prepared["actions"])
    assert "action" in calls[1][1] and "string_too_long" in calls[1][1]
    assert "2000" in calls[1][0]
    assert outreach.drafts(venture_id) == drafts
    assert len(calls) == 2 and not market[6]


@pytest.mark.parametrize("provider_validation", [False, True], ids=["returned-json", "provider-validation"])
@pytest.mark.parametrize("previous_result", [False, True], ids=["first-test", "after-supported-test"])
def test_rejected_design_fallback_preserves_your_requested_method_and_criteria(market, monkeypatch,
        provider_validation, previous_result):
    from app.autopilot import outreach
    from app.chat import experiments
    from app.chat.execute import execute_action
    from app.experiments import ledger
    from app.idea.validation_agent import ProposedTest

    venture_id, view = market[1], market[3]
    method, criteria = (
        ("a private stock-log review", "Pass if four team leads schedule a log review by Friday; fail if none do; otherwise inconclusive.")
        if view["icp"]["mode"] == "company" else
        ("a private watering-diary comparison", "Pass if six gardeners compare two diary entries by Sunday; fail if none do; otherwise inconclusive.")
    )
    request = f"Design {'the next' if previous_result else 'an'} experiment for {view['icp']['who']} using {method}. {criteria}"
    snapshot = experiments._experiment_snapshot(venture_id)
    snapshot["confirmed_scope"] = {"customer": view["icp"]["who"], "problem": view["icp"]["problem"]}
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: snapshot)
    if previous_result:
        identifier = ledger.create_from_tests(venture_id, "chat", [{
            "hypothesis": "You can learn whether a recurring problem costs time.",
            "action": "You interview five consenting people about their recent workarounds.",
            "success_measure": "Pass if three describe lost time; fail if none do; otherwise inconclusive.",
        }])[0]
        ledger.record_result(identifier, venture_id, True, "I interviewed five people; four described losing an hour.",
            "supported", "You have reported recurring time costs, not evidence of payment.", sample_size=5,
            provenance={"source": "Your anonymized interview notes", "method": "Interviews", "observed_at": utcnow().isoformat()})
    plan = {"hypothesis": f"You can test {method}.", "action": request, "success_measure": criteria}
    invalid = {**plan, "action": request * 40}
    calls = []

    async def design(system, user, venture, **kwargs):
        calls.append(user)
        assert request in user and kwargs["response_schema"] == ProposedTest.model_json_schema()
        if provider_validation:
            ProposedTest.model_validate(invalid)
        return invalid

    monkeypatch.setattr(experiments, "_llm_json", design)
    original = ledger.list_for_venture(venture_id)
    drafts = outreach.drafts(venture_id)
    prepared = asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": request}, None))
    assert "Prepared your experiment" in prepared["body_md"]
    saved = ledger.list_for_venture(venture_id)
    assert len(saved) == len(original) + 1 and saved[:len(original)] == original
    runner = next(action for action in prepared["actions"] if action["label"] == "Run it for me")
    selected = ledger.get(runner["payload"]["experiment_id"], venture_id)
    assert request.removeprefix("Design ") in selected["action"] and method in prepared["body_md"]
    assert criteria in selected["measure"] and criteria in prepared["body_md"]
    assert not any(word in (selected["action"] + selected["measure"]).lower()
        for word in ("paid-trial", "paid trial", "past spending"))
    assert selected["state"] == "proposed"
    ProposedTest.model_validate({"hypothesis": selected["hypothesis"], "action": selected["action"],
        "success_measure": selected["measure"]})
    assert len(calls) == 2 and "string_too_long" in calls[1]
    assert not any(action.get("confirmation") for action in prepared["actions"])
    assert outreach.drafts(venture_id) == drafts and not market[6]

    oversized_request = request + " Keep the plan private." * 100
    before_retry = ledger.list_for_venture(venture_id)

    async def oversized_design(*args, **kwargs):
        return {**plan, "action": oversized_request}

    monkeypatch.setattr(experiments, "_llm_json", oversized_design)
    rejected = asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": oversized_request}, None))
    assert "could not prepare a complete plan" in rejected["body_md"]
    assert ledger.list_for_venture(venture_id) == before_retry
    retry = rejected["actions"][0]
    assert retry["action"] == "experiment.start" and retry["payload"]["text"] == oversized_request
    assert retry["payload"]["repair_feedback"] == {"action": "string_too_long"}

    async def repaired_design(system, user, venture, **kwargs):
        assert oversized_request in user and "string_too_long" in user
        return {**plan, "action": request + " Keep the plan private."}

    monkeypatch.setattr(experiments, "_llm_json", repaired_design)
    repaired = asyncio.run(execute_action(venture_id, retry["action"], payload=retry["payload"]))
    assert "Prepared your experiment" in repaired["body_md"] and criteria in repaired["body_md"]
    assert len(ledger.list_for_venture(venture_id)) == len(before_retry) + 1
    assert not any(action.get("confirmation") for action in repaired["actions"])
    assert outreach.drafts(venture_id) == drafts and not market[6]


def test_constrained_follow_up_retry_repairs_schema_and_keeps_the_selected_test(interview_plan, monkeypatch):
    from app.autopilot import outreach
    from app.chat import assistant, customers, experiments
    from app.chat.execute import execute_action
    from app.chat.store import CLIENT_REQUEST_ID
    from app.experiments import ledger, recruitment
    from app.idea.validation_agent import ProposedTest
    from core.clock import utcnow

    market, identifier = interview_plan
    venture_id, view = market[1], market[3]
    safety = "Do not send invitations or collect identifying records."
    request = f"Design a follow-up test for {view['icp']['who']}. Plan five interviews during one week. {safety}"
    snapshot = experiments._experiment_snapshot(venture_id)
    snapshot["confirmed_scope"] = {"customer": view["icp"]["who"], "problem": view["icp"]["problem"],
        "constraints": [safety], "requirements": ["Keep demand separate from product performance."]}
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: snapshot)
    ledger.record_result(identifier, venture_id, True, "I interviewed five people; two described a recent workaround.",
        "inconclusive", "You still need evidence of a recurring cost.", sample_size=5,
        provenance={"source": "Your interview notes", "method": "Voluntary conversations", "observed_at": utcnow().isoformat()})
    plan = {"hypothesis": f"You can test demand among {view['icp']['who']} independently of product performance.",
        "action": f"You plan five interviews during one week about {view['icp']['problem']}. {safety} "
            "Ask about the cost of the previous workaround. Keep demand separate from product performance.",
        "success_measure": "Pass if three describe a recurring cost; fail if none do; otherwise inconclusive, including missing interviews."}
    invalid = {**plan, "action": plan["action"] * 30}
    replies = {}
    calls = []

    async def complete(messages, **kwargs):
        digest = kwargs["request_id"].split(":experiment-json:")[-1]
        calls.append((messages, kwargs))
        assert kwargs["response_schema"] == ProposedTest.model_json_schema()
        assert kwargs["workspace_id"] == market[2] and kwargs["venture_id"] == venture_id
        assert request in messages[1]["content"] and safety in messages[1]["content"]
        assert "recurring cost" in messages[1]["content"]
        if digest not in replies:
            replies[digest] = json.dumps(invalid if len(replies) < 2 else plan)
        return replies[digest]

    monkeypatch.setattr(assistant, "_complete", complete)
    original = ledger.list_for_venture(venture_id)
    drafts = outreach.drafts(venture_id)
    token = CLIENT_REQUEST_ID.set("bounded-follow-up")
    try:
        failed = asyncio.run(execute_action(venture_id, "experiment.start", payload={"text": request}))
        assert "could not prepare a complete plan" in failed["body_md"]
        assert ledger.list_for_venture(venture_id) == original
        retry = failed["actions"][0]
        prepared = asyncio.run(execute_action(venture_id, retry["action"], payload=retry["payload"]))
    finally:
        CLIENT_REQUEST_ID.reset(token)
    assert "Prepared your experiment" in prepared["body_md"]
    runner = next(action for action in prepared["actions"] if action["label"] == "Run it for me")
    selected = recruitment.selected(venture_id, runner["payload"]["experiment_id"])
    assert selected["action"] == plan["action"] and safety in prepared["body_md"]
    ProposedTest.model_validate({"hypothesis": selected["hypothesis"], "action": selected["action"],
        "success_measure": selected["measure"]})
    assert len(replies) == 3 and len(calls) == 3
    assert "String should have at most 2000 characters" in calls[1][0][1]["content"]
    assert "string_too_long" in calls[-1][0][1]["content"]
    assert outreach.drafts(venture_id) == drafts and not market[6]
    assert not any(action.get("confirmation") for action in prepared["actions"])
    for malformed in ([], {"action": 1}, {"customer": "string_too_long"}, {"action": "a" * 65}):
        rejected = asyncio.run(experiments.execute(venture_id, "experiment.start",
            {"text": request, "repair_feedback": malformed}, None))
        assert "Your retry details changed" in rejected["body_md"]
    assert len(calls) == 3 and ledger.list_for_venture(venture_id)[-1]["id"] == selected["id"]

    searches = []

    async def find(venture, action, payload, actor_subject):
        searches.append(payload)
        return experiments._reply("Your test is running. Next: review your interview plan.")

    monkeypatch.setattr(customers, "execute", find)
    running = asyncio.run(experiments.dispatch(venture_id, "experiment_from_review", "Run it for me."))
    assert "Your test is running" in running["body_md"] and "Your experiments:" not in running["body_md"]
    assert ledger.get(selected["id"], venture_id)["state"] == "running"
    assert searches and all(payload["note"] == plan["hypothesis"] for payload in searches)
    assert outreach.drafts(venture_id) == drafts and not market[6]
    assert not any(action.get("confirmation") for action in running["actions"])


@pytest.mark.parametrize("next_step", ["find", "draft"])
def test_running_a_manual_test_keeps_its_recruiting_handoff(interview_plan, monkeypatch, next_step):
    from app.autopilot import outreach
    from app.chat import experiments, router
    from app.chat.context import ChatContext
    from app.chat.execute import execute_action
    from app.db.models import GtmContact, Workspace
    from app.experiments import ledger, recruitment
    from app.state import db
    from app.tenancy import bind

    market, identifier = interview_plan
    venture_id = market[1]
    drafted = []
    with db()() as session:
        actor = session.get(Workspace, market[2]).owner_id
    def requested(awaitable):
        with bind(workspace_id=market[2], venture_id=venture_id, actor=actor):
            return asyncio.run(awaitable)
    if next_step == "draft":
        monkeypatch.setattr(outreach, "contacts", lambda *_args: [GtmContact(id="prepared_person", workspace_id=market[2],
            email="reader@local.example", times_emailed=0, do_not_contact=0)])
        async def draft(venture_id, row, request=""):
            drafted.append(row["id"])
            return {"kind": "text", "body_md": "Your invitations are saved. Review your drafts.",
                    "actions": [{"label": "Show your drafts", "action": "outreach.drafts"}], "citations": []}
        monkeypatch.setattr(recruitment, "draft", draft)
    prepared = requested(execute_action(venture_id, "experiment.recommendation.prepare",
        payload={"experiment_id": identifier, "run": False}))
    assert ledger.get(identifier, venture_id)["state"] == "proposed"
    assert prepared["actions"][0]["action"] == ("gtm.find" if next_step == "find" else "outreach.draft")
    assert not drafted and not market[6]
    for reply in (requested(experiments.dispatch(venture_id, "experiment_from_review", "Run it for me.")),
            requested(experiments.dispatch(venture_id, "experiment_start", "Run it for me.")),
            requested(router._dispatch_intent(venture_id,
                ChatContext(venture={"id": venture_id, "workspace_id": market[2]}),
                "experiment_start", "Run it for me.", None)),
            requested(execute_action(venture_id, "experiment.recommendation.prepare",
                payload={"experiment_id": identifier})),
            requested(execute_action(venture_id, "experiment.run_review",
                payload={"experiment_id": identifier, "run": True}))):
        assert "Prepared your experiment" not in reply["body_md"]
        assert reply["actions"]
        assert not any(action.get("confirmation") for action in reply["actions"])
    assert recruitment.selected(venture_id, identifier)["id"] == identifier
    assert ledger.get(identifier, venture_id)["state"] == "running"
    if next_step == "find":
        from app.db.models import Job
        from app.state import db
        with db()() as session:
            assert session.query(Job).filter_by(venture_id=venture_id, kind="gtm.journey.prepare").count() == 1
    else:
        assert drafted == [identifier] * 5 and not market[6]


def test_recording_results_forwards_evidence_and_requests_missing_details(interview_plan, monkeypatch):
    from app.core.local_mode import is_local
    from app.chat import experiments, store
    from app.chat.confirmations import start_requested_work
    from app.credits import policy
    from app.experiments import ledger
    from core.clock import utcnow

    market, identifier = interview_plan
    venture_id, view = market[1], market[3]
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True, "experiments_quality_v1": True}, "test")
    observed = utcnow().isoformat()
    report = (f"I interviewed five {view['icp']['who']} about {view['icp']['problem']}. "
        "Four described costly workarounds. I kept anonymized interview notes today.")
    metadata = {"sample_size": 5, "provenance": {"source": "Your anonymized interview notes",
        "method": "Interviews", "observed_at": observed}}
    extracted = {"ran": True, "outcome": "supported", "learning": "You reported costly workarounds."}

    async def extract(system, user, venture):
        assert "sample_size" in system and "observed_at" in system
        return {**extracted, **metadata}

    monkeypatch.setattr(experiments, "_llm_json", extract)
    store.append_message(venture_id, role="user", body_md=report)
    store.append_message(venture_id, role="user", body_md="Record these results.")
    invalid_reports = [{"sample_size": 0, "provenance": metadata["provenance"]},
            {"sample_size": 5, "provenance": {**metadata["provenance"], "observed_at": "invalid"}},
            {"sample_size": 5, "provenance": {**metadata["provenance"],
                "observed_at": (utcnow() + timedelta(days=1)).isoformat()}}]
    if not is_local():
        invalid_reports.insert(0, {})
    for invalid in invalid_reports:
        reply = asyncio.run(experiments.record_run(venture_id, {"experiment_id": identifier,
            "result_text": report, **extracted, **invalid}))
        assert "Open Experiments and choose Record result or Add evidence" in reply["body_md"]
        assert "positive count" in reply["body_md"] and "not in the future" in reply["body_md"]
        assert "retry" not in reply["body_md"].lower()
        assert ledger.get(identifier, venture_id)["state"] == "proposed"
    prepared = asyncio.run(experiments.record_prepare(venture_id, "Record these results."))
    if not is_local():
        payload = prepared["actions"][0]["confirmation"]["resulting_state"]
        assert payload["sample_size"] == 5 and payload["provenance"] == metadata["provenance"]
    reply = asyncio.run(start_requested_work(venture_id, prepared, actions={"experiment.record"}))
    saved = ledger.get(identifier, venture_id)
    assert saved["state"] == "concluded" and saved["evidence_type"] == "founder_reported"
    assert saved["sample_size"] == 5 and saved["provenance"] == metadata["provenance"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert "credit" not in reply["body_md"].lower() and not market[6]
    original = ledger.list_for_venture(venture_id)
    repeated = asyncio.run(experiments.record_prepare(venture_id, "Record these results."))
    assert repeated["kind"] == "text" and "already saved" in repeated["body_md"]
    assert repeated["actions"][0]["payload"]["experiment_id"] == identifier
    assert ledger.list_for_venture(venture_id) == original
