import asyncio
import json
import os

import pytest
from sqlalchemy import select

from tests.integration.test_intake_chat_api import client as base_client, confirm, preview, stub


GYM_IDEA = "something for gym owners idk, members keep leaving"


@pytest.fixture()
def client(base_client, monkeypatch):
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    from app.credits.migration import migrate_wallets
    from app.core.ratelimit import limiter
    from app.state import db
    from app.ventures import founder_facts

    async def extract(*args, **kwargs):
        return {}

    monkeypatch.setattr(founder_facts, "extract", extract)

    limiter.reset()
    with db()() as session:
        assert migrate_wallets(session)["deferred_accounts"] == []
        session.commit()
    return base_client


@pytest.mark.parametrize("audience,task,new_audience,new_offer", [
    ("greenhouse operators",
     "It records irrigation readings and schedules planting.", "Site supervisors", "Internal growing board"),
    ("distance runners",
     "It records training sessions and schedules recovery.", "Local households", "Private exercise planner"),
])
def test_reroute_replaces_entry_answers_without_overwriting_prefill(request, monkeypatch, audience, task, new_audience, new_offer):
    api = request.getfixturevalue("client")

    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.idea.service import open_session
    from app.intake.smart import classify_fallback
    from app.state import db

    text = f"A mobile service for {audience}.\nClarification: {task}"
    stub(monkeypatch, classify_fallback(text).to_dict())
    receipt = confirm(api, preview(api, text)["proposal"])
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        prior = session.get(IdeaRevision, row.current_revision_id)
        prior_id, prior_structured = prior.id, prior.structured
        answers = json.loads(prior_structured)["_answered_fields"]
        assert answers["customer"] == audience
        assert answers["workflow"] == task
        venture = session.get(Venture, receipt["venture_id"])
        session.expunge(venture)

    changed = f"Change path to an internal build for {new_audience.lower()}"
    opened = open_session(venture=venture, intake_text=changed, intake_idea=new_offer,
        prefill_answers={"customer": new_audience, "offer": new_offer})
    structured = opened["current_revision"]["structured"]
    assert structured["customer"] == new_audience
    assert structured["offer"] == new_offer
    assert structured["summary"] == new_offer
    assert structured["_answered_fields"]["customer"] == new_audience
    assert "workflow" not in structured["_answered_fields"]
    with db()() as session:
        assert session.get(IdeaRevision, prior_id).structured == prior_structured


@pytest.mark.parametrize("audience,pain,task", [
    ("quarry surveyors", "Inconsistent elevation measurements are frustrating before blasting.",
     "It compares survey readings and highlights mismatched elevations."),
    ("aquarium keepers", "Inconsistent water readings are frustrating between cleanings.",
     "It tracks water readings and schedules tank cleanings."),
    ("aquarium keepers", "", "It tracks water readings and schedules tank cleanings."),
    ("quarry surveyors", "Inconsistent elevation measurements are frustrating before blasting.", "skip"),
])
def test_entry_answers_survive_creation_without_repeated_questions(request, monkeypatch, audience, pain, task):
    api = request.getfixturevalue("client")

    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.runner import _core_clarification
    from app.intake.smart import classify_fallback
    from app.state import db

    opening = f"FirstLight: A mobile service for {audience}. {pain}".strip()
    stub(monkeypatch, classify_fallback(opening).to_dict())
    first = preview(api, opening)["proposal"]
    assert first["onboarding_field"] == "offer"
    text = f"{opening}\nClarification: {task}"
    stub(monkeypatch, classify_fallback(text).to_dict())
    proposal = preview(api, text)["proposal"]
    assert not proposal.get("onboarding_question")
    receipt = confirm(api, proposal)
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        structured = json.loads(session.get(IdeaRevision, row.current_revision_id).structured)
    answers = structured.get("_answered_fields", {})
    assert answers.get("customer") == audience
    if pain:
        assert answers.get("problem") == pain
    else:
        assert "problem" not in answers
    if task == "skip":
        assert "workflow" not in answers and "offer" not in answers
        assert structured["assumptions"]
        return
    assert answers.get("offer") == task
    assert answers.get("workflow") == task
    snapshot = {"original_idea": text, "project_intent": "startup", "understanding": structured,
                "answered_fields": answers, "question_fields_asked": structured["_onboarding_completed"]}
    for field in ("problem", "workflow"):
        result = _core_clarification(snapshot, Clarification(case="know", action="question",
            message="Choose your next step.", understanding=Understanding.model_validate({
                key: value for key, value in structured.items() if key in Understanding.model_fields}),
            question=Question(field=field, text="What should you focus on next?", why="Choose your next step.", options=[])))
        assert (result.question is not None and result.question.field == field) == (field == "problem" and not pain)


@pytest.mark.parametrize("offer,audience,payment", [
    ("A scanner that tracks equipment returns", "Equipment rental operators", "Nine equipment rental operators have paid for it."),
    ("A watering device that measures soil moisture", "Home gardeners", "We already have twenty-four paying customers."),
])
@pytest.mark.parametrize("live", [False, True])
def test_business_stage_selects_questions_and_accepts_unflagged_text(client, monkeypatch, offer, audience, payment, live):
    from app.credits.policy import wallet_phase
    from app.db.models import Job, Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.agent import messages_for
    from app.idea.runner import post_fallback_question
    from app.idea.service import get_session
    from app.state import db
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued
    from tests.unit.chat.test_founder_intake_progress import _pending_questions

    opening = f"{offer} for {audience}." + (" " + payment if live else "")
    venture_id, session_id, question_id = _pending_questions(client, offer, audience, offer, field="offer")
    with db()() as session:
        session.get(Venture, venture_id).raw_idea = opening
        session.delete(session.get(ClarifierTurn, question_id))
        row = session.get(IdeaSession, session_id)
        for other in session.scalars(select(IdeaSession).where(IdeaSession.venture_id == venture_id, IdeaSession.id != session_id)):
            other.state = "abandoned"
        payload = {"session_id": row.id, "revision_id": row.current_revision_id,
            "workspace_id": row.workspace_id, "venture_id": row.venture_id}
        assert wallet_phase(session) == "person"
        for job in session.scalars(select(Job).where(Job.venture_id == venture_id, Job.status == "scheduled")):
            job.status = "cancelled"
        session.commit()
    prompt = messages_for({"original_idea": opening, "project_intent": "startup"})[0]["content"]
    assert ("already have customers" in prompt) is live
    assert ("first paid user" in prompt) is not live
    assert post_fallback_question(payload)
    saved = get_session(session_id)
    question = saved["turns"][-1]
    assert question["kind"] == "question"
    assert not question["options"].get("options")
    assert ("first version" in question["text"].lower()) is not live
    assert ("improve" in question["text"].lower()) is live
    answer = "I spend most of my week on device maintenance and support." if live else "It should record one measurement and display it on a screen."
    calls = install_provider(monkeypatch, lambda *_: completion(understanding={"offer": offer, "customer": audience}))
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": answer})
    assert response.status_code == 200, response.text
    run_queued("idea.clarify_turn")
    assert len(calls) == 1
    saved = get_session(session_id)
    assert saved["current_revision"]["structured"]["customer"] == audience
    assert any(turn["role"] == "founder" and turn["text"] == answer for turn in saved["turns"])


@pytest.mark.parametrize("offer,audience,summary", [
    ("A scanner that tracks equipment returns", "Equipment rental operators", "Track rental returns."),
    ("A watering device that measures soil moisture", "Home gardeners", "Monitor soil moisture."),
])
def test_saved_customer_is_not_replaced_by_product_pronouns(client, offer, audience, summary):
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.runner import _core_clarification

    snapshot = {"project_intent": "startup", "original_idea": f"{offer} for {audience}.",
        "understanding": {"offer": offer, "customer": audience},
        "conversation": [{"role": "founder", "kind": "answer", "field": "workflow",
            "text": "You need to reduce support time for those existing devices."}]}
    result = _core_clarification(snapshot, Clarification(case="know", action="question",
        message="Choose your next improvement.",
        understanding=Understanding(summary=summary, offer=offer, customer="those existing devices"),
        question=Question(field="problem", text="What have you observed when those existing devices try to handle this?",
            why="Choose your next improvement.", options=[])))
    assert result.understanding.customer == audience
    assert result.understanding.summary == summary
@pytest.mark.parametrize("options", [[], ["Describe your first task"]])
@pytest.mark.parametrize("offer,audience,answers", [
    ("A vulnerability review tool for hospital security teams", "hospital security teams", [
        ("workflow", "The first task is to turn our device inventory and vulnerability alerts into a short ranked list of devices that require action today with a reason for each priority and a suggested owner.", None),
        ("requirements", "The product must match device models and firmware versions to known vulnerabilities, rank the risk using network exposure and clinical importance, and keep an audit trail that hospital security leads can review without any patient data.", None),
    ]),
    ("A process change review workspace for cell-therapy companies", "general lab staff", [
        ("pricing_hypothesis", "I would start with a fixed-scope pilot for one process-development team at $2,000 for a month, including five example change records and a handoff review. That is a pricing hypothesis, not a price anyone has agreed to pay.", None),
        ("workflow", "One correction: the first users are process-development leads at early-stage autologous cell-therapy companies, not clinicians or manufacturing operators. This is a read-only document workspace for reviewing experiment evidence and the human-approved reason behind a process change, not a tool that makes GMP decisions.", "process-development leads at early-stage autologous cell-therapy companies"),
    ]),
    ("A pallet transport robot for midsize warehouses", "midsize warehouses", [
        ("requirements", "The robot already moves loaded pallets from warehouse receiving to storage during our three pilots. It must safely handle that same route, stay within existing warehouse safety procedures, and show an operations manager how much forklift travel time it saves.", None),
    ]),
    ("A home aquarium water monitor", "home aquarium keepers", [
        ("workflow", "It should compare water readings to safe ranges and export a short care report for a household member.", None),
    ]),
    ("A telemetry review tool for satellite mission operators", "mission operators at small earth observation satellite companies", [
        ("requirements", "It must read a telemetry CSV, flag unusual battery readings, and show the operator the readings behind each warning.", None),
    ]),
    ("A heat pump scheduling app for food manufacturing energy managers", "food manufacturing energy managers", [
        ("first_user_path", "I would ask food manufacturing energy managers for short research interviews through industry associations and personal introductions, and I have not contacted anyone yet.", None),
    ]),
    ("A meal-planning app for families managing food allergies", "families managing food allergies", [
        ("requirements", "The app must match ingredients to allergy warnings and show a parent the reasons behind each warning.", None),
        ("pricing_hypothesis", "I would start with a month-long trial for one household at $5 for a month, including five example meal plans. That is only a pricing hypothesis.", None),
        ("first_user_path", "I would ask parents for short research interviews through school associations and personal introductions, and I have not contacted anyone yet.", None),
        ("workflow", "Actually, the first users are parents planning meals for children with food allergies, not all households. The app must keep the original allergy records.", "parents planning meals for children with food allergies"),
        ("customer", "For families planning meals for older relatives, the app should preserve allergy records.", "families planning meals for older relatives"),
    ]),
])
def test_only_customer_answers_or_explicit_corrections_change_audience(client, monkeypatch, offer, audience, answers, options):
    from app import state
    from app.clarify.dilemmas import venture_context
    from app.credits.policy import wallet_phase
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack.office_hours import saved_snapshot
    from app.idea.agent import Clarification, Understanding
    from app.idea.runner import _core_clarification, _known_fields
    from app.idea.service import _next_turn_number, get_session, post_message
    from tests.unit.chat.test_founder_intake_progress import _pending_questions
    from core.clock import utcnow
    from core.ids import new_id

    if postgres_url := os.environ.get("TEST_POSTGRES_URL"):
        monkeypatch.setenv("COMPANY_OS_DB_URL", postgres_url)
        state.reset_db_factory()
    venture_id, session_id, question_id = _pending_questions(client, offer, audience, offer)
    initial_audience = audience
    for index, (field, answer, correction) in enumerate(answers):
        with state.db()() as session:
            assert wallet_phase(session) == "person"
            idea = session.get(IdeaSession, session_id)
            idea.state = "clarifying"
            question = session.get(ClarifierTurn, question_id) if index == 0 else ClarifierTurn(
                id=new_id("ict"), session_id=session_id, workspace_id=idea.workspace_id, venture_id=venture_id,
                turn=_next_turn_number(session, session_id), role="agent", kind="question", revision_id=idea.current_revision_id,
                created_at=utcnow())
            question.text = "Who is your product for?" if field == "customer" else "What should your product do first?"
            question.options = json.dumps({"field": field, "options": options})
            session.add(question)
            question_id = question.id
            session.commit()
        post_message(session_id, answer, defer_job=True, **({"expected_question_id": question_id} if options else {}))
        audience = correction or audience
        structured = get_session(session_id)["current_revision"]["structured"]
        assert structured["_answered_fields"]["customer"] == (answer if field == "customer" else audience)
        with state.db()() as session:
            idea = session.get(IdeaSession, session_id)
            snapshot = saved_snapshot(session, venture_id, idea.current_revision_id)
        assert snapshot["question_context"]["audience"] == audience
        assert venture_context({**snapshot, "founder_facts": {}, "founder_fact_updated_at": {}, "answered_questions": [
            {"field": "audience", "founder_text": initial_audience}, *snapshot["answered_questions"]]})[0] == audience
        snapshot.update(original_idea=offer, project_intent="startup", understanding=structured,
            answered_fields=structured["_answered_fields"], conversation=[{"role": "founder", "field": field, "text": answer}])
        assert _known_fields(snapshot)["customer"] == audience
        result = _core_clarification(snapshot, Clarification(case="know", action="proposal", message="Review your idea.",
            understanding=Understanding(offer=offer, customer="an operator")))
        assert result.understanding.customer == audience


@pytest.mark.parametrize("offer,audience,requirement,constraint", [
    ("A tool to reconcile shipment invoices", "Freight coordinators", "Show invoice discrepancies", "Do not change accounting records"),
    ("An app to plan weekend meals", "Parents planning family dinners", "Show ingredient quantities", "Respect food allergies"),
])
def test_first_question_keeps_list_answers_and_fact_times(client, monkeypatch, offer, audience, requirement, constraint):
    from app.credits.policy import wallet_phase
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaClarificationRun, IdeaSession
    from app.gstack.office_hours import saved_snapshot
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.service import get_session
    from app.intake.smart import classify_fallback
    from app.state import db
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued

    text = f"{offer}. Audience: {audience}\nRequirements: {requirement}\nConstraints: {constraint}"
    stub(monkeypatch, classify_fallback(text).to_dict())
    response = completion()
    response.choices[0].message.content = Clarification(case="know", action="question",
        message="Your idea is saved. How would you charge for it?",
        understanding=Understanding(offer=offer, customer=audience, requirements=[requirement], constraints=[constraint]),
        question=Question(text="How would you charge for it?", field="pricing_hypothesis",
            why="Understand how you would charge", options=[])).model_dump_json()
    calls = install_provider(monkeypatch, lambda *_: response)
    created = confirm(client, preview(client, text)["proposal"])
    job_id = run_queued("idea.clarify_turn")
    with db()() as session:
        assert wallet_phase(session) == "person"
        job = session.get(Job, job_id)
        assert job.status == "succeeded", job.last_error
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job_id))
        assert run.state == "completed" and json.loads(run.usage)["settled"]
        idea = session.get(IdeaSession, run.session_id)
        assert idea.questions_asked == 1
        snapshot = saved_snapshot(session, created["venture_id"], idea.current_revision_id)
        assert snapshot["_answered_fields"]["requirements"] == [requirement]
        assert snapshot["_answered_fields"]["constraints"] == [constraint]
        assert snapshot["founder_facts"]["audience"]["value"] == audience
        assert snapshot["founder_fact_updated_at"]["audience"]
        assert not {"requirements", "constraints"} & snapshot["founder_fact_updated_at"].keys()
        assert session.get(Venture, created["venture_id"]).workspace_id == run.workspace_id
    saved = get_session(run.session_id)
    assert saved["turns"][-1]["kind"] == "question"
    assert len(calls) == 1


@pytest.mark.parametrize("offer,audience", [
    ("A tool that schedules equipment maintenance", "Repair shop operators"),
    ("An app that plans stargazing outings", "Astronomy hobbyists"),
])
@pytest.mark.parametrize("rejected", [False, True])
def test_rejected_followup_recovers_saved_answers_without_a_second_call(client, monkeypatch, offer, audience, rejected):
    from sqlalchemy import select
    from app.chat.conversations import _legacy
    from app.db.models import ChatMessage, Job, Venture
    from app.db.supymem_models import IdeaClarificationRun, IdeaSession
    from app.idea.service import get_session, post_message
    from app.state import db
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued
    from tests.unit.chat.test_founder_intake_progress import _pending_questions
    from core.clock import utcnow
    from core.ids import new_id

    venture_id, session_id, question_id = _pending_questions(client, offer, audience, offer, field="offer")
    with db()() as session:
        session.get(IdeaSession, session_id).questions_asked = 2
        for job in session.scalars(select(Job).where(Job.venture_id == venture_id, Job.status == "scheduled")):
            job.status = "cancelled"
        session.commit()
    response = completion(understanding={"offer": offer, "customer": audience}, message="Review your saved idea and correct anything missing.")
    if rejected:
        response.choices[0].message.content = '{"action":"send_email"}'
    calls = install_provider(monkeypatch, lambda *_: response)
    accepted = post_message(session_id, offer, expected_question_id=question_id, reply_in_chat=True)
    with db()() as session:
        venture = session.get(Venture, venture_id)
        session.add(ChatMessage(id=new_id("msg"), venture_id=venture_id, workspace_id=venture.workspace_id,
            conversation_id=_legacy(session, venture).id, role="user", kind="idea_answer_with_request",
            body_md=offer + ". Please plan a small market test.", created_at=utcnow()))
        session.commit()
        from app.idea.runner import _questionnaire_interrupted
        assert _questionnaire_interrupted(session, json.loads(session.get(Job, accepted["job_id"]).payload_json))
    run_queued("idea.clarify_turn", through_job_id=accepted["job_id"])
    with db()() as session:
        job = session.get(Job, accepted["job_id"])
        assert job.status == "succeeded", job.last_error
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job.id))
        assert run.state == "completed"
        assert json.loads(run.usage)["provenance"] == "provider_usage"
        assert run.error_code is None
        if rejected:
            assert json.loads(run.usage)["output"]["validation_kind"] == "schema"
    saved = get_session(session_id)
    assert len(calls) == 1
    assert saved["current_revision"]["structured"]["offer"] == offer
    assert saved["current_revision"]["structured"]["customer"] == audience
    assert saved["turns"][-1]["role"] == "agent"
    assert saved["turns"][-1]["kind"] in {"question", "proposal"}
    assert not saved["clarification_invalid_output_seen"] and not saved["clarification_retry_available"]
    from app.chat.store import list_messages
    replies = [message for message in list_messages(venture_id) if message["kind"] == "idea_clarification_ready"]
    assert len(replies) == 1
    reply = replies[0]
    assert "Try again" not in reply["body_md"] and "model" not in reply["body_md"]


@pytest.mark.parametrize("description", ["marketplace", "meals", "invoices"])
def test_earlier_description_is_saved_shown_and_editable(client, monkeypatch, description):
    from app.core.local_mode import is_local
    from app.idea.question_history import question_field
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.runner import _known_fields
    from app.idea.service import _next_turn_number, get_session, patch_revision, post_message
    from app.intake.smart import classify_fallback
    from app.state import db
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued
    from tests.unit.idea.test_answered_fields import INVOICES, MARKETPLACE, MEALS

    text = {"marketplace": MARKETPLACE + "\nAudience: Developers publishing reusable AI agents",
            "meals": MEALS, "invoices": INVOICES}[description]
    stub(monkeypatch, classify_fallback(text).to_dict())
    response = completion()
    response.choices[0].message.content = Clarification(case="know", action="question", message="Saved",
        understanding=Understanding(), question=Question(text="How would it work?", field="workflow",
            why="Understand how people use it", options=[])).model_dump_json()
    snapshots = []

    def answer(messages, *_):
        snapshots.append(json.loads(messages[-1]["content"]))
        return response

    install_provider(monkeypatch, answer)
    receipt = confirm(client, preview(client, text)["proposal"])
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        session_id = row.id
        if description == "invoices":
            from core.clock import utcnow
            first_turn = _next_turn_number(session, session_id)
            for index in range(25):
                session.add(ClarifierTurn(id=f"earlier-description-{index}", session_id=session_id,
                    workspace_id=row.workspace_id, venture_id=row.venture_id, turn=first_turn + index,
                    role="founder", kind="answer", text=text if index == 0 else "Keep the first version small.",
                    created_at=utcnow()))
            session.commit()
    post_message(session_id, "The first version should work on mobile.", reply_in_chat=True)
    run_queued("idea.clarify_turn")
    assert snapshots and any(turn["text"] == text for turn in snapshots[-1]["conversation"] if turn["role"] == "founder")
    saved = get_session(session_id)
    structured = saved["current_revision"]["structured"]
    for field in ("offer", "customer", "workflow", "pricing_hypothesis"):
        assert structured["_answered_fields"][field] in text
        assert structured[field] == structured["_answered_fields"][field]
    latest = saved["turns"][-1]
    if is_local() and latest["kind"] == "question":
        assert question_field({**latest["options"], "text": latest["text"]}) not in {"offer", "audience", "workflow", "pricing_hypothesis"}
    else:
        assert latest["kind"] == "proposal"
    from app.chat.store import list_messages
    bodies = "\n".join(message["body_md"] for message in list_messages(receipt["venture_id"]))
    assert structured["workflow"] in bodies
    assert structured["pricing_hypothesis"] in bodies
    correction = "Choose a plan, review it, then save it."
    patch_revision(session_id, {"workflow": correction})
    edited = get_session(session_id)["current_revision"]["structured"]
    assert edited["workflow"] == edited["_answered_fields"]["workflow"] == correction
    assert _known_fields({"original_idea": text, "answered_fields": edited["_answered_fields"]})["workflow"] == correction


@pytest.mark.parametrize("idea,quote,audience,correction", [
    ("A tool to reconcile shipment invoices", "People arranging deliveries at small freight firms need this",
     "Independent freight coordinators", "Regional dispatch managers"),
    ("An app to plan weekend meals", "Mothers and fathers arranging supper each week need this",
     "Parents planning family dinners", "Families cooking together"),
])
def test_intake_evidence_survives_model_revisions_and_yields_to_founder_corrections(client, monkeypatch, idea, quote, audience, correction):
    from app.db.supymem_models import IdeaSession
    from app.entry.chat_start import _onboarding_progress
    from app.idea.service import get_session, patch_revision, post_message
    from app.intake.smart import PageContext, _validated_result, classify_fallback
    from app.state import db
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued

    text = idea + ". " + quote
    fallback = classify_fallback(text)
    intake = fallback.to_dict()
    fact = {"value": audience, "quote": quote, "source": "founder message", "confidence": .95}
    intake["prefill_answers"]["customer"] = audience
    intake["prefill_facts"] = {"customer": fact}
    literal = {**intake, "prefill_answers": {**intake["prefill_answers"], "offer": idea},
        "prefill_facts": {"customer": fact, "offer": {"value": idea, "quote": idea, "source": "founder message", "confidence": .7}}}
    assert not _onboarding_progress(_validated_result(literal, text, PageContext(), fallback).to_dict(), text).get("question")
    uncertain = _validated_result({**intake, "prefill_facts": {"customer": {**fact, "confidence": .7}}}, text, PageContext(), fallback).to_dict()
    progress = _onboarding_progress(uncertain, text)
    assert progress["field"] == "customer" and progress["prefill"] == audience
    assert "customer" not in progress["completed"]
    stub(monkeypatch, _validated_result(intake, text, PageContext(), fallback).to_dict())
    receipt = confirm(client, preview(client, text)["proposal"])
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        session_id = row.id
    assert get_session(session_id)["current_revision"]["structured"]["_prefill_facts"]["customer"] == fact
    calls = install_provider(monkeypatch, lambda *_: completion(understanding={"offer": idea}))
    post_message(session_id, "Just propose")
    run_queued("idea.clarify_turn")
    assert calls
    assert get_session(session_id)["current_revision"]["structured"]["_prefill_facts"]["customer"] == fact
    patch_revision(session_id, {"customer": correction})
    saved = get_session(session_id)["current_revision"]["structured"]
    assert saved["_answered_fields"]["customer"] == correction
    assert "customer" not in saved["_prefill_facts"]
    patch_revision(session_id, {"customer": "Assumed: " + audience + "\nNo one interviewed yet"})
    from app.db.models import Venture
    from app.ventures import founder_facts
    with db()() as session:
        facts = founder_facts.read(session, session.get(Venture, receipt["venture_id"]))
        assert founder_facts.policy(facts, "customer") == "ask"


@pytest.mark.parametrize("idea,audience", [
    ("A tool to reconcile shipment invoices", "CFOs at regional transport companies"),
    ("An app to plan weekend meals", "Families who cook together on weekends"),
])
def test_explicit_free_text_audience_is_not_reasked(client, monkeypatch, idea, audience):
    from app.core.local_mode import is_local
    from app.idea.question_history import question_field
    from tests.unit.idea.test_core_onboarding import assert_moves_past
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.entry.chat_start import _onboarding_progress
    from app.idea.service import _next_turn_number, get_session, post_message
    from app.intake.smart import classify_fallback
    from app.state import db
    from core.clock import utcnow
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued

    answer = "It is for " + audience
    text = idea + "\nAudience: " + answer
    intake = classify_fallback(text).to_dict()
    progress = _onboarding_progress(intake, text)
    assert progress["completed"] == ["offer", "customer"] and not progress.get("question")
    stub(monkeypatch, intake)
    receipt = confirm(client, preview(client, text)["proposal"])
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        row.state = "clarifying"
        session.add(ClarifierTurn(id="audience-question", session_id=row.id, workspace_id=row.workspace_id,
            venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role="agent", kind="question",
            text="Who is it for?", options=json.dumps({"field": "customer", "options": []}),
            revision_id=row.current_revision_id, created_at=utcnow()))
        session.commit()
        session_id = row.id
    result = post_message(session_id, answer, expected_question_id="audience-question")
    assert result["session"]["current_revision"]["structured"]["_answered_fields"]["customer"] == answer
    assert result["session"]["turns"][-1]["options"]["field"] == "customer"
    calls = install_provider(monkeypatch, lambda *_: completion(proposal=False,
        understanding={"offer": idea}, text="Who is it for?", options=["A first group", "A second group"]))
    run_queued("idea.clarify_turn")
    saved = get_session(session_id)
    assert calls and saved["current_revision"]["structured"]["_answered_fields"]["customer"] == audience
    assert any(turn["role"] == "founder" and turn["text"] == answer for turn in saved["turns"])
    latest = saved["turns"][-1]
    if is_local() and latest["kind"] == "question":
        assert question_field({**latest["options"], "text": latest["text"]}) != "audience"
    else:
        assert latest["kind"] == "proposal"
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.runner import _core_clarification
    result = Clarification(case="know", action="question", message="Let's review this.", understanding=Understanding(),
        question=Question(field="requirements", text="What must this support?", why="Save the requirements", options=[]))
    refined = _core_clarification({"original_idea": idea, "answered_fields": {"customer": answer, "requirements": audience}}, result)
    assert_moves_past(refined, "offer")
    assert refined.understanding.requirements == []


def test_partial_idea_acknowledges_audience_without_inventing_segments(client, monkeypatch):
    from app.intake.smart import classify_fallback
    text = "GymLedger: " + GYM_IDEA
    intake = classify_fallback(text).to_dict()
    assert intake["prefill_answers"]["customer"] == "gym owners"
    stub(monkeypatch, intake)
    result = preview(client, text)
    proposal = result.get("proposal") or client.get(f"/entry/chat/proposals/{result['id']}").json()["proposal"]
    assert proposal["onboarding_question"].startswith("Gym owners, and members keep leaving.")
    assert proposal["onboarding_field"] == "customer"
    assert proposal["onboarding_choices"] == []
    assert client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]["onboarding_choices"] == proposal["onboarding_choices"]


def test_startup_handoff_does_not_repeat_onboarding_intro(client, monkeypatch):
    from app.intake.smart import classify_fallback
    text = "An app for gym owners to track inactive members"
    stub(monkeypatch, classify_fallback(text).to_dict())
    proposal = preview(client, text)["proposal"]
    receipt = confirm(client, proposal)
    messages = client.get(f"/ventures/{receipt['venture_id']}/chat").json()["items"]
    assert proposal["message"] == "What should we call it?"
    assert all(proposal["message"] not in message["body_md"] for message in messages)
    assert "Your startup idea is saved." in messages[-1]["body_md"]


@pytest.mark.parametrize("idea", ["A tool for freight dispatchers to track delayed deliveries", "An app for weekend hikers to plan safe walking routes"])
@pytest.mark.parametrize("phase", ["clarifying", "gstack"])
def test_options_request_keeps_the_same_question_without_saving_an_answer(client, monkeypatch, idea, phase):
    from app.chat.idea_bridge import pending_question
    from app.idea.agent import is_options_request
    from app.chat.router import _answer_idea
    from core.clock import utcnow
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import _next_turn_number
    from app.intake.smart import classify_fallback
    from app.state import db
    stub(monkeypatch, classify_fallback(idea).to_dict())
    receipt = confirm(client, preview(client, idea)["proposal"])
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        row.state = "validating" if phase == "gstack" else "clarifying"
        options = {"field": "customer", "options": []}
        if phase == "gstack":
            options.update(phase="gstack", field="gstack_Q2", gstack_id="Q2", gstack_name="Status quo", index=2, total=3, queue=[], why="Understand the current approach")
        question = ClarifierTurn(id="ict_unsure", session_id=row.id, workspace_id=row.workspace_id,
            venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role="agent", kind="question",
            text="How do you handle this task today?", options=json.dumps(options), revision_id=row.current_revision_id, created_at=utcnow())
        session.add(question)
        session.commit()
        session_id, revision_id, count = row.id, row.current_revision_id, row.questions_asked
    def not_saved(*args, **kwargs):
        pytest.fail("An options request must not be saved, captured, or queued as an answer")
    monkeypatch.setattr("app.intake.evidence.save_statement", not_saved)
    monkeypatch.setattr("app.ventures.founder_facts.capture", not_saved)
    monkeypatch.setattr("app.ventures.founder_facts.schedule", not_saved)
    monkeypatch.setattr("app.idea.service.enqueue", not_saved)
    question_id = "ict_unsure"
    assert not is_options_request("I help people answer customer questions")
    assert not is_options_request("Teams give me options during planning")
    assert pending_question(receipt["venture_id"], "give me options") == (session_id, question_id)
    with db()() as session:
        shown_text = session.get(ClarifierTurn, question_id).text
    for answer in ["give me options", "options?", "suggest answers", "help me answer", "Could you show me choices?", "please give me some options", "Assumed: People handling it themselves\ngive me options"]:
        assert pending_question(receipt["venture_id"], answer) == (session_id, question_id)
        reply = asyncio.run(_answer_idea(receipt["venture_id"], answer, (session_id, question_id), actor_subject=None, persist=False))
        assert reply["kind"] == "idea_clarification_ready"
        assert "your answer is saved" not in reply["body_md"]
        chips = [action for action in reply["actions"] or [] if action["label"] != "Skip the questions"]
        assert not chips and "Type your own answer" in reply["body_md"]
        assert "Here are a few possibilities" not in reply["body_md"]
        with db()() as session:
            row = session.get(IdeaSession, session_id)
            assert row.current_revision_id == revision_id and row.questions_asked == count
            latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id).order_by(ClarifierTurn.turn.desc()))
            assert latest.role == "agent" and latest.kind == "question"
            assert not session.scalar(select(ClarifierTurn.id).where(ClarifierTurn.session_id == session_id, ClarifierTurn.role == "founder", ClarifierTurn.text == answer))
            assert latest.id == question_id and latest.text == shown_text


@pytest.mark.parametrize("idea,audience", [("A tool for freight dispatchers to track delayed deliveries", "freight dispatchers"),
    ("An app for weekend hikers to plan safe walking routes", "weekend hikers")])
def test_missing_choices_without_customer_pain_remain_open(idea, audience):
    from types import SimpleNamespace
    from tests.unit.idea.test_question_choices_fit import _question_reply
    from app.idea.service import serialize_turn
    from app.gstack.office_hours import render_question
    from app.entry.chat_start import _clarify_choices, _onboarding_progress

    for field, text in [("customer", "Who needs this?"), ("current_solution", "How do you handle this today?")]:
        question = {"field": field, "options": []}
        body, actions = _question_reply(text, question, "question", idea=idea)
        assert actions is None and "Type your own answer" in body
        assert question["options"] == [] and question["free_text_only"]
        turn = SimpleNamespace(id="question", role="agent", kind="question", text=text, options=json.dumps({"field": field, "options": []}),
            turn=1, chosen=None, revision_id=None, llm_usage=None)
        saved = serialize_turn(turn, {"idea": idea})["options"]
        assert saved["options"] == [] and saved["free_text_only"]
    entry = _onboarding_progress({"path": "startup", "extracted": {"idea": idea}}, "A tool to help with a task")
    assert entry["choices"] == []
    assert _clarify_choices("What do you want to work on?") == _clarify_choices(idea) == []
    for flags in [{}, {"free_text_only": True}, {"detail_pending": True}]:
        rendered = render_question({"text": "How do you handle this today?", "gstack_id": "Q2", "options": [], **flags}, {"idea": idea})
        assert rendered["options"] == [] and rendered["free_text_only"]


@pytest.mark.parametrize("idea", ["A tool to track delayed freight deliveries", "An app to plan safe walking routes"])
@pytest.mark.parametrize("phase", ["clarifying", "gstack"])
@pytest.mark.parametrize("answer", ["I don't know the answer", "I don't know the answer yet", "not sure", "skip", "IDK?", "I do not know yet", "I'm not sure", "Assumed: People handling it themselves\nnot sure"])
def test_uncertainty_records_an_assumption_and_never_repeats(client, monkeypatch, idea, phase, answer):
    from app.gstack.office_hours import is_skip
    from app.idea.agent import answer_metadata
    from app.chat.idea_bridge import pending_question
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import _next_turn_number, post_message
    from app.intake.smart import classify_fallback
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    from app.entry.chat_start import _onboarding_progress

    stub(monkeypatch, classify_fallback(idea).to_dict())
    receipt = confirm(client, preview(client, idea)["proposal"])
    entry = _onboarding_progress({"path": "startup", "extracted": {"idea": idea}}, idea + "\nAudience: " + answer)
    assert "customer" in entry["completed"] and any("customer" in assumption for assumption in entry["assumptions"])
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
        row.state = "validating" if phase == "gstack" else "clarifying"
        options = {"field": "customer", "options": ["People handling it themselves", "Teams handling it together"]}
        if phase == "gstack":
            options.update(phase="gstack", field="gstack_Q2", gstack_id="Q2", gstack_name="Status quo", index=1, total=2,
                queue=[{"gstack_id": "Q4", "gstack_name": "First version", "text": "What should the first version do?", "why": "Choose a starting task", "options": ["One task", "Related tasks"]}])
        qid = new_id("ict")
        session.add(ClarifierTurn(id=qid, session_id=row.id, workspace_id=row.workspace_id, venture_id=row.venture_id,
            turn=_next_turn_number(session, row.id), role="agent", kind="question", text="How do you handle this today?",
            options=json.dumps(options), revision_id=row.current_revision_id, created_at=utcnow()))
        session.commit()
        sid = row.id
    assert pending_question(receipt["venture_id"], answer) == (sid, qid)
    result = post_message(sid, answer, expected_question_id=qid)
    turns = result["session"]["turns"]
    saved = next(turn for turn in reversed(turns) if turn["role"] == "founder")
    if phase == "gstack" and not is_skip(answer):
        metadata = answer_metadata(answer)
        assert saved["text"] == answer
        assert saved["options"]["original_metadata"]["founder_text"] == metadata["founder_text"]
    else:
        assert saved["text"].startswith(("Assumed: ", "Skip"))
    assert saved["options"]["founder_text"] == "" or saved["text"].startswith("Skip")
    assert not any(turn["role"] == "agent" and turn["id"] != qid and turn["turn"] > next(turn["turn"] for turn in turns if turn["id"] == qid)
        and turn["text"] == "How do you handle this today?" for turn in turns)


def test_plain_uncertainty_keeps_the_original_answer_without_capturing_evidence(client, monkeypatch):
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import _next_turn_number, post_message
    from app.intake.smart import classify_fallback
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    def not_evidence(*args, **kwargs):
        pytest.fail("An uncertain answer must not be captured as evidence")

    receipts = []
    for idea in ["A tool for payroll teams to reconcile deductions", "An app for hikers to plan walking routes"]:
        stub(monkeypatch, classify_fallback(idea).to_dict())
        receipts.append(confirm(client, preview(client, idea)["proposal"]))
    monkeypatch.setattr("app.intake.evidence.save_statement", not_evidence)
    monkeypatch.setattr("app.ventures.founder_facts.capture", not_evidence)
    monkeypatch.setattr("app.ventures.founder_facts.schedule", not_evidence)
    for receipt in receipts:
        for answer in ["skip", "not sure"]:
            with db()() as session:
                row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == receipt["venture_id"]))
                row.state = "clarifying"
                session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id,
                    venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role="agent", kind="question",
                    text="Who would use it?", options=json.dumps({"field": "customer", "options": []}),
                    revision_id=row.current_revision_id, created_at=utcnow()))
                session.commit()
                sid = row.id
            result = post_message(sid, answer)
            saved = next(turn for turn in reversed(result["session"]["turns"]) if turn["role"] == "founder")
            assert saved["text"] == answer


def test_q3_known_audience_asks_urgency_with_matching_title_and_options(client):
    from app.gstack.office_hours import default_picks, render_question
    from app.ventures.founder_facts import literal_facts
    snapshot = {"idea": "A tool for gym owners to track inactive members", "problem": "track inactive members",
        "audience": "gym owners", "founder_facts": literal_facts("A tool for gym owners to track inactive members")}
    question = next(pick for pick in default_picks(snapshot) if pick["gstack_id"] == "Q3")
    rendered = render_question(question, snapshot)
    assert rendered["gstack_name"] == "What makes it urgent"
    assert rendered["text"] == "For gym owners, what makes this problem urgent in their day-to-day work?"
    assert rendered["prefill"] == "gym owners"
    assert len(rendered["options"]) == 3
    assert all("gym owners" not in option and "track inactive members" not in option for option in rendered["options"])
    assert rendered["requires_detail"] == rendered["options"][:1]


def test_recommendation_copy_is_plain_including_saved_legacy_copy(client):
    from app.idea.agent import safe_recommendation
    from app.gstack.office_hours import question_turn_options
    _, reason = safe_recommendation("Have you talked to gym owners?", ["No one yet", "A few people"])
    assert reason == "Pick this if you have not checked yet."
    options = question_turn_options({"text": "Which gym owners should we start with?", "options": ["Independent gyms", "Fitness studios"],
        "gstack_id": "Q3", "gstack_name": "Desperate Specificity", "why": "Choose a group"}, [], 3, 3)
    assert not options["recommended_option"] and not options["recommendation_reason"]
    for legacy in ("No founder evidence is assumed; choose a stronger answer only if it matches what actually happened.",
                   "Use this conservative assumption until you have evidence for a stronger answer."):
        assert safe_recommendation("Which gyms?", ["Independent gyms", "Fitness studios"], reason=legacy)[1] == reason
    assert "—" not in reason


def test_saved_default_is_exposed_to_chat_even_without_options(client):
    from app.db.supymem_models import ClarifierTurn
    from app.gstack.office_hours import _reply
    default = "Independent gym owners"
    question = ClarifierTurn(id="ict_default", text="Is this the audience you meant?", options=json.dumps({
        "phase": "gstack", "gstack_id": "Q3", "gstack_name": "Desperate Specificity", "index": 3, "total": 3,
        "options": [], "prefill": default}))
    _, actions = _reply(question)
    assert actions[0]["text"] == default
    assert actions[0]["payload"]["prefill"] == default


@pytest.mark.parametrize("tag", ["I haven't observed the specific step yet / I'm not sure", "A tag the model invented"])
def test_unknown_detail_tags_are_dropped_not_fatal(tag):
    """Production 2026-10-24: a detail tag naming no option failed the whole clarification turn."""
    from app.idea.agent import Question
    from app.gstack import office_hours as oh

    q = Question(text="Which step takes the most time?", field="workflow", why="Defines the first feature.",
                 options=[], requires_detail=[tag], detail_prompt="")
    assert q.requires_detail == []
    q = Question(text="Which step takes the most time?", field="workflow", why="Defines the first feature.",
                 options=["Matching", "Exceptions"], requires_detail=["Matching", tag])
    assert q.requires_detail == ["Matching"]
    pick = oh.Picked(id="Q2", question="What do they use today?", options=["A spreadsheet", "Paper", "Nothing yet"],
                requires_detail=[tag], detail_prompt="")
    assert pick.requires_detail == []
