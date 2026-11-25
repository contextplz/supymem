import asyncio
import json

import pytest

from tests.company_os_harness import local_owner_clients
from tests.unit.test_autonomous_experiments import market, venture
from tests.unit.test_local_mode import local_owner_app as local_owner_app


@pytest.mark.parametrize("audience", ["freight scheduling teams", "families planning camping trips"])
@pytest.mark.parametrize("supplied", [False, True])
def test_local_http_results_save_owner_reports_without_inventing_evidence(local_owner_app, monkeypatch, audience, supplied):
    from fastapi.testclient import TestClient
    from app.chat import experiments
    from app.core.local_owner import ensure_owner
    from app.db.models import Venture
    from app.experiments import ledger
    from app.state import db
    from core.local_security import _proxy_secret
    from tests.unit.pagebuilder.test_api import seed

    monkeypatch.setattr(ledger, "quality_enabled", lambda: True)
    async def extracted(*args, **kwargs):
        return {"ran": not supplied, "outcome": "inconclusive", "learning": "You reported observations, not verified demand.",
            "sample_size": 5, "provenance": {"source": None, "method": "Interviews", "observed_at": None}, "complete": True}
    monkeypatch.setattr(experiments, "_llm_json", extracted)
    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode()
        data = seed(db(), actor=ensure_owner().id)
        with db()() as session:
            session.get(Venture, data["vid"]).raw_idea = f"Help {audience} plan together"
            session.commit()
        identifier, = ledger.create_from_tests(data["vid"], "chat", [{"hypothesis": f"{audience} need easier planning",
            "action": f"Interview five consenting {audience}", "success_measure": "Two agree to review a prototype"}])
        report = f"I interviewed five {audience}; two agreed to review a prototype."
        if supplied:
            report = "For a disposable trial, " + report + " These are supplied test inputs, not verified market evidence."
        response = client.post(f'/ventures/{data["vid"]}/chat', json={"message": "Results: " + report, "idempotency_key": "reported-results"})
        assert response.status_code == 200
        monkeypatch.setattr(ledger, "quality_enabled", lambda: False)
        saved = next(item for item in client.get(f'/ventures/{data["vid"]}/experiments').json()["items"]
            if item["id"] == identifier)
        assert report in saved["result_text"]
        assert saved["evidence_type"] == "founder_reported" and saved["evidence_label"] == "reported by you"
        assert saved["state"] == "running" and saved["outcome"] is None and saved["needs_evidence"]
        assert saved["sample_size"] == 5
        assert "Add evidence" in response.json()["body_md"]
        assert not saved["provenance"].get("observed_at")
        assert not any(action.get("confirmation") for action in response.json().get("actions", []))


@pytest.mark.parametrize("audience,problem", [("factory maintenance coordinators", "missed inspection appointments"),
                                             ("families sharing allotments", "missed watering turns")])
def test_prepared_interview_plan_renders_its_script_once(venture, monkeypatch, audience, problem):
    from app.chat import experiments
    from app.core.local_mode import is_local
    from app.experiments import coach, ledger

    venture_id = venture[1]
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {"journey": {"icp": {"who": audience, "problem": problem}}})
    script = coach.interview_script(audience, problem, "Check your last missed task", "Interview five consenting participants")
    assert script
    experiment_id, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "Check your last missed task",
        "action": "Interview five consenting participants.\n" + script, "success_measure": "Record five anonymous observations"}])
    reply = experiments.review_test_run(venture_id, {"run": True, "experiment_id": experiment_id})
    for question in script.splitlines():
        if question.endswith("?"):
            assert reply["body_md"].count(question) == (1 if is_local() else 2)
    assert "five consenting participants" in reply["body_md"]


@pytest.mark.parametrize("audience", ["independent cinema operators", "families planning bike trips"])
@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_empty_confirmed_experiment_reply_is_not_replayed_for_a_repair(venture, monkeypatch, audience, mode):
    from app.chat import experiments
    from app.experiments import recruitment

    venture_id = venture[1]
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    if mode == "local":
        from app.core.providers_local import save_settings

        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
            models={"chat": "local-test-model"})
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {"confirmed_scope": {
        "customer": audience, "problem": "Missed planning steps"}})
    monkeypatch.setattr(experiments, "_idea_gate", lambda _: None)
    monkeypatch.setattr(experiments.ledger, "list_for_venture", lambda _: [])
    calls, plans = [], []

    async def empty(*args, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(experiments, "_llm_json", empty)
    monkeypatch.setattr(recruitment, "prepare", lambda venture_id, plan, **kwargs: plans.append(plan) or {
        "body_md": "Your plan is ready. Review your test next."})
    reply = asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": "Design an experiment."}, None))
    assert len(calls) == (1 if mode == "local" else 2)
    assert len(plans) == 1
    assert audience in plans[0]["action"] and "consenting" in plans[0]["action"]
    assert "Review" in reply["body_md"]


@pytest.mark.parametrize("audience", ["warehouse dispatchers", "weekend gardeners"])
def test_requested_customer_interview_gets_proposed_measurable_criteria(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import recruitment

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(experiments, "_idea_gate", lambda _: None)
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {"confirmed_scope": {
        "customer": audience, "problem": "Missed planning steps"}})
    monkeypatch.setattr(experiments.ledger, "list_for_venture", lambda _: [])
    async def empty(*args, **kwargs):
        return None
    plans = []
    monkeypatch.setattr(experiments, "_llm_json", empty)
    monkeypatch.setattr(recruitment, "prepare", lambda _, plan, **kwargs: plans.append(plan) or {
        "body_md": "Your test is ready. Open Experiments to review it."})
    asyncio.run(experiments.execute(venture[1], "experiment.start", {"text":
        "Design a customer interview experiment for my saved idea and save its hypothesis, action and success measure. Do not run it or send anything."}, None))
    assert len(plans) == 1
    plan = plans[0]
    assert audience in plan["action"] and "consenting" in plan["action"]
    assert "three of eight" in plan["success_measure"] and "all eight decline" in plan["success_measure"]
    assert "inconclusive" in plan["success_measure"] and "proposed criteria" in plan["success_measure"]
    assert "requested success criteria" not in plan["success_measure"]


def test_saved_unrun_interview_is_recommended_before_an_unrequested_scan(market, monkeypatch):
    from app.chat import experiments
    from app.experiments import ledger

    venture_id = market[1]
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    experiment_id, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "Your audience experiences the stated problem",
        "action": "Interview five consenting participants about their latest example",
        "success_measure": "Record five anonymous observations"}])
    reply = experiments.list_reply(venture_id)
    assert "Recommended: Your saved test" in reply["body_md"]
    assert any(action.get("style") == "primary" and action.get("payload", {}).get("experiment_id") == experiment_id
               for action in reply["actions"])


@pytest.mark.parametrize("audience,problem", [("warehouse maintenance teams", "missed inspections"),
                                             ("weekend walking groups", "planning accessible routes")])
def test_sample_experiment_hypothesis_reads_naturally_without_echoing_your_command(venture, monkeypatch, audience, problem):
    from app.chat import experiments
    from app.experiments import recruitment

    venture_id = venture[1]
    hypothesis = f"{audience} will try a new way to address {problem}"
    request = f"Design a one-week experiment to test if {hypothesis}. Do not collect private records."
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {"confirmed_scope": {"customer": audience, "problem": problem}})
    monkeypatch.setattr(experiments, "_idea_gate", lambda _: None)
    monkeypatch.setattr(experiments.ledger, "list_for_venture", lambda _: [])

    async def invalid_plan(*args, **kwargs):
        return {}

    plans = []
    monkeypatch.setattr(experiments, "_llm_json", invalid_plan)
    monkeypatch.setattr(recruitment, "prepare", lambda venture_id, plan, **kwargs: plans.append(plan) or {"body_md": "Your plan is ready. Review it next."})
    asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": request}, None))
    assert len(plans) == 1
    assert hypothesis in plans[0]["hypothesis"]
    assert "You test this request:" not in plans[0]["hypothesis"]
    assert request not in plans[0]["hypothesis"]
    assert request not in plans[0]["action"]
    assert request not in plans[0]["success_measure"]
    assert "one-week" in plans[0]["action"]
    assert "Do not collect private records" in plans[0]["action"]
    assert "Do not send messages" in plans[0]["action"]
    assert audience in plans[0]["action"]
    assert "interview" in plans[0]["action"].lower()
    assert "current workaround" in plans[0]["action"]
    assert "follow only the method" not in plans[0]["action"]
    assert "Your test:" not in plans[0]["success_measure"]
    assert "three of eight" in plans[0]["success_measure"]
    assert "inconclusive" in plans[0]["success_measure"]
    assert "not results" in plans[0]["success_measure"]
    asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": request.replace("will try", "will pay for")}, None))
    assert "paid pilot at an agreed price and scope" in plans[-1]["success_measure"]
    assert "three of eight" in plans[-1]["success_measure"]
    assert "Do not collect private records" in plans[-1]["action"]
    asyncio.run(experiments.execute(venture_id, "experiment.start", {"text": "Design an experiment."}, None))
    assert "You prepare your interview plan" in plans[-1]["action"]
    assert audience in plans[-1]["action"] and "current workaround" in plans[-1]["action"]
    assert "three of eight" in plans[-1]["success_measure"]


@pytest.mark.usefixtures("venture")
@pytest.mark.parametrize("audience", ["dental laboratory owners", "recreational kayakers"])
def test_reported_purchase_commitments_are_not_discarded_as_hypothetical(audience):
    from app.experiments import coach

    evidence = coach.classify_interview(f"I talked to eight {audience} and five said they would commit to a conditional pre-order.")
    assert evidence["weight"] == 2 and evidence["commitments"]
    evidence = coach.classify_interview(f"I talked to eight {audience}; five said they agreed to pre-order if you deliver the promised scope.")
    assert evidence["weight"] == 2 and evidence["commitments"]
    for statement in ("They said they might like it.", "They said they might commit to a pre-order.",
                      "They said if they agreed to a pre-order, they would pay later.", "They said they would not commit to a pre-order.",
                      "They said they might have pre-ordered.",
                      'I asked: "Would you commit to a pre-order?"'):
        evidence = coach.classify_interview(f"I talked to eight {audience}. {statement}")
        assert evidence["weight"] == 0 and not evidence["commitments"]


@pytest.fixture(params=[("freight dispatchers", "missed pickup windows"),
                        ("museum visitors", "finding accessible exhibits")], ids=["b2b-freight", "consumer-museums"])
def domain(request):
    return request.param


@pytest.mark.usefixtures("venture")
def test_question_repair_removes_conditional_lead_ins_without_losing_safety(domain):
    from app.experiments import coach

    audience, problem = domain
    valid = f"When did you last experience {problem}?"
    rejected = "If your next attempt succeeds, would you use a different approach?"
    safety = "You must not share identifying records. Keep your notes anonymous."
    for wrapper in ("", "Ask: ", "Then ask: '", "You must not share identifying records: ",
            "You will not share identifying records: "):
        action = f"Interview {audience}. {valid} 7. {wrapper}{rejected} {safety}"
        script = coach.interview_script(audience, problem, action=action)
        grounded = coach.ground_interview_action(action, script)
        assert "If your next attempt succeeds" not in grounded
        assert rejected not in grounded and valid in grounded
        assert f"7. {wrapper}" in grounded and safety in grounded
        assert coach._INTERVIEW_FALLBACK[0] in grounded
        assert "If your next attempt succeeds" not in script


@pytest.mark.parametrize("audience,problem", [
    ("independent laboratory managers", "late equipment reservations"),
    ("families keeping freshwater aquariums", "missed water changes"),
])
@pytest.mark.usefixtures("venture")
def test_quoted_valid_interview_spans_survive_mixed_question_grounding(audience, problem):
    from app.experiments import coach

    questions = [f"When did you last encounter {problem}?", "What did you try during that incident?",
        "What did that cost in time?", "Who helped you resolve it?"]
    rejected = "Would you buy a solution next month?"
    for opening, closing in [("'", "'"), ('"', '"'), ("“", "”")]:
        quoted = " ".join(f"{opening}{question}{closing}" for question in questions)
        action = f"Interview {audience}. Ask: {quoted} Then ask: {opening}{rejected}{closing} Keep all records private."
        script = coach.interview_script(audience, problem, action=action)
        assert script.splitlines()[1:] == questions
        grounded = coach.ground_interview_action(action, script)
        assert quoted in grounded and rejected not in grounded
        assert all(grounded.count(question) == 1 for question in questions)
        assert "Keep all records private." in grounded and "Then ask:" in grounded
        valid_action = f"Interview {audience}. Ask: {quoted} Keep all records private."
        assert coach.ground_interview_action(valid_action, script) == valid_action


@pytest.mark.usefixtures("venture")
def test_specific_interview_questions_survive_grounding_without_repeated_context(domain):
    from app.experiments import coach

    audience, problem = domain
    hypothesis = f"You can find recurring incidents of {problem} among {audience}."
    questions = [f"When did you last experience {problem}?", "Where did you keep the records?",
        "Who helped you resolve that incident?", "How long did it take?"]
    action = "Use voluntary referrals. Ask: " + " ".join(questions) + " Do not collect private records."
    script = coach.interview_script(audience, problem, hypothesis, action)
    assert all(question in script for question in questions)
    assert script.count(hypothesis.rstrip(".")) == 1 and script.count(audience) <= 2
    assert coach.ground_interview_action(action, script) == action
    fallback = coach.interview_script(audience, problem, hypothesis)
    assert fallback.count(hypothesis.rstrip(".")) == 1 and fallback.count("?") == 4


@pytest.mark.parametrize("audience,problem", [
    ("commercial kitchen managers", "missed refrigeration checks"),
    ("amateur sailing club members", "conflicting boat reservations"),
])
@pytest.mark.usefixtures("venture")
def test_grounding_preserves_concrete_commitments_approvals_and_instructions(audience, problem):
    from app.experiments import coach

    questions = [f"When did you last encounter {problem}?",
        "Would you agree in principle to a supervised pilot?",
        "What approvals would your sponsor require?",
        "Could you send a written conditional outline naming the sponsor and dates?",
        "When could you start the read-only pilot?",
        "Can you introduce us to the person who approves access?",
        "Do you agree to join one supervised session?",
        "Would you consent to observation without sharing private records?",
        "Could you provide read-only access after approval?",
        "Would you book a thirty-minute pilot session next Tuesday?",
        "Would you agree to pay a refundable deposit for a scheduled session?"]
    instructions = ("Explain that you have no working product. Describe your proposed supervised pilot. "
        "Do not promise results or collect private records. Access requires permission and safety approval.")
    rejected = ["Would you buy a solution someday?", "Don't you agree to a pilot because it sounds wonderful?"]
    action = f"Interview {audience}. " + " ".join(questions) + " " + instructions + " Ask: " + " ".join(rejected)
    script = coach.interview_script(audience, problem, action=action)
    grounded = coach.ground_interview_action(action, script)
    assert instructions in grounded
    assert all(question in script and grounded.count(question) == 1 for question in questions)
    assert all(question not in script and question not in grounded for question in rejected)
    assert coach._INTERVIEW_FALLBACK[0] in grounded


def test_interview_classification_weights_grounded_actions_and_flags_bad_questions(domain, venture):
    from pathlib import Path
    from app.experiments import coach

    assert Path(coach.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
    audience, problem = domain
    report = (f"I interviewed {audience} about {problem}. They loved the idea. "
              "They spent three hours on a workaround last week. They paid us a $20 deposit. "
              "They booked a demo for Tuesday. They introduced me to their manager. "
              "I asked: don't you think it is great? I asked: would you pay if it existed?")
    evidence = coach.classify_interview(report)
    assert evidence["compliments"] == ["They loved the idea"]
    assert evidence["facts"] == ["They spent three hours on a workaround last week"]
    assert {item["resource"] for item in evidence["commitments"]} == {"money", "time", "reputation"}
    assert evidence["leading_questions"] == ["I asked: don't you think it is great"]
    assert evidence["hypothetical_questions"] == ["I asked: would you pay if it existed"]
    assert evidence["weight"] == 2
    for clause, weight in [("They loved the idea", 0), ("They paid for a workaround last month", 1),
                           ("They paid us a deposit", 2), ("They would pay a deposit", 0),
                           ("They did not pay a deposit", 1), ("I paid a deposit", 0),
                           ("They will pay us a deposit", 0), ("They'll pay us a deposit", 0),
                           ("They won't pay a deposit", 0),
                           (f"I interviewed {audience} and paid a deposit", 0)]:
        result = coach.classify_interview(f"I interviewed {audience} about {problem}. {clause}.")
        assert result["weight"] == weight
    note = coach.coaching_note(evidence)
    assert note.count("?") == 1 and "nudged" in note and "guess" in note
    assert "\u2014" not in note and "founder" not in note.lower()
    assert coach.classify_interview(f"I bought a ticket for {problem}") is None


@pytest.mark.parametrize("enabled,detail,weight", [(False, "They loved the idea", None),
                                                 (True, "They loved the idea", 0),
                                                 (True, "They spent two hours on a workaround yesterday", 1),
                                                 (True, "They paid us a deposit", 2)])
def test_interview_confirmation_saves_weighted_evidence_only_after_approval(market, monkeypatch, enabled, detail, weight):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.chat import experiments
    from app.credits import policy
    from app.db.supymem_models import Experiment
    from app.experiments import coach, ledger
    from app.state import db

    _client, venture_id, _workspace_id, view, *_rest = market
    audience, problem = view["icp"]["who"], view["icp"]["problem"]
    identifier = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"{audience} need help with {problem}"}])[0]
    report = f"I interviewed {audience} about {problem}. {detail}."
    monkeypatch.setattr(policy, "get_policy", lambda key: {"experiments_autopilot_v1": enabled})

    async def extract_result(*args):
        return {"ran": True, "outcome": "supported", "learning": "You reported a positive response."}

    monkeypatch.setattr(experiments, "_llm_json", extract_result)
    prepared = asyncio.run(experiments.record_prepare(venture_id, report))
    card = prepared["actions"][0]["confirmation"]
    payload = card["resulting_state"]
    assert card["action"] == "experiment.record" and ledger.get(identifier, venture_id)["state"] == "proposed"
    with db()() as session:
        assert "result" not in json.loads(session.get(Experiment, identifier).context)
    assert payload["outcome"] == "inconclusive"
    if enabled:
        assert prepared["body_md"].count("?") == 1
        assert payload["interview_evidence"]["weight"] == weight
        payload["interview_evidence"]["weight"] = 999
    else:
        assert "interview_evidence" not in payload and "Next time" not in prepared["body_md"]
        assert card["consequences"][0] == f"Result: inconclusive. Learning: {payload['learning']}"
        assert payload["learning"].startswith("Your report does not establish an agreed pass or fail.")
    saved = asyncio.run(experiments.record_run(venture_id, payload))
    assert ledger.get(identifier, venture_id)["state"] == "concluded"
    recorded = next(item for item in ledger.evidence_for_venture(venture_id) if item["id"] == identifier)
    assert recorded["outcome"] == "inconclusive"
    if enabled:
        assert recorded["result"]["interview_evidence"] == coach.classify_interview(report)
        assert recorded["result"]["evidence_weight"] == weight
        assert recorded["learning"] == payload["learning"]
        assert coach.coaching_note(recorded["result"]["interview_evidence"]) in recorded["learning"]
        assert "Saved what you reported" in saved["body_md"] and "Next time" in saved["body_md"]
    else:
        assert not recorded["result"] and "Your test is still inconclusive" in saved["body_md"]


@pytest.mark.parametrize("reason,text", [("traffic", "The page test has no traffic"),
                                        ("platform", "The platform rejected my ad test"),
                                        ("connection", "The experiment has a missing connection"),
                                        ("connection", "My customer calls are blocked by a missing connection"),
                                        ("budget", "I haven't interviewed anyone because the experiment budget is exhausted"),
                                        ("budget", "The experiment budget is exhausted")])
def test_blocked_experiment_proposes_one_runnable_safe_twin_without_saving_a_result(market, monkeypatch, reason, text):
    from app.core.local_mode import is_local
    from app.chat import experiments
    from app.credits import policy
    from app.experiments import autonomous, coach, ledger

    _client, venture_id, _workspace_id, view, *_rest = market
    hypothesis = f"{view['icp']['who']} need help with {view['icp']['problem']}"
    identifier = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": hypothesis}])[0]
    monkeypatch.setattr(policy, "get_policy", lambda key: {"experiments_autopilot_v1": True})
    assert coach.blocked_reason(text) == reason

    async def unexpected_extract(*args):
        raise AssertionError("A blocker is not a completed experiment")

    monkeypatch.setattr(experiments, "extract", unexpected_extract)
    reply = asyncio.run(experiments.record_prepare(venture_id, text))
    assert len(reply["actions"]) == 1 and "another way" in reply["body_md"]
    assert "not disproven" in reply["body_md"]
    action = reply["actions"][0]
    if reason == "budget" and not is_local():
        assert action["action"] == "experiment.run_review" and "spending anything" in reply["body_md"]
        assert action["confirmation"]["resulting_state"]["run"]
        assert "Nothing is sent or published" in " ".join(action["confirmation"]["consequences"])
    else:
        assert action["action"] == "experiment.prepare"
        chosen = next(item for item in autonomous.available(venture_id) if item["id"] == action["payload"]["experiment_id"])
        assert chosen["runner"] == "scan" and chosen["state"] == "proposed"
        if reason == "budget":
            assert chosen["ceiling_micro"] == 0
        prepared = autonomous.prepare(venture_id, chosen["id"])
        if is_local():
            run, = prepared["actions"]
            assert run["action"] == "experiment.run" and "confirmation" not in run
            assert run["payload"]["fingerprint"] == chosen["fingerprint"]
            assert run["payload"]["ceiling_micro"] == chosen["ceiling_micro"]
        else:
            assert prepared["actions"][0]["confirmation"]["action"] == "experiment.run"
    assert ledger.get(identifier, venture_id)["state"] == "proposed"


def test_unavailable_scans_fall_back_to_existing_private_preparation(market, monkeypatch):
    from app.chat import experiments
    from app.core.local_mode import is_local
    from app.credits import policy
    from app.experiments import autonomous, ledger

    _client, venture_id, _workspace_id, view, *_rest = market
    monkeypatch.setattr(policy, "get_policy", lambda key: {"experiments_autopilot_v1": True})
    monkeypatch.setattr(autonomous, "available", lambda identifier: [])
    hypothesis = f"{view['icp']['who']} need help with {view['icp']['problem']}"
    identifier = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": hypothesis}])[0]
    reply = asyncio.run(experiments.record_prepare(venture_id, "The test has no traffic"))
    action = reply["actions"][0]
    assert len(reply["actions"]) == 1 and action["action"] == "experiment.run_review"
    if is_local():
        assert "confirmation" not in action
        payload = action["payload"]
    else:
        payload = action["confirmation"]["resulting_state"]
    prepared = experiments.review_test_run(venture_id, payload)
    assert "Interview script:" in prepared["body_md"] and "Nothing was sent, published or built." in prepared["body_md"]
    assert not market[6]
    assert ledger.get(identifier, venture_id)["state"] == "proposed"


@pytest.mark.parametrize("ceiling_micro", [0, 1])
def test_local_coach_uses_your_scan_without_a_hosted_wallet_or_confirmation(market, monkeypatch, ceiling_micro):
    from app.core.providers_local import save_settings
    from app.credits import accounts
    from app.experiments import autonomous, coach

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
        models={"chat": "local-test-model"})
    venture_id = market[1]
    selected = next(item for item in autonomous.available(venture_id) if item.get("runner") == "scan")
    selected["ceiling_micro"] = ceiling_micro
    monkeypatch.setattr(autonomous, "available", lambda _venture: [selected])

    def hosted_wallet(_venture):
        raise AssertionError("Your local experiment must not depend on a hosted wallet")

    monkeypatch.setattr(accounts, "get_account", hosted_wallet)
    alternative = coach.blocked_alternative(venture_id, "traffic", selected["hypothesis"], None)
    action = alternative["action"]
    assert action["action"] == "experiment.prepare" and action["payload"]["experiment_id"] == selected["id"]
    assert "review the run" not in alternative["body_md"]
    assert "confirmation" not in action


def test_coaching_write_failure_reports_that_the_result_already_saved(market, monkeypatch):
    from core.clock import utcnow
    from app.chat import experiments
    from app.credits import policy
    from app.experiments import coach, ledger
    from sqlalchemy.exc import OperationalError

    _client, venture_id, _workspace_id, view, *_rest = market
    identifier = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": view["icp"]["problem"]}])[0]
    monkeypatch.setattr(policy, "get_policy", lambda key: {"experiments_autopilot_v1": True})

    def fail_write(*args):
        raise OperationalError("write", {}, Exception("unavailable"))

    monkeypatch.setattr(coach, "save_evidence", fail_write)
    report = f"I interviewed {view['icp']['who']}. They loved the idea."
    reply = asyncio.run(experiments.record_run(venture_id, {"experiment_id": identifier,
        "result_text": report, "outcome": "supported", "ran": True, "sample_size": 5,
        "provenance": {"source": "Your interview notes", "method": "Customer interviews",
                       "observed_at": utcnow().date().isoformat()}}))
    assert "result was saved" in reply["body_md"] and "could not save your call notes" in reply["body_md"]
    assert ledger.get(identifier, venture_id)["state"] == "concluded"


def test_manual_alternatives_respect_local_owner_and_hosted_flag(market, monkeypatch):
    from app.chat import experiments
    from app.credits import policy
    from app.experiments import coach

    _client, venture_id, _workspace_id, view, *_rest = market
    monkeypatch.setattr(policy, "get_policy", lambda key: {})
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        update_settings("capabilities", {"run_for_me_experiments": False}, {})

    calls = []
    blocked_alternative = coach.blocked_alternative

    def alternative(*args):
        if not is_local():
            raise AssertionError("Feature is off")
        calls.append(args)
        return blocked_alternative(*args)

    monkeypatch.setattr(coach, "blocked_alternative", alternative)
    monkeypatch.setattr("app.core.jobs.enqueue", lambda *args, **kwargs: pytest.fail("An unrequested test started"))

    async def not_run(*args):
        return {"ran": False}

    monkeypatch.setattr(experiments, "extract", not_run)
    reply = asyncio.run(experiments.record_prepare(venture_id, "The experiment budget is exhausted"))
    if is_local():
        assert len(calls) == 1 and calls[0][1] == "budget"
        assert "blocked, not disproven" in reply["body_md"] and "another way" in reply["body_md"]
        assert reply["actions"]
    else:
        assert "has not been run yet" in reply["body_md"] and "another way" not in reply["body_md"]
    assert coach.classify_interview(f"The page for {view['icp']['who']} has signups") is None


def test_customer_budget_observation_is_saved_as_a_call_not_treated_as_a_blocker(market, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.chat import experiments
    from app.credits import policy
    from app.experiments import coach, ledger

    _client, venture_id, _workspace_id, view, *_rest = market
    identifier = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": view["icp"]["problem"]}])[0]
    monkeypatch.setattr(policy, "get_policy", lambda key: {"experiments_autopilot_v1": True})

    async def extracted(*args):
        return {"ran": True, "outcome": "refuted", "learning": "You heard they have no budget."}

    def unexpected_alternative(*args):
        raise AssertionError("A customer's situation is not an experiment blocker")

    monkeypatch.setattr(experiments, "_llm_json", extracted)
    monkeypatch.setattr(coach, "blocked_alternative", unexpected_alternative)
    report = f"I interviewed {view['icp']['who']}. They said their budget is exhausted."
    reply = asyncio.run(experiments.record_prepare(venture_id, report))
    assert reply["actions"][0]["confirmation"]["action"] == "experiment.record"
    assert reply["actions"][0]["confirmation"]["resulting_state"]["outcome"] == "inconclusive"
    assert ledger.get(identifier, venture_id)["state"] == "proposed"
