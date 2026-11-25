"""Experiment ledger: idempotent creation, results, learning loop, next step, chat intents."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_round3_matrix import venture as venture_fixture

venture = venture_fixture


@pytest.fixture(autouse=True)
def hosted_experiments(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


load_company_os_app()

TESTS = [{"hypothesis": "Volunteers will sign up for a shared calendar",
          "action": "Post in three local groups", "success_measure": "5 signups in a week"},
         {"hypothesis": "Coordinators will pay for reminders",
          "action": "Interview 5 coordinators", "success_measure": "2 say they would pay"}]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(params=["elevator inspection firms", "amateur kite flyers"])
def audit_reports(venture, monkeypatch, request):
    from app.chat import experiments
    from app.experiments import ledger

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ledger, "autopilot_enabled", lambda: False)
    audience = request.param
    vid = venture[1]
    first, second = ledger.create_from_tests(vid, "chat", [
        {"hypothesis": f"A prior question about whether {audience} will try your new service with a different " + ending,
         "success_measure": "Pass if two agree to a trial."} for ending in ("schedule", "price")])

    async def extract(_vid, text, _hypothesis, _action, measure):
        assert measure == "Pass if two agree to a trial."
        return {"ran": True, "complete": True, "outcome": "supported", "learning": "Two people agreed to try it.",
            "sample_size": 3, "provenance": {"source": "My notes", "method": "Interviews",
                **({"observed_at": "2020-05-06"} if "2020-05-06" in text else {})}}

    monkeypatch.setattr(experiments, "extract", extract)
    return vid, audience, first, second


def test_card_report_targets_only_its_own_test(audit_reports, monkeypatch):
    from app.chat import experiments, router
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, recruitment
    from app.state import db

    vid, audience, first, second = audit_reports
    original = ledger.start(first, vid)
    monkeypatch.setattr(recruitment, "selected", lambda *_: original)
    observation = f"I interviewed three {audience}. Two agreed to a trial on 2020-05-06."
    assert "Open experiments" in str(run(experiments.record_prepare(vid, observation))["actions"])
    assert not ledger.get(first, vid)["result_text"] and not ledger.get(second, vid)["result_text"]
    run(router.handle_message(vid, f"Test results: {second}\nWhat happened: {observation}"))
    assert ledger.get(second, vid)["outcome"] == "supported"
    assert ledger.get(first, vid) == original
    with db()() as session:
        session.get(Experiment, first).state = "aborted"
        session.commit()
    reply = run(experiments.record_prepare(vid, f"Test results: {first}\nWhat happened: {observation}"))
    assert "Open Experiments" in reply["body_md"] and not ledger.get(first, vid)["result_text"]


def test_card_report_can_add_missing_evidence_to_same_row(audit_reports, monkeypatch):
    from app.chat import experiments
    from app.experiments import ledger, recruitment

    vid, audience, first, _second = audit_reports
    monkeypatch.setattr(recruitment, "selected", lambda *_: ledger.get(first, vid))
    report = f"I interviewed three {audience}. Two agreed to try it."
    run(experiments.record_prepare(vid, f"Test results: {first}\nWhat happened: {report}"))
    saved = ledger.get(first, vid)
    assert saved["state"] == "running" and saved["needs_evidence"] and saved["result_text"] == report
    async def date_only(_vid, text, _hypothesis, _action, measure):
        assert report in text and measure == saved["measure"]
        return {"ran": True, "complete": True, "outcome": "supported", "learning": "Two agreed to try it.",
                "provenance": {"observed_at": "2020-05-06"}}
    monkeypatch.setattr(experiments, "extract", date_only)
    run(experiments.record_prepare(vid, f"Test results: {first}\nWhat happened: I observed this on 2020-05-06."))
    saved = ledger.get(first, vid)
    assert len(ledger.list_for_venture(vid)) == 2 and report in saved["result_text"]
    assert saved["state"] == "concluded" and saved["outcome"] == "supported"
    assert saved["sample_size"] == 3 and saved["provenance"]["source"] == "My notes"
    assert saved["evidence_type"] == "founder_reported"


def test_untouched_card_report_does_not_write_or_infer(audit_reports, monkeypatch):
    from app.chat import experiments, router
    from app.experiments import ledger

    vid, _audience, first, _second = audit_reports
    async def no_extract(*_args):
        pytest.fail("An empty report must not reach inference")
    monkeypatch.setattr(experiments, "extract", no_extract)
    before = ledger.list_for_venture(vid)
    legacy = f"I ran the test: {before[0]['hypothesis'][:80]}. What happened: "
    for prompt in (legacy, f"Test results: {first}\nWhat happened: ", f"Test results: {first}\nWhat happened:   \n "):
        reply = run(router.handle_message(vid, prompt))
        assert "what happened" in reply["body_md"].lower()
    assert ledger.list_for_venture(vid) == before


@pytest.mark.parametrize("audience", ["cold-chain dispatchers", "home origami learners"])
def test_learning_review_keeps_finished_results_with_open_notes(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ledger, "autopilot_enabled", lambda: False)
    monkeypatch.setattr(experiments, "_llm_json", unavailable_json)
    vid = venture[1]
    *finished, pending = ledger.create_from_tests(vid, "chat", [
        {"hypothesis": f"{audience} will try offer {index}"} for index in range(4)])
    for identifier in finished:
        ledger.record_result(identifier, vid, True, "I interviewed three people. All declined.", "refuted", "All declined.",
            sample_size=3, provenance={"source": "My notes", "method": "Interviews", "observed_at": "2020-05-06"})
    ledger.record_result(pending, vid, True, "I interviewed one person.", "inconclusive", "One more interview remains.", complete=False)
    state = ledger.loop_state(vid)
    assert state["ready"]
    reply = run(experiments.review_reply(vid, state))
    assert "One more interview remains" in reply["body_md"] and "All declined" in reply["body_md"]
    assert any(action.get("action") == "experiment.revise" for action in reply["actions"])
    assert ledger.loop_state(vid)["direction"] == state["direction"]


async def unavailable_json(*_args, **_kwargs):
    return None


@pytest.mark.parametrize("audience", ["dental equipment distributors", "weekend stargazers"])
def test_stale_research_suggestion_cannot_start_after_pivot(venture, monkeypatch, audience):
    import json
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.api import experiments_api
    from app.db.supymem_models import ExecutionPlan, IdeaSession
    from app.experiments import ledger
    from app.state import db
    from core.clock import utcnow

    _, vid, ws = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    test = {"hypothesis": f"{audience} need a different reminder", "action": f"Interview five {audience}."}
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == vid))
        if idea is None:
            idea = IdeaSession(id="idea_suggestion", workspace_id=ws, venture_id=vid, case="new", state="confirmed",
                created_at=utcnow(), updated_at=utcnow())
            session.add(idea)
        idea.current_revision_id = "rev_before_pivot"
        session.add(ExecutionPlan(id="plan_old", workspace_id=ws, venture_id=vid, revision_id=idea.current_revision_id,
            version=1, created_at=utcnow(), structured=json.dumps({"_runtime": {"context": {"research_review": {"proposed_tests": [test]}}}})))
        session.commit()
        shown, = ledger.suggested_tests(vid)
        idea.current_revision_id = "rev_after_pivot"
        session.commit()
    with pytest.raises(HTTPException) as rejected:
        experiments_api.start_proposed_experiment(vid, experiments_api.ProposedTest.model_validate(shown))
    assert rejected.value.status_code == 422 and "Experiments" in rejected.value.detail
    assert not ledger.suggested_tests(vid) and not ledger.list_for_venture(vid)


@pytest.mark.parametrize("report", [
    "Three of five distributors agreed to a call; two asked for pricing.",
    "Two of four gardeners asked for a trial; the others requested more time.",
])
def test_reported_commitments_are_saved_as_inconclusive_without_inventing_evidence(report):
    from app.chat import experiments

    result = experiments._heuristic("Results: " + report)
    assert result["ran"] is True and result["outcome"] == "inconclusive"
    assert report in result["learning"]
    assert experiments._heuristic("I have not run the test. " + report)["ran"] is False


@pytest.mark.parametrize("venture,audience", [
    ("Shift handovers for hotel operations", "hotel duty managers"),
    ("Activity journals for birdwatchers", "recreational birdwatchers"),
], indirect=["venture"])
def test_past_work_narratives_reach_model_without_action_word_matching(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture
    message = (f"Yesterday's five discussions with {audience} are in my notebook. "
        "Three participants recounted recurring frustrations. Two requested another look at the sketch. "
        "I've closed this round. The full schedule was not finished. Tomorrow I will refine the sketch.")
    model_calls = []

    async def extracted_report(system, user, venture_id):
        model_calls.append(user)
        return {"ran": True, "complete": True, "outcome": "inconclusive",
            "learning": "You heard requests to see the sketch again, not purchase commitments."}

    monkeypatch.setattr(experiments, "_llm_json", extracted_report)
    hypothesis = "You can reduce recurring frustrations"
    extracted = run(experiments.extract(venture_id, message, hypothesis))
    assert extracted["ran"] is True
    assert len(model_calls) == 1 and message in model_calls[0]
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": hypothesis}])
    reply = run(experiments.record_run(venture_id, {**extracted,
        "experiment_id": identifier, "result_text": message}))
    saved = ledger.get(identifier, venture_id)
    assert saved["state"] == "concluded" and saved["outcome"] == "inconclusive", reply["body_md"]
    assert saved["result_text"] == message and saved["learning"] == extracted["learning"]
    assert "Your test is still inconclusive." in reply["body_md"]

    for pending in (f"We will gather five accounts from {audience} tomorrow.",
            f"We finished planning five discussions with {audience}.",
            f"We have not conducted any interviews with {audience}.",
            message + " Actually, we have not run the experiment."):
        assert run(experiments.extract(venture_id, pending, hypothesis))["ran"] is False
    assert len(model_calls) == 1
    assert len(ledger.list_for_venture(venture_id)) == 1

    async def rejected_report(*args):
        return {"ran": False, "outcome": "inconclusive", "learning": "You need to report your own work."}

    monkeypatch.setattr(experiments, "_llm_json", rejected_report)
    assert run(experiments.extract(venture_id, message, hypothesis))["ran"] is False
    assert len(ledger.list_for_venture(venture_id)) == 1

    async def partial_report(*args):
        return {"ran": True, "complete": False, "outcome": "inconclusive",
            "learning": "You have more conversations to finish."}

    monkeypatch.setattr(experiments, "_llm_json", partial_report)
    partial = message.replace("I've closed this round. The full schedule was not finished.", "Three conversations remain.")
    extracted = run(experiments.extract(venture_id, partial, hypothesis))
    assert extracted["ran"] is True and extracted["complete"] is False
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "You can improve the next sketch"}])
    run(experiments.record_run(venture_id, {**extracted, "experiment_id": identifier, "result_text": partial}))
    saved = ledger.get(identifier, venture_id)
    assert saved["state"] == "running" and saved["outcome"] is None
    assert saved["result_text"] == partial

    async def unavailable(*args):
        return None

    monkeypatch.setattr(experiments, "_llm_json", unavailable)
    assert run(experiments.extract(venture_id, message, hypothesis))["ran"] is False


@pytest.mark.parametrize("venture,audience", [
    ("Service schedules for commercial cleaning teams", "cleaning supervisors"),
    ("Skill-sharing meetups for amateur musicians", "amateur musicians"),
], indirect=["venture"])
def test_completed_quantified_interviews_are_not_mistaken_for_preparation(venture, audience):
    from app.experiments import ledger

    for text in (f"I finished every planned interview with {audience} today.",
            f"I completed all the scheduled interviews with {audience} today.",
            f"I finished my five customer interviews with {audience} today."):
        assert ledger.reports_performed_test(text)
    for text in (f"I finished planning interviews with {audience}.",
            f"I have only scheduled five interviews with {audience}.",
            f"We have not conducted any interviews with {audience}."):
        assert not ledger.reports_performed_test(text)


@pytest.mark.parametrize("venture,audience", [
    ("Inventory reconciliation for small wholesalers", "wholesale buyers"),
    ("Language practice for exchange students", "language learners"),
], indirect=["venture"])
def test_completed_interviews_with_descriptive_modifiers_reach_extraction(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture
    message = (f"Save my notes: We have finished our six scheduled detailed one-to-one interviews with {audience}. "
        "Several participants requested a walkthrough. No purchases occurred.")
    model_calls = []

    async def extracted_report(system, user, venture_id):
        model_calls.append(user)
        return {"ran": True, "complete": True, "outcome": "inconclusive",
            "learning": "You heard requests for a walkthrough, not purchases."}

    monkeypatch.setattr(experiments, "_llm_json", extracted_report)
    hypothesis = "You can make this recurring task easier"
    extracted = run(experiments.extract(venture_id, message, hypothesis))
    assert extracted["ran"] is True
    assert len(model_calls) == 1 and message in model_calls[0]
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": hypothesis}])
    saved = ledger.record_result(identifier, venture_id, extracted["ran"], message,
        extracted["outcome"], extracted["learning"])
    assert saved["state"] == "concluded" and saved["outcome"] == "inconclusive"
    assert saved["result_text"] == message and saved["learning"] == extracted["learning"]

    for pending in (f"We will finish our six scheduled detailed one-to-one interviews with {audience} tomorrow.",
            f"We have not finished our six scheduled detailed one-to-one interviews with {audience}.",
            f"We finished planning six interviews with {audience}.",
            f"We completed scheduling six interviews with {audience}.",
            message + " Actually, we did not conduct any interviews."):
        assert run(experiments.extract(venture_id, pending, hypothesis))["ran"] is False
    assert len(model_calls) == 1
    assert len(ledger.list_for_venture(venture_id)) == 1

    partial = (f"We completed two scheduled detailed one-to-one interviews with {audience}. "
        "Four interviews remaining.")
    extracted = run(experiments.extract(venture_id, partial, hypothesis))
    assert extracted["ran"] is True and extracted["complete"] is False
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "You can improve the follow-up"}])
    saved = ledger.record_result(identifier, venture_id, extracted["ran"], partial,
        extracted["outcome"], extracted["learning"], complete=extracted["complete"])
    assert saved["state"] == "running" and saved["outcome"] is None


@pytest.mark.parametrize("venture,audience", [
    ("Compliance training for laboratory teams", "laboratory managers"),
    ("Local cooking workshops for home cooks", "home cooks"),
], indirect=["venture"])
@pytest.mark.parametrize("outcome,verdict", [
    ("supported", "passed"), ("refuted", "did not pass"), ("inconclusive", "is still inconclusive"),
])
def test_saved_verdict_uses_plain_language_and_gives_you_a_next_step(venture, audience, outcome, verdict):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"{audience} will try your service"}])
    reply = run(experiments.record_run(venture_id, {"experiment_id": identifier, "ran": True,
        "result_text": f"I interviewed five {audience} and recorded their decisions.",
        "outcome": outcome, "learning": "You reported their decisions."}))
    assert f"Your test {verdict}." in reply["body_md"]
    assert "Saved as supported" not in reply["body_md"] and "Saved as refuted" not in reply["body_md"]
    assert "You reported their decisions." in reply["body_md"] and reply["actions"]


@pytest.mark.parametrize("venture,audience", [
    ("Inspection records for warehouse teams", "warehouse supervisors"),
    ("Weekend lessons for pottery hobbyists", "pottery learners"),
], indirect=["venture"])
def test_local_next_step_uses_saved_owner_reported_progress(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    identifier, = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": f"{audience} will try your service", "action": f"Interview five {audience}",
        "success_measure": "Three of five agree to a follow-up."}])
    report = f"I interviewed two {audience}; one agreed to a follow-up. Three interviews remain."
    learning = "One person agreed to a follow-up; three interviews remain."
    ledger.record_result(identifier, venture_id, True, report, "inconclusive", learning, complete=False)
    reply = run(experiments.review_reply(venture_id))
    assert learning in reply["body_md"] and "reported by you" in reply["body_md"]
    assert "finish" in reply["body_md"].lower() and "not finished" in reply["body_md"]
    assert "No experiment has a result" not in reply["body_md"]
    assert ledger.get(identifier, venture_id)["state"] == "running"


@pytest.mark.parametrize("venture,audience", [
    ("Reservation schedules for sports clubs", "club coordinators"),
    ("Learning journals for knitters", "hobby knitters"),
], indirect=["venture"])
def test_local_report_and_result_review_bypass_model_rerouting(venture, monkeypatch, audience):
    from app.chat import assistant, experiments, llm_router, router
    from app.experiments import ledger

    _, venture_id, _ = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    identifier, = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": f"{audience} will try your service", "action": f"Interview five {audience}",
        "success_measure": "Three of five agree to a follow-up."}])
    rerouted = []

    async def wrong_route(_venture, text, *_args, **_kwargs):
        rerouted.append(text)
        return llm_router.Decision([("assistant", text)])

    async def commentary(*_args, **_kwargs):
        return experiments._reply("Your supplied observations are not verified. Review your test.")

    async def reported(*_args, **_kwargs):
        return {"ran": False, "complete": False, "outcome": "inconclusive", "learning": "Three interviews remain."}

    monkeypatch.setattr(llm_router, "route", wrong_route)
    monkeypatch.setattr(assistant, "answer_question", commentary)
    monkeypatch.setattr(experiments, "_llm_json", reported)
    report = (f"Results: For this private exercise, I interviewed two {audience}; one agreed to review a sketch. "
        "These are supplied observations, not independently verified evidence. Do not send emails or publish anything.")
    run(router.handle_message(venture_id, report))
    saved = ledger.get(identifier, venture_id)
    assert saved["result_text"] == report and saved["evidence_type"] == "founder_reported"
    assert saved["state"] == "running" and saved["outcome"] is None
    reply = run(router.handle_message(venture_id,
        "What should I do next based on my saved experiment results? Do not send emails or publish anything."))
    assert "Three interviews remain." in reply["body_md"] and "finish" in reply["body_md"].lower()
    assert not rerouted
    withheld = [report + " " + refusal for refusal in ("Do not record these observations.",
        "Don't save these observations.", "Don’t store these observations.",
        "Don't email anyone or save these observations.")]
    for message in withheld:
        run(router.handle_message(venture_id, message))
        assert ledger.get(identifier, venture_id)["result_text"] == report
    assert rerouted == withheld


@pytest.mark.parametrize("venture,audience", [
    ("Inspection records for warehouse teams", "warehouse supervisors"),
    ("Weekend lessons for pottery hobbyists", "pottery learners"),
], indirect=["venture"])
@pytest.mark.parametrize("coaching", [False, True])
def test_partial_interviews_stay_open_for_the_final_report(venture, monkeypatch, audience, coaching):
    import json

    from app.chat import experiments
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    _, venture_id, _ = venture

    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(experiments, "_llm_json", unavailable)
    monkeypatch.setattr(ledger, "autopilot_enabled", lambda: coaching)
    for verb in ("finished", "completed"):
        pending = f"I talked to two {audience}; one agreed. I have not {verb} the test yet. I have three interviews left."
        assert ledger.reports_performed_test(pending)
        assert ledger.reports_partial_test(pending)
    identifier, = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": f"{audience} will try your service", "action": f"Interview five {audience}",
        "success_measure": "Pass if three agree to try it. Fail if all five decline."}])
    message = f"I talked to two {audience}; one agreed. I have not finished the test yet. I have three interviews left."
    extracted = run(experiments.extract(venture_id, message, "People will try your service"))
    reply = run(experiments.record_run(venture_id, {**extracted, "experiment_id": identifier, "result_text": message}))
    saved = ledger.get(identifier, venture_id)
    assert saved["state"] == "running" and saved["outcome"] is None
    assert message == saved["result_text"]
    assert "remaining" in reply["body_md"].lower() and "finish" in reply["body_md"].lower()
    assert "change direction" not in reply["body_md"].lower()

    async def partial(system, user, venture_id):
        return {"ran": True, "complete": False, "outcome": "inconclusive", "learning": "Three interviews remain."}

    monkeypatch.setattr(experiments, "_llm_json", partial)
    progress = f"I interviewed two {audience}. One agreed to try it."
    extracted = run(experiments.extract(venture_id, progress, "People will try your service",
        action=f"Interview five {audience}", success_measure="Three of five agree to try it."))
    assert extracted["complete"] is False
    prepared = run(experiments.record_prepare(venture_id, progress))
    payload = prepared["actions"][0]["confirmation"]["resulting_state"]
    assert payload["complete"] is False and payload["experiment_id"] == identifier
    reply = run(experiments.record_run(venture_id, payload))
    assert ledger.get(identifier, venture_id)["state"] == "running"
    final = f"I finished all five interviews with {audience}. Three agreed to try it."
    saved = ledger.record_result(identifier, venture_id, True, final, "supported", "Three agreed.")
    assert saved["state"] == "concluded" and saved["result_text"] == final
    with db()() as session:
        context = json.loads(session.get(Experiment, identifier).context)
    assert context["previous_reports"][0]["result_text"] == message
    stopped, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"{audience} will book your service"}])
    report = f"I talked to two {audience}. I ended the test early, with three interviews remaining."
    saved = ledger.record_result(stopped, venture_id, True, report, "inconclusive", "You ended the test early.")
    assert saved["state"] == "concluded" and saved["outcome"] == "inconclusive"


@pytest.mark.parametrize("venture,audience", [
    ("Shift planning for commercial bakeries", "commercial bakery owners"),
    ("Observing sessions for astronomy hobbyists", "amateur stargazers"),
], indirect=["venture"])
@pytest.mark.parametrize("outcome", ["supported", "refuted", "inconclusive"])
def test_explicitly_stopped_undersized_test_closes_without_a_verdict(venture, monkeypatch, audience, outcome):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture
    identifier, = ledger.create_from_tests(venture_id, "chat", [{
        "hypothesis": f"{audience} will try your service",
        "action": f"Interview seven {audience}.",
        "success_measure": "Pass if at least four of seven accept a trial; fail if all seven decline; otherwise inconclusive."}])
    plan = ledger.get(identifier, venture_id)
    report = f"I spoke with three {audience}. My notebook contains today's answers."
    complete = False

    async def extracted_report(*args):
        return {"ran": True, "complete": complete, "sample_size": 3, "planned_sample_size": 7,
            "outcome": outcome, "criteria_met": outcome == "supported", "criteria_failed": outcome == "refuted",
            "learning": "Your answers cover fewer people than the agreed sample."}

    monkeypatch.setattr(experiments, "_llm_json", extracted_report)
    for complete in (False, True):
        message = report + (" I stopped our experiment with four interviews left." if complete
            else " Four interviews left; I am still interviewing.")
        extracted = run(experiments.extract(venture_id, message, plan["hypothesis"], plan["action"], plan["measure"]))
        assert extracted["ran"] is True and extracted["complete"] is complete
        assert extracted["outcome"] == "inconclusive" and extracted["sample_size"] == 3
        reply = run(experiments.record_run(venture_id, {**extracted, "experiment_id": identifier, "result_text": message}))
        saved = ledger.get(identifier, venture_id)
        assert saved["state"] == ("concluded" if complete else "running")
        assert saved["outcome"] == ("inconclusive" if complete else None)
        assert saved["result_text"] == message
        if complete:
            assert "still running" not in reply["body_md"].lower()
            assert "not finished" not in reply["body_md"].lower()
            assert "remain" not in reply["body_md"].lower()
            assert "next" in reply["body_md"].lower()
            assert not ledger.loop_state(venture_id)["open"]
        else:
            assert "still running" in reply["body_md"].lower()
            assert "4 remain" in reply["body_md"].lower()


@pytest.mark.parametrize("venture,audience", [
    ("Maintenance notes for ferry operators", "ferry maintenance teams"),
    ("Meal planning for trail runners", "weekend trail runners"),
], indirect=["venture"])
def test_report_instructions_use_words_you_can_answer(venture, monkeypatch, audience):
    from app.experiments import ledger

    _, venture_id, _ = venture
    monkeypatch.setattr(ledger, "quality_enabled", lambda: True)
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"You can help {audience}."}])
    instructions = " ".join(ledger.get(identifier, venture_id)["how_to_run"])
    assert "sample_size" not in instructions and "observed_at" not in instructions
    assert all(word in instructions.lower() for word in ("how many", "where", "when"))
    with pytest.raises(ValueError) as error:
        ledger.record_result(identifier, venture_id, True, f"I interviewed five {audience}.", "inconclusive", "You need more evidence.")
    assert "observed_at" not in str(error.value) and "Tell me" in str(error.value)
    assert ledger.get(identifier, venture_id)["state"] == "proposed"


@pytest.mark.parametrize("venture,audience", [
    ("Equipment logs for theatre technicians", "theatre technicians"),
    ("Trip journals for amateur cyclists", "amateur cyclists"),
], indirect=["venture"])
def test_completed_interviews_survive_unrelated_product_limitations(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture

    async def unavailable(*args):
        return None

    monkeypatch.setattr(experiments, "_llm_json", unavailable)
    message = (f"Record these results: I interviewed six {audience}. Four described recent difficulties. "
        "Nobody paid, no prototype exists, and we have not tested accuracy or reliability.")
    assert ledger.reports_performed_test(message)
    extracted = run(experiments.extract(venture_id, message, "You can reduce recurring difficulties"))
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "You can reduce recurring difficulties"}])
    saved = ledger.record_result(identifier, venture_id, extracted["ran"], message, extracted["outcome"], extracted["learning"])
    assert saved["state"] == "concluded" and saved["outcome"] == "inconclusive"
    assert saved["result_text"] == message
    for denial in ("Actually, I never interviewed anyone.", "We have not run the experiment.",
                   "We have not tested it.", "We have not conducted any interviews.",
                   "We have not run the current experiment."):
        assert not ledger.reports_performed_test(f"{message} {denial}")
    assert ledger.reports_performed_test(message.replace("tested accuracy or reliability", "run a reliability check"))


@pytest.mark.parametrize("venture,audience", [("Invoice approvals for purchasing teams", "purchasing managers"),
    ("Shared garden planning for households", "home gardeners")], indirect=["venture"])
def test_prefixed_completed_interviews_save_without_promoting_reported_facts(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture

    async def unavailable(*args):
        return None

    monkeypatch.setattr(experiments, "_llm_json", unavailable)
    message = (f"Record these results: I interviewed three {audience}. Two reported recurring delays; "
        "one did not. One agreed to a follow-up, one declined and one wanted details. Nobody paid. "
        "These are my notes, not independently verified evidence.")
    assert ledger.reports_performed_test(message)
    extracted = run(experiments.extract(venture_id, message, "You can reduce recurring delays"))
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "You can reduce recurring delays"}])
    saved = ledger.record_result(identifier, venture_id, extracted["ran"], message, extracted["outcome"], extracted["learning"])
    assert saved["state"] == "concluded" and saved["outcome"] == "inconclusive" and saved["result_text"] == message
    for denial in (f"Record these results: I did not interview any {audience}.",
            f"Record these results: I interviewed three {audience}. Actually, I never interviewed anyone."):
        assert not ledger.reports_performed_test(denial)


@pytest.mark.parametrize("venture,audience", [("Invoice approvals for purchasing teams", "purchasing"),
    ("Shared garden planning for households", "gardening")], indirect=["venture"])
def test_exp4_rough_completed_conversations_save_without_rephrasing(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture
    async def unavailable(*args):
        return None
    monkeypatch.setattr(experiments, "_llm_json", unavailable)
    for index, message in enumerate((
        f"Notes are rough. 4 {audience} chats, plus one non-buyer. Nobody paid. One no-show; the count is uncertain.",
        f"spoke with 6 {audience} people I think. Two may be duplicates. Nobody paid or agreed a pilot.",
        f"Messy notes: talked to 5 {audience} people. One was not a buyer. No measured outcomes.",
        f"I interviewed 5 {audience} people. One never called back. No paid commitments.")):
        assert ledger.reports_performed_test(message), message
        prepared = run(experiments.record_prepare(venture_id, message))
        assert "not reported a test" not in prepared["body_md"]
        extracted = run(experiments.extract(venture_id, message, "You can help this audience"))
        identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"You can help this audience {index}"}])
        saved = ledger.record_result(identifier, venture_id, extracted["ran"], message, extracted["outcome"], extracted["learning"])
        assert saved["result_text"] == message and saved["state"] == "concluded"
    for message in (f"We plan to talk to 5 {audience} people.", f"4 {audience} chats planned next week.",
                    f"4 {audience} chats next month.",
                    "Did not speak with anyone.", "Never talked to anyone.", "We haven't conducted the interviews.",
                    "I spoke with five participants. Actually, I didn't speak with anyone.",
                    "I interviewed five participants. Actually, I didn't interview anyone."):
        assert not ledger.reports_performed_test(message), message


@pytest.mark.parametrize("venture,audience", [("Invoice approvals for purchasing teams", "purchasing"),
    ("Shared garden planning for households", "gardening")], indirect=["venture"])
def test_exp4_unavailable_extraction_cannot_turn_uncertainty_into_a_verdict(venture, monkeypatch, audience):
    from app.chat import experiments
    from app.experiments import ledger

    _, venture_id, _ = venture
    async def unavailable(*args):
        return None
    monkeypatch.setattr(experiments, "_llm_json", unavailable)
    for index, observation in enumerate(("Nobody paid; one said maybe, and the count is uncertain.",
                                         "One paid for an unrelated service; others loved the mockup.")):
        message = f"I interviewed five {audience} people. {observation} No success criterion was agreed."
        extracted = run(experiments.extract(venture_id, message, "You have validated demand"))
        assert extracted["ran"] and extracted["outcome"] == "inconclusive"
        identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": f"You have validated demand {index}"}])
        recorded = run(experiments.record_run(venture_id, {"experiment_id": identifier, "ran": True,
            "result_text": message, "outcome": extracted["outcome"], "learning": extracted["learning"]}))
        assert "count as success" in recorded["body_md"]
    assert not ledger.loop_state(venture_id)["ready"]
    reply = run(experiments.review_reply(venture_id))
    assert "supported the idea" not in reply["body_md"] and "did not hold up" not in reply["body_md"]
    assert "unclear" in reply["body_md"].lower() and reply["actions"]


def test_create_from_tests_is_idempotent(venture):
    _client, vid, _ws = venture
    from app.experiments import ledger

    assert len(ledger.create_from_tests(vid, "validation", TESTS)) == 2
    assert ledger.create_from_tests(vid, "validation", TESTS) == []
    assert ledger.create_from_tests(vid, "research", ["Volunteers will sign up for a shared calendar", "  "]) == []
    rows = ledger.list_for_venture(vid)
    assert [r["state"] for r in rows] == ["proposed", "proposed"]
    assert rows[0]["how_to_run"] and rows[0]["measure"] == "5 signups in a week"
    assert ledger.create_from_tests("ven_missing", "validation", TESTS) == []


@pytest.mark.parametrize("domain", ["dental clinics", "public libraries"])
def test_experiment_copy_is_clean_in_storage_and_legacy_views(venture, domain):
    import hashlib
    import json
    from app.chat import experiments
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db
    from app.research.review import SYSTEM
    from app.gtm.relationships.email_compliance import NO_DASH_RULE

    _client, vid, _ws = venture
    hypothesis = f"The founder wants to help {domain}—by reducing missed requests."
    test = {"hypothesis": hypothesis, "action": "Ask privately—do not send automatically.", "success_measure": "2–3 requests"}
    created = ledger.create_from_tests(vid, "validation", [test])
    assert len(created) == 1 and ledger.create_from_tests(vid, "validation", [test]) == []
    with db()() as session:
        row = session.get(Experiment, created[0])
        assert row.hypothesis == f"You want to help {domain}, by reducing missed requests."
        assert row.action == "Ask privately, do not send automatically." and row.metric == "2-3 requests"
        row.hypothesis, row.action, row.metric = hypothesis, test["action"], test["success_measure"]
        context = json.loads(row.context)
        old_key = hashlib.sha256(f"{context['direction']}|{hypothesis.lower()}".encode()).hexdigest()[:24]
        row.context = json.dumps({**context, "key": old_key, "how_to_run": [test["action"]]})
        session.commit()
    served = ledger.list_for_venture(vid)[0]
    assert served["hypothesis"] == f"You want to help {domain}, by reducing missed requests."
    assert served["action"] == served["how_to_run"][0] == "Ask privately, do not send automatically."
    assert served["measure"] == "2-3 requests"
    assert ledger.start_proposed(vid, test)["id"] == created[0]
    assert len(ledger.list_for_venture(vid)) == 1
    started = experiments.review_test_run(vid, {"run": True, **test})
    assert "Prepared your experiment:" in started["body_md"]
    recorded = run(experiments.record_run(vid, {"new_hypothesis": hypothesis, "ran": True,
        "result_text": "No signups—no replies", "outcome": "refuted", "learning": "No pull—try another group"}))
    assert "Your test did not pass." in recorded["body_md"]
    assert ledger.create_from_tests(vid, "validation", [test]) == []
    served = ledger.list_for_venture(vid)[0]
    assert (served["id"], served["state"], served["outcome"]) == (created[0], "concluded", "refuted")
    assert served["result_text"] == "No signups, no replies" and served["learning"] == "No pull, try another group"
    action = "Ask—" * 120 + "Do not send automatically."
    from core.copy import no_dashes
    longer = ledger.create_from_tests(vid, "validation", [{"hypothesis": f"A second test for {domain}", "action": action}])
    assert ledger.get(longer[0], vid)["action"] == no_dashes(action)
    assert NO_DASH_RULE in SYSTEM


def test_record_result_and_start(venture):
    _client, vid, _ws = venture
    from app.experiments import ledger

    a, b = ledger.create_from_tests(vid, "validation", TESTS)
    assert ledger.start(a, vid)["state"] == "running"
    with pytest.raises(ValueError):
        ledger.record_result(a, vid, True, "x", "great", "y")
    out = ledger.record_result(a, vid, True, "nobody signed up", "refuted", "No pull from groups")
    assert (out["state"], out["outcome"], out["learning"]) == ("concluded", "refuted", "No pull from groups")
    with pytest.raises(RuntimeError):
        ledger.record_result(a, vid, True, "again", "supported", "x")
    assert ledger.record_result(b, vid, False, "not yet", "", "")["state"] == "proposed"
    with pytest.raises(KeyError):
        ledger.record_result(a, "ven_other", True, "x", "refuted", "x")
    assert "Experiments:" in ledger.summary_lines(vid)[0]


@pytest.mark.parametrize("venture,answer", [
    ("Purchase approvals for procurement teams",
     "A buyer mentioned three colleagues booked the same supplier visit. No paid interest yet."),
    ("Meal planning for home cooks",
     "A neighbor mentioned two families cooked the same recipe. No paid interest yet."),
], indirect=["venture"])
def test_results_require_a_performed_test(venture, answer, monkeypatch):
    from app.chat import experiments
    from app.credits.policy import wallet_phase
    from app.experiments import ledger

    client, venture_id, _workspace_id = venture
    assert wallet_phase() == "person"
    model_calls = []

    async def invented_result(*args):
        model_calls.append(args)
        return {"ran": True, "outcome": "supported", "learning": "A positive signal."}

    monkeypatch.setattr(experiments, "_llm_json", invented_result)
    unperformed = [answer, "I'm answering your question. I didn't run a test.",
                   "im answering your question i didnt run a test",
                   "We have not performed the experiment.", "I never tried the test.",
                   "The test was not performed.", "I ran the test. Actually, I didn’t run it.",
                   "The test was proposed for next week."]
    for message in unperformed:
        extracted = run(experiments.extract(venture_id, message, "People will use this"))
        assert extracted["ran"] is False, message
        prepared = run(experiments.record_prepare(venture_id, message))
        assert not any(action.get("confirmation") for action in prepared["actions"])
        assert "tell me" in prepared["body_md"].lower()
    assert model_calls == []
    assert ledger.list_for_venture(venture_id) == []
    identifier, = ledger.create_from_tests(venture_id, "chat", [{"hypothesis": "People will use this"}])
    for message in unperformed:
        with pytest.raises(ValueError, match="experiment_not_run"):
            ledger.record_result(identifier, venture_id, True, message, "refuted", "Not supported")
        reply = run(experiments.record_run(venture_id, {"new_hypothesis": "People will pay for this",
            "ran": True, "result_text": message, "outcome": "refuted", "learning": "Not supported"}))
        assert "not saved" in reply["body_md"].lower()
    assert len(ledger.list_for_venture(venture_id)) == 1
    assert ledger.get(identifier, venture_id)["state"] == "proposed"
    assert ledger.get(identifier, venture_id)["ran"] is None
    assert ledger.evidence_for_venture(venture_id)[0]["state"] == "proposed"
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": answer})
    assert response.status_code == 200
    assert ledger.get(identifier, venture_id)["state"] == "proposed"
    performed = ("I interviewed five people. They paid us a deposit.",
                 "I ran the test. Nobody signed up.", "Nobody from the invited group signed up.",
                 "12 signed up", "I sent 20 emails and got 3 replies.", "Got 3 replies from the pilot group.",
                 "Three of the ten people replied.", "We posted the offer and two people bought it.")
    for message in performed:
        assert run(experiments.extract(venture_id, message, "People will use this"))["ran"] is True, message
    assert len(model_calls) == len(performed)
    saved = ledger.record_result(identifier, venture_id, True, "I ran the test. Nobody signed up.",
                                 "refuted", "Try a different invitation.")
    assert (saved["ran"], saved["state"], saved["outcome"]) == (True, "concluded", "refuted")


def test_learning_loop_needs_all_failed_and_never_pivots(venture, monkeypatch):
    client, vid, _ws = venture
    from app.chat import experiments
    from app.experiments import ledger

    a, b = ledger.create_from_tests(vid, "validation", TESTS)
    assert not ledger.loop_state(vid)["ready"]
    reply = run(experiments.record_run(vid, {"experiment_id": a, "ran": True, "outcome": "refuted",
                                             "result_text": "I ran the test. Nobody signed up.", "learning": "No pull."}))
    assert "What we learned: No pull." in reply["body_md"]
    assert "Here's what we've learned" not in reply["body_md"]  # one open test remains
    reply = run(experiments.record_run(vid, {"experiment_id": b, "ran": True, "outcome": "inconclusive",
                                             "result_text": "I interviewed two people. Neither committed.", "learning": "Unclear."}))
    assert not any(action.get("confirmation") for action in reply["actions"])
    reply = run(experiments.review_reply(vid, ledger.loop_state(vid)))
    assert "Here's what we've learned" in reply["body_md"]
    cards = [x["confirmation"] for x in reply["actions"] if x.get("confirmation")]
    assert 1 <= len(cards) <= 3
    assert {c["action"] for c in cards} == {"experiment.revise"}  # only confirmation cards, no direct pivot
    from app.db.supymem_models import IdeaSession
    from app.state import db
    with db()() as s:
        assert s.query(IdeaSession).count() == 0 or all(i.state != "abandoned" for i in s.query(IdeaSession))


def test_supported_result_blocks_pivot(venture):
    _client, vid, _ws = venture
    from app.experiments import ledger

    a, b = ledger.create_from_tests(vid, "validation", TESTS)
    ledger.record_result(a, vid, True, "I ran the test. 12 signed up", "supported", "Real pull")
    ledger.record_result(b, vid, True, "I ran the test. None paid", "refuted", "No payers")
    assert not ledger.loop_state(vid)["ready"]


def test_two_failures_end_direction_with_open_test(venture):
    _client, vid, _ws = venture
    from app.experiments import ledger

    ids = ledger.create_from_tests(vid, "validation", TESTS + [{"hypothesis": "Third idea"}])
    ledger.record_result(ids[0], vid, True, "I ran the test. Nobody replied", "refuted", "l1")
    assert not ledger.loop_state(vid)["ready"]
    ledger.record_result(ids[1], vid, True, "I ran the test. Nobody replied", "refuted", "l2")
    assert ledger.loop_state(vid)["ready"]


@pytest.mark.parametrize("domain", ["warehouse teams", "home gardeners"])
def test_next_step_stages(venture, monkeypatch, domain):
    _client, vid, _ws = venture
    from app.ventures import next_step as ns
    from app.chat import execute
    from app.planning import store
    from app.ventures import state_brief

    monkeypatch.setattr(execute, "research_needs_start", lambda _v: True)
    monkeypatch.setattr(store, "list_current_tasks", lambda _v: [])
    site = {"live": False, "waitlist_enabled": False}
    monkeypatch.setattr(state_brief, "_website_section", lambda *_a: site)
    base = dict(workspace_id="w1", gtm_entry=False, active_jobs=False, idea_state="confirmed", idea_confirmed=True,
                personal=False, plan_state="approved", dossier_state="complete", products=1, accepted_build=True,
                product_attempt=False, accepted_product=False)
    exp = {"id": "exp_1", "hypothesis": f"{domain} sign up", "state": "proposed"}
    assert ns._resolve(vid, "/v/x", {**base, "exp_run": exp})["key"] == "site.publish"
    assert ns._resolve(vid, "/v/x", {**base})["key"] == "site.publish"
    assert ns._resolve(vid, "/v/x", {**base, "exp_review": True, "exp_run": exp})["key"] == "experiment.review"
    site["live"] = True
    step = ns._resolve(vid, "/v/x", {**base, "exp_run": exp})
    assert (step["key"], step["chat_action"], step["title"]) == ("experiment.run", "experiment.start", f"Run: {domain} sign up")
    assert ns._resolve(vid, "/v/x", {**base, "exp_run": {**exp, "state": "running"}})["chat_action"] is None
    assert ns._resolve(vid, "/v/x", {**base})["key"] == "gtm.diagnose"
    monkeypatch.setattr(store, "list_current_tasks", lambda _v: [
        {"id": "t1", "title": f"Interview five {domain}", "source": "approved_plan:1"}])
    assert ns._resolve(vid, "/v/x", {**base, "products": 0, "exp_run": exp})["key"] == "experiment.run"
    assert ns._resolve(vid, "/v/x", {**base, "products": 0})["key"] == "work.next_task"


def test_brief_and_console_endpoint(venture):
    client, vid, _ws = venture
    from app.experiments import ledger
    from app.ventures.state_brief import get_venture_state_brief

    ledger.create_from_tests(vid, "validation", TESTS)
    brief = get_venture_state_brief(vid, force=True)
    assert "Experiments: 2 not run" in brief["_rendered"]
    body = client.get(f"/ventures/{vid}/experiments").json()
    assert len(body["items"]) == 2 and body["review_ready"] is False
    assert client.get("/ventures/ven_nope/experiments").status_code == 404


def test_start_proposed_records_and_runs_once(venture):
    client, vid, _ws = venture
    body = TESTS[0]
    first = client.post(f"/ventures/{vid}/experiments/start", json=body).json()
    again = client.post(f"/ventures/{vid}/experiments/start", json=body).json()
    assert first["state"] == "running" and again["id"] == first["id"]
    assert len(client.get(f"/ventures/{vid}/experiments").json()["items"]) == 1
    assert client.post("/ventures/ven_nope/experiments/start", json=body).status_code == 404


@pytest.mark.parametrize("text,intent", [
    ("I ran the test", "experiment_record"), ("the interviews went badly", "experiment_record"),
    ("nobody signed up", "experiment_record"), ("We finished the survey", "experiment_record"),
    ("what experiments are running", "experiment_list"), ("show my experiments", "experiment_list"),
    ("start the next experiment", "experiment_start"),
    ("what have we learned so far", "experiment_review"),
    ("who signed up on the waitlist?", "assistant"),
])
def test_intents(text, intent):
    from app.chat.intent import classify_intent

    assert classify_intent(text) == intent


def test_chat_routes_and_guard_runs_first(venture, monkeypatch):
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    client, vid, _ws = venture
    from app.experiments import ledger
    from app.chat import experiments, router

    confirm_saved_idea(vid)
    a, _b = ledger.create_from_tests(vid, "validation", TESTS)
    listed = client.post(f"/ventures/{vid}/chat", json={"message": "what experiments are running"}).json()
    assert "Volunteers will sign up" in listed["body_md"]
    text = "the interviews went badly, nobody would pay"
    rec = run(experiments.record_prepare(vid, text))
    cards = [x["confirmation"] for x in rec["actions"] if x.get("confirmation")]
    assert cards and cards[0]["action"] == "experiment.record"
    assert ledger.get(a, vid)["state"] == "proposed"  # nothing saved before confirm
    state = cards[0]["resulting_state"]
    assert state["outcome"] == "inconclusive"
    done = client.post(f"/ventures/{vid}/chat", json={"message": text})
    assert done.status_code == 200, done.text
    assert ledger.get(state["experiment_id"], vid)["outcome"] == "inconclusive"
    started = client.post(f"/ventures/{vid}/chat/confirm",
                          json={"action": "experiment.start", "payload": {"experiment_id": ledger.list_for_venture(vid)[1]["id"]}})
    assert started.status_code == 200
    assert ledger.list_for_venture(vid)[1]["state"] == "running"
    assert "Nothing is sent" in started.json()["body_md"]
    # the adversarial guard runs before the experiment handlers
    def boom(*_a, **_k):
        raise AssertionError("experiment handler ran before the guard")

    monkeypatch.setattr(router, "guard_reply", None, raising=False)
    monkeypatch.setattr("app.chat.guard.guard_reply", lambda *_a, **_k: {"kind": "text", "body_md": "blocked",
                                                                         "actions": [], "citations": []})
    monkeypatch.setattr(experiments, "dispatch", boom)
    out = client.post(f"/ventures/{vid}/chat", json={"message": "I ran the test"}).json()
    assert out["body_md"].endswith("blocked") or "blocked" in out["body_md"]


def test_revise_starts_idea_revision_only_on_confirm(venture, monkeypatch):
    _client, vid, _ws = venture
    from app.chat import experiments
    from app.idea import service

    seen = []
    monkeypatch.setattr(service, "start_direction_correction", lambda *a, **k: seen.append(k) or {"id": "ids_new"})
    monkeypatch.setattr(service, "post_message", lambda sid, text, **k: seen.append(text))
    from app.db.supymem_models import IdeaSession
    from app.state import db
    from core.clock import utcnow
    with db()() as s:
        s.add(IdeaSession(id="ids_1", workspace_id=_ws, venture_id=vid, case="know", state="confirmed",
                          current_revision_id="rev_1", confirmed_revision_id="rev_1",
                          created_at=utcnow(), updated_at=utcnow()))
        s.commit()
    out = run(experiments.revise_run(vid, {"kind": "different_customer", "text": "Church youth groups"}, None))
    assert "Started an idea revision" in out["body_md"] and len(seen) == 2
    bad = run(experiments.revise_run(vid, {"kind": "nonsense", "text": "x"}, None))
    assert "Started" not in bad["body_md"]


@pytest.mark.parametrize("domain", ["warehouse teams", "home gardeners"])
def test_gstack_options_never_keep_bracket_placeholders(domain):
    import json
    from app.gstack.office_hours import parse_picks
    from app.gstack.methodology import upstream_forcing_questions

    ids = list(upstream_forcing_questions())[:2]
    raw = json.dumps({"questions": [
        {"id": ids[0], "question": f"Who in {domain} is asking for this?",
         "options": ["I talked to [Name] and they described it", "None yet"]},
        {"id": ids[1], "question": f"What do {domain} do today?",
         "options": ["A spreadsheet from [Name]", "Nothing"]}]})
    picks = parse_picks(raw)
    assert picks[0]["options"] == ["No evidence yet", "Some evidence to describe"]
    assert picks[1]["options"][0] == "A spreadsheet from an owner"
    assert all("[" not in option and "]" not in option for pick in picks for option in pick["options"])


def test_suggested_tests_from_validation_run_and_dedup(venture):
    import json

    from app.db.supymem_models import IdeaRevision, IdeaSession, ValidationRun
    from app.experiments import ledger
    from app.state import db
    from core.clock import utcnow

    _client, vid, ws = venture
    now = utcnow()
    with db()() as s:
        s.add(IdeaSession(id="ids_x", workspace_id=ws, venture_id=vid, case="new", state="confirmed", created_at=now, updated_at=now))
        s.flush()
        s.add(IdeaRevision(id="rev_x", session_id="ids_x", workspace_id=ws, venture_id=vid, rev=1, source="t",
                           created_at=now))
        s.flush()
        s.get(IdeaSession, "ids_x").current_revision_id = "rev_x"
        s.flush()
        s.add(ValidationRun(id="val_x", session_id="ids_x", revision_id="rev_x", workspace_id=ws, venture_id=vid,
                            method="gstack", state="complete", report=json.dumps({"proposed_tests": TESTS}),
                            created_at=now))
        s.commit()
    got = ledger.suggested_tests(vid)
    assert [g["hypothesis"] for g in got] == [t["hypothesis"] for t in TESTS]
    ledger.start_proposed(vid, TESTS[0])
    assert len(ledger.suggested_tests(vid)) == 1


@pytest.mark.parametrize("domain", ["warehouse teams", "home gardeners"])
def test_gstack_pick_with_overlong_option_is_rejected_without_clipping(domain):
    import json
    from pydantic import ValidationError
    from app.gstack.office_hours import parse_picks, upstream_forcing_questions
    ids = list(upstream_forcing_questions())[:2]
    raw = json.dumps({"questions": [
        {"id": qid, "question": f"Who in {domain} needs this?", "options": ["Short answer one", "word " * 80]} for qid in ids]})
    with pytest.raises(ValidationError) as rejected:
        parse_picks(raw)
    assert all(error["type"] == "string_too_long" and error["ctx"]["max_length"] == 220
               for error in rejected.value.errors())


@pytest.mark.parametrize("venture,audience", [
    ("Maintenance planning for factories", "factory maintenance teams"),
    ("Nature journals for families", "family nature groups"),
], indirect=["venture"])
def test_local_report_saves_requested_result_without_confirmation(venture, monkeypatch, audience):
    from datetime import timedelta

    from app.chat import experiments
    from app.experiments import ledger
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    _, venture_id, _ = venture
    proposal = {"hypothesis": f"You can help {audience}", "action": f"You interview {audience}",
        "success_measure": "You record whether participants want a demonstration"}
    identifier, = ledger.create_from_tests(venture_id, "chat", [proposal])
    observed_at = (utcnow() - timedelta(days=1)).date().isoformat()
    report = (f"We interviewed five {audience} on {observed_at}. Our interview notes: "
        "Two asked for a demonstration; the other three declined.")
    with pytest.raises(ValueError):
        ledger.record_result(identifier, venture_id, True, report, "supported", "You reported interest.",
            sample_size=0, provenance={"source": "Your interview notes", "method": "Interviews", "observed_at": observed_at})
    evidence = {}

    async def extracted(*args, **kwargs):
        return {"ran": True, "complete": True, "outcome": "inconclusive",
            "learning": "You reported interest in a demonstration, not verified demand.", **evidence}

    monkeypatch.setattr(experiments, "_llm_json", extracted)
    incomplete = run(experiments.record_prepare(venture_id,
        f"We interviewed five {audience}. Two asked for a demonstration; the other three declined."))
    reported = ledger.get(identifier, venture_id)
    assert reported["state"] == "running" and reported["outcome"] is None and reported["needs_evidence"]
    assert reported["evidence_type"] == "founder_reported" and reported["evidence_label"] == "reported by you"
    assert not reported.get("sample_size") and reported["provenance"] == {}
    assert "not independently verified" in reported["learning"]
    assert incomplete["kind"] != "confirmation"
    complete_identifier, = ledger.create_from_tests(venture_id, "chat", [proposal], request_id="complete-provenance")
    evidence.update(sample_size=5, provenance={"source": "Your interview notes", "method": "Interviews",
        "observed_at": observed_at})
    reply = run(experiments.record_prepare(venture_id, f"Test results: {complete_identifier}\nWhat happened: {report}"))
    assert reply["kind"] != "confirmation" and not any(action.get("confirmation") for action in reply["actions"])
    saved = ledger.get(complete_identifier, venture_id)
    assert saved["state"] == "concluded" and saved["result_text"] == report
    assert saved["sample_size"] == 5 and saved["provenance"] == evidence["provenance"]
    assert saved["outcome"] == "inconclusive" and "You" in reply["body_md"]
    run(experiments.record_prepare(venture_id, f"We have not run interviews with {audience}."))
    assert ledger.get(complete_identifier, venture_id)["result_text"] == report
    assert ledger.get(identifier, venture_id) == reported


@pytest.mark.parametrize("audience", ["factory maintenance teams", "family nature groups"])
def test_local_result_reports_use_shared_performed_test_validation(monkeypatch, audience):
    from app.chat.intent import _experiment_intent, classify_intent

    report = f"Seven interviews with {audience} completed; four participants requested a demonstration."
    assert _experiment_intent(report) is None
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert classify_intent(report) == "experiment_record"
    for pending in (f"Seven interviews with {audience} are scheduled tomorrow.",
                    f"We have not conducted interviews with {audience}.",
                    f"Have seven interviews with {audience} completed?",
                    f"I received a delivery notice for {audience}."):
        assert classify_intent(pending) != "experiment_record"
