"""GStack office hours: vendored text, interactive questions, critique, feed-forward."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from tests.company_os_harness import COMPANY_OS, append_idea_revision, company_os_test_client, load_company_os_app

load_company_os_app()

from tests.security import test_connector_oauth_boundary as connector_oauth
from tests.security.test_durable_journey_boundary import IDEA, request, run_queued, venture
from tests.idea_fixtures import completion, install_provider
from tests.planning_fixtures import install_planner
from tests.research_fixtures import install_research
from tests.unit.gtm.test_known import known_case, journey_db

hosted_client = connector_oauth.client


@pytest.fixture()
def client(request, monkeypatch):
    if (COMPANY_OS / "app/admin/internal.py").is_file():
        yield request.getfixturevalue("hosted_client")
        return
    callspec = getattr(request.node, "callspec", None)
    if callspec and callspec.params.get("mode") == "hosted":
        pytest.skip("Hosted idea review is not in the public local release.")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "local")

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected live provider HTTP call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    with company_os_test_client(load_company_os_app().create_app()) as local_http:
        yield local_http


@pytest.mark.parametrize("product,buyer", [
    ("bridge cables", "engineering firms"), ("vinyl records", "music collectors"),
])
def test_office_hours_keeps_the_offer_excerpt_at_a_word_boundary(monkeypatch, product, buyer):
    from app.chat import execute
    from app.ventures import current_idea
    from textwrap import shorten

    offer = f"An inspection kit for {product} sold to {buyer} " + "to find hard-to-spot faults before damage spreads " * 5
    saved = SimpleNamespace(stage="draft")
    session = MagicMock()
    session.__enter__.return_value.get.return_value = saved
    monkeypatch.setattr(execute, "db", lambda: lambda: session)
    monkeypatch.setattr(current_idea, "idea_summary", lambda *args, **kwargs: f"Customer: {buyer}\nOffer: {offer}")
    reply = execute._office_hours("venture")["body_md"]
    assert "\n\nIdea:" in reply
    assert f"Customer: {buyer}\nOffer: {shorten(offer, width=173, placeholder='…')}\n" in reply


@pytest.mark.parametrize("product,buyer,description", [
    ("bridge cables", "engineering firms", "A kit for bridge cables that detects corrosion"),
    ("vinyl records", "music collectors", "A kit that cleans vinyl records"),
])
def test_boot_repairs_the_confirmed_audience_once_without_replacing_history(journey_db, monkeypatch, product, buyer, description):
    from sqlalchemy import select
    from app.bootstrap import bootstrap
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.ventures.current_idea import revision_fields
    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    venture_id = journey_db["vid"]
    factory = journey_db["factory"]
    confirm_saved_idea(venture_id)
    with factory() as session:
        offer = f"{description}, sold as a service to {buyer} to avoid waste."
        session.get(Venture, venture_id).raw_idea = offer
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        original = append_idea_revision(session, idea.current_revision_id,
            source="agent", structured=json.dumps({"offer": offer, "customer": product})).id
        session.commit()
    bootstrap()
    with factory() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        repaired = idea.current_revision_id
        assert repaired == idea.confirmed_revision_id and repaired != original
        assert revision_fields(session.get(IdeaRevision, original))["customer"] == product
        assert revision_fields(session.get(IdeaRevision, repaired))["customer"] == buyer
    bootstrap()
    with factory() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        assert idea.current_revision_id == repaired


@pytest.mark.parametrize("reason", [
    "I only know warehouse receiving from my own work",
    "I only know weekend birdwatching from my own trips",
], ids=["b2b", "consumer"])
def test_polite_question_skips_keep_reasons_and_conditions_distinct(reason):
    from app.gstack.office_hours import is_skip, skips_all_questions

    for message in (f"Skip the remaining questions please, {reason}.",
                    f"Please skip all questions because {reason}.",
                    f"Skip the questions please. {reason}.",
                    f"Skip all questions, please, {reason}."):
        assert skips_all_questions(message) and is_skip(message), message
    for message in (f"Skip the remaining questions please if {reason}.",
                    f"Please do not skip all questions because {reason}.",
                    f"Skip all questions please, but only if {reason}.",
                    f"Skip all questions because {reason}, unless I can answer.",
                    f"Skip all questions please. Actually, don't skip anything. {reason}."):
        assert not skips_all_questions(message) and not is_skip(message), message


@pytest.mark.parametrize("idea,reason", [
    ({"summary": "A receiving ledger", "offer": "Track damaged shipments", "customer": "warehouse receiving supervisors",
      "problem": "missing delivery records"}, "I have only worked on a loading dock"),
    ({"summary": "A birding notebook", "offer": "Record local bird sightings", "customer": "weekend birdwatchers",
      "problem": "forgotten sighting locations"}, "I have only kept my own field notes"),
], ids=["b2b", "consumer"])
def test_polite_setup_skip_never_falls_through_to_journey_steps(client, seen, monkeypatch, idea, reason):
    from app.chat import router

    venture_id, _, base = start(client, idea)

    async def dispatch(*args, **kwargs):
        pytest.fail("A setup skip must not be dispatched as a journey step")

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    response = request(client, "POST", f"/ventures/{venture_id}/chat",
                       json={"message": f"Skip the remaining questions please, {reason}."})
    assert response.status_code == 200, response.text
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing" and state["current_revision"]["structured"]["_questions_skipped"]
    assert "Nothing changed" not in response.json()["body_md"]
    assert all(item["skipped"] and not item["answer"] for item in state["gstack_answers"])


@pytest.mark.parametrize("audience,offer", [
    ("independent equipment rental owners", "a return inspection checklist"),
    ("people learning pottery at home", "a firing schedule journal"),
])
@pytest.mark.parametrize("phase", ["clarifier", "gstack"])
def test_typed_current_pricing_skip_advances_before_journey_routing(request, monkeypatch, audience, offer, phase):
    data = request.getfixturevalue("journey_db")
    import asyncio

    from app.chat import router
    from app.chat.store import append_message
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.gstack import office_hours
    from app.idea.runner import _question_reply
    from app.idea.service import _next_turn_number, get_session
    from app.state import db
    from core.clock import utcnow
    from sqlalchemy import select

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    venture_id = data["vid"]
    confirm_saved_idea(venture_id)
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        idea.state = "validating" if phase == "gstack" else "clarifying"
        revision = append_idea_revision(session, idea.current_revision_id,
            summary=f"Build {offer} for {audience}.",
            structured=json.dumps({"customer": audience, "offer": offer}))
        question = {"gstack_id": "Q2", "gstack_name": "Pricing", "field": "pricing_hypothesis",
            "text": f"How would you charge for helping {audience}?", "options": []}
        following = {"gstack_id": "Q5", "gstack_name": "Alternatives", "field": "alternatives",
            "text": f"What did {audience} try instead last time?", "options": []}
        if phase == "gstack":
            turn = office_hours.add_question_turn(session, idea, question, [following], 1, 2, utcnow())
            office_hours.chat_question(session, idea, turn)
        else:
            turn = ClarifierTurn(id="pricing_open_question", session_id=idea.id, workspace_id=idea.workspace_id,
                venture_id=venture_id, revision_id=revision.id, turn=_next_turn_number(session, idea.id), role="agent",
                kind="question", text=question["text"], options=json.dumps({"field": "pricing_hypothesis"}), created_at=utcnow())
            session.add(turn)
            session.flush()
            body, actions = _question_reply(turn.text, json.loads(turn.options), turn.id)
        session_id = idea.id
        session.commit()
    if phase != "gstack":
        append_message(venture_id, role="assistant", body_md=body, kind="idea_clarification_ready", actions=actions)
    async def reject_journey(*args, **kwargs):
        pytest.fail("The current question's skip must not reach journey routing")
    monkeypatch.setattr(router, "_prepare_intent", reject_journey)
    text = f"Skip pricing for now. You need evidence from {audience} before choosing a price."
    reply = asyncio.run(router.handle_message(venture_id, text, actor_subject=data["actor"]))
    saved = get_session(session_id)
    if phase == "gstack":
        assert saved["gstack_answers"][0]["skipped"] and saved["gstack_answers"][0]["answer"] == ""
        assert saved["turns"][-1]["kind"] == "question" and saved["turns"][-1]["options"]["index"] == 2
        assert saved["turns"][-1]["text"] in reply["body_md"]
    else:
        assert saved["turns"][-1]["role"] == "founder"
        assert not saved["turns"][-1]["options"]["founder_text"]
        with db()() as session:
            assert session.scalar(select(Job).where(Job.venture_id == venture_id, Job.kind == "idea.clarify_turn")) is not None
    assert "Nothing changed" not in reply["body_md"]
    assert not saved["current_revision"]["structured"].get("pricing_hypothesis")
    assert not office_hours.skips_only_this_question("Skip research for now.", field="pricing_hypothesis")


def test_office_hours_reuses_known_topics_and_keeps_source_choices(known_case, monkeypatch):
    from app.connections import context_docs
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession, IdeaRevision
    from app.gstack import office_hours as oh
    from app.ventures import founder_facts, startup_review
    from core.clock import utcnow

    data, now = known_case, utcnow()
    with data["factory"]() as session:
        row = IdeaSession(id="idea_known", workspace_id=data["wid"], venture_id=data["vid"],
            case="startup", state="clarifying", created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        picks = [{"gstack_id": "Q3", "gstack_name": "Customer", "text": "Who buys it?", "why": "Choose your audience", "options": ["A role", "Other"]}]
        assert oh.unanswered_picks(session, row, picks) == []
        assert oh.answers_for(session, row.id)[0]["answer"] == data["audience"]
        row2 = IdeaSession(id="idea_confirm", workspace_id=data["wid"], venture_id=data["vid"],
            case="startup", state="clarifying", created_at=now, updated_at=now)
        session.add(row2)
        startup_review._document(session, data["vid"], startup_review.INTERNAL_KIND,
            {"founder_facts": {"audience": founder_facts._fact(data["audience"], "website", .8)}})
        session.flush()
        question = oh.unanswered_picks(session, row2, picks)[0]
        assert question["known_check"]["status"] == "confirm" and "Is this correct?" in question["text"]
        assert oh.render_question(question, {})["text"] == question["text"]
        monkeypatch.setattr(context_docs, "research_context", lambda *_args: {"connected_context": {"documents": [
            {"title": "Your selected strategy", "excerpt": "Audience: Museum visitors"}]}})
        question = oh.unanswered_picks(session, row2, picks)[0]
        assert question["known_check"]["status"] == "conflict"
        assert "Your selected strategy" in question["text"] and data["audience"] in question["text"]
        session.add(IdeaRevision(id="revision_known", workspace_id=data["wid"], venture_id=data["vid"],
            session_id=row2.id, rev=1, source="agent", structured="{}", summary="Your saved product", created_at=now))
        session.flush()
        row2.current_revision_id = "revision_known"
        turn = oh.add_question_turn(session, row2, question, [], 1, 1, now)
        from app.idea import service
        serialized = service.serialize_turn(turn, {})
        assert serialized["options"]["known_check"]["status"] == "conflict"
        assert data["audience"] in serialized["options"]["options"]
        turn_id, row_id = turn.id, row2.id
        session.commit()
    service.post_message(row_id, data["audience"], actor_subject=data["actor"], expected_question_id=turn_id, defer_job=True)
    from app.ventures import known
    assert known.known(data["vid"], "audience")["status"] == "known"

ROOT = Path(__file__).resolve().parents[3]
VENDORED = ROOT / "third_party" / "gstack"
PRIVATE_SKILLS_MANIFEST = ROOT / "vendor" / "skills" / "MANIFEST.yaml"
RISK = "No bakery has yet paid for order intake."
SKIP_TEXT = "Skip — run the review with what you have"


@pytest.mark.parametrize("snapshot", [{"audience": "First-time parents in the US", "offer": "Baby sleep app; free + $6/mo"},
    {"audience": "Students"}, {"summary": "Consumer app free + $6/mo"}])
def test_u9_consumer_q3_in_model_and_fallback(snapshot):
    from app.gstack import office_hours as oh
    raw = json.dumps({"questions": [{"id": "Q1", "question": "What evidence of demand do you have?", "options": ["No one yet", "A few people"]},
        {"id": "Q3", "question": "What's their title? What gets them promoted or fired?", "options": ["A broad category (SMBs, marketers, developers)", "A specific role"]}]})
    for picks in (oh.default_picks(snapshot), oh.parse_picks(raw, snapshot)):
        question = next(pick for pick in picks if pick["gstack_id"] == "Q3")
        assert "life situation" in question["text"] and "where they look for help" in question["text"]
        assert not re.search(r"promot|fired|SMBs|marketers|developers", json.dumps(question), re.I)
    assert "promoted" in next(pick for pick in oh.default_picks({"audience": "Bakery owners"}) if pick["gstack_id"] == "Q3")["text"]


@pytest.fixture(autouse=True)
def legacy_flow(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "new_user_flow", "off")


def reply(payload):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=500, completion_tokens=300, total_tokens=800), model="DeepSeek-V4-Pro")


def questions_reply(snapshot=None):
    from app.idea.classify import is_consumer
    if snapshot and is_consumer(snapshot):
        return reply({"questions": [
            {"id": "Q1", "question": "What evidence do you have that parents need help with toddler dinners?", "options": ["No evidence yet", "Some evidence to describe", "A commitment to describe"]},
            {"id": "Q2", "question": "What do parents use today to plan toddler dinners?", "options": ["Find recipes on BBC Good Food", "Save dinner ideas on Pinterest", "Ask a WhatsApp parenting group"]},
            {"id": "Q3", "question": "When does planning toddler dinners become urgent for parents?", "options": ["A toddler rejects the dinner you prepared", "You need a shopping list for the coming week", "A busy evening leaves little time to cook"]}]})
    return reply({"questions": [
        {"id": "Q1", "question": "Has any bakery paid or begged for an order board?", "options": ["A customer pays already", "Only interested", "Not yet"]},
        {"id": "Q2", "question": "How do bakeries track custom cake orders today?", "options": ["Email and paper", "A shared spreadsheet"]},
        {"id": "Q4", "question": "What is the smallest board a bakery would pay for this week?", "options": ["Order form only", "Form plus reminders"]}]})


@pytest.mark.parametrize("snapshot", [
    {"idea": "A board for freight dispatchers to handle delayed deliveries", "audience": "freight dispatchers",
     "problem": "delayed deliveries", "business_model": "b2b"},
    {"idea": "A route planner for weekend hikers to avoid closed trails", "audience": "weekend hikers",
     "problem": "closed trails", "business_model": "b2c"},
])
def test_office_hours_rejects_unsourced_statistics_and_recommendations(snapshot):
    from app.gstack import office_hours as oh

    options = ["You lose 15-25% of your time to interruptions.", "You can describe a specific incident.", "You have not asked yet."]
    raw = json.dumps({"questions": [
        {"id": "Q2", "question": "What do you use to handle this problem today?", "options": options},
        {"id": "Q3", "question": "What makes this problem urgent for you?", "options": options},
    ]})
    assert oh.parse_picks(raw, snapshot) == oh.default_picks(snapshot)
    question = {**oh.default_picks(snapshot)[2], "options": options, "contextual_options": True,
                "recommended_option": options[0], "recommendation_reason": "15-25% interruption rates are commonly cited."}
    rendered = oh.render_question(question, snapshot)
    assert options[0] not in rendered["options"]
    assert not rendered["recommended_option"] and not rendered["recommendation_reason"]
    safe = oh.default_picks(snapshot)[2]
    for claim in ["15-25% rates are commonly cited.", "These rates are commonly cited.", "You lose twenty percent of your time.", "You lose half your time."]:
        rendered = oh.render_question({**safe, "recommended_option": safe["options"][0], "recommendation_reason": claim}, snapshot)
        assert not rendered["recommendation_reason"]
        assert oh._unsupported_claim(claim, {**snapshot, "idea": snapshot["idea"] + ". You have 15-25 colleagues."})
    stated = options[0]
    grounded = {**snapshot, "founder_facts": {"problem": {"source": "founder message", "quote": stated, "value": stated}}}
    assert not oh._unsupported_claim(stated, grounded)
    assert oh._unsupported_claim(stated, {**grounded, "founder_facts": {"problem": {"source": "website", "quote": stated}}})
    pending = oh.render_question({**question, "detail_pending": True}, snapshot)
    assert options[0] not in pending["options"] and not pending["recommendation_reason"]
    assert "commonly cited" in oh.messages_for(snapshot)[0]["content"]


def critique_reply(data):
    rid = data["founder_revision_source"]["id"]
    answers = data["gstack_answers"]
    forcing = [{"question_id": a["id"], "question": a["question"], "founder_answer": "", "assessment": "Answer noted as testimony.",
                "risk": RISK, "suggested_test": "Ask one bakery to prepay a trial."} for a in answers]
    if not forcing:
        forcing = [{"question_id": "Q1", "question": "What is the strongest evidence of demand?", "founder_answer": "",
                    "assessment": "No demand answer was given.", "risk": RISK, "suggested_test": "Ask one bakery to prepay a trial."}]
    return reply({"verdict": "needs_evidence", "summary": "A promising wedge that still needs a paying customer.",
        "findings": [{"method": m, "assessment": "You know a real bakery problem.", "source_ids": [rid]} for m in data["requested_methods"]],
        "gaps": ["No customer payment evidence was supplied."],
        "proposed_tests": [{"hypothesis": "A bakery prepays a trial", "action": "Ask one bakery for a paid trial", "success_measure": "One prepayment"}],
        "questions": [], "strengths": ["You already know the customer's workaround."], "biggest_risk": RISK,
        "forcing_questions": forcing})


@pytest.fixture()
def empty_brain_memory(monkeypatch):
    from importlib import import_module

    source = Path("app/gbrain/think.py")
    if not (COMPANY_OS / source).is_file():
        return
    think = import_module(".".join(source.with_suffix("").parts))

    async def gather(**kwargs):
        return think.Gathered()
    monkeypatch.setattr(think, "gather", gather)


@pytest.fixture()
def seen(monkeypatch, empty_brain_memory):
    requests = []

    def callback(messages, kwargs):
        requests.append(messages)
        if "short GStack office-hours session" in messages[0]["content"]:
            return questions_reply(json.loads(messages[-1]["content"])["founder_idea"])
        data = json.loads(messages[-1]["content"])
        if "requested_methods" in data:
            return critique_reply(data)
        return completion(proposal=data["force_propose"], case=data.get("explicit_case") or "know", understanding=data["understanding"])
    install_provider(monkeypatch, callback)
    return requests


def start(client, idea=None):
    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "PATCH", base + "/revision", json={"patch": idea or IDEA}).status_code == 200
    assert request(client, "POST", base + "/propose").status_code == 200
    run_queued("idea.clarify_turn")
    looks_right(client, vid, base)  # GStack starts only from the founder's explicit Looks right
    run_queued("idea.validate")  # the office-hours question stage
    return vid, wid, base


@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("idea", [
    {"summary": "Track equipment inspections", "offer": "An inspection checklist",
     "customer": "equipment rental operators", "problem": "missing inspection records"},
    {"summary": "Plan community garden visits", "offer": "A shared garden calendar",
     "customer": "community gardeners", "problem": "missed watering visits"},
], ids=["b2b", "consumer"])
def test_answered_questions_use_saved_sample_review_without_provider(client, seen, monkeypatch, idea, mode):
    from tests.company_os_harness import company_os_test_client
    from sqlalchemy import select
    from app.core import providers_local
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ResearchFinding, ValidationRun
    from app.gstack.office_hours import brief
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    if mode == "local":
        monkeypatch.setenv("CONSOLE_ORIGIN", "http://127.0.0.1")
    client = company_os_test_client(client.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    root = providers_local._root()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "sample"
    marker.write_bytes(b"1")
    marker.chmod(0o600)
    for name in ("LLM_OFFLINE", "LLM_CASSETTE_MODE", "GUARDRAILS_MODE", "EMAIL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    vid, wid, base = start(client, idea)
    with db()() as session:
        ledger_before = session.scalars(select(CreditLedgerEntry.id)).all()
    answers = []
    for _ in range(6):
        saved = request(client, "GET", base).json()
        question = saved["turns"][-1]
        if question["kind"] != "question":
            break
        assert question["options"]["phase"] == "gstack"
        text = f"I would interview {idea['customer']} about {idea['problem']}. I have not contacted anyone yet."
        answers.append(text)
        response = request(client, "POST", base + "/messages", json={"text": text})
        assert response.status_code == 200, response.text
    assert answers
    remaining = request(client, "GET", base).json()["turns"][-1]
    assert remaining["kind"] != "question", remaining["options"]
    job_id = run_queued("idea.validate")
    saved = request(client, "GET", base).json()
    assert not saved["current_revision"]["structured"].get("_questions_skipped")
    assert [answer["answer"] for answer in saved["gstack_answers"] if not answer.get("reused")] == answers
    reused = [answer for answer in saved["gstack_answers"] if answer.get("reused")]
    if mode == "local":
        assert [answer["answer"] for answer in reused] == [idea["customer"]]
        assert all(answer["source"].startswith("Your ") for answer in reused)
    assert all(not answer["skipped"] for answer in saved["gstack_answers"])
    with db()() as session:
        job = session.get(Job, job_id)
        assert job.status == "succeeded", job.last_error
        runs = session.scalars(select(ValidationRun).where(ValidationRun.venture_id == vid)).all()
        assert len(runs) == 3
        reports = [json.loads(run.report) for run in runs]
        if mode == "local":
            assert not seen
            assert session.scalars(select(CreditLedgerEntry.id)).all() == ledger_before
            assert all(report["sample"] is True and report["verified"] is False for report in reports)
            assert all(run.verdict == "needs_evidence" and run.founder_decision is None for run in runs)
            assert all(report["usage"]["credits_micro"] == 0 for report in reports)
            assert all("sample research" in report["summary"].lower() for report in reports)
            assert all([question["founder_answer"] for question in report["forcing_questions"]] ==
                [answer["answer"] for answer in saved["gstack_answers"]] for report in reports)
            assert "sample research" in brief(session, saved["id"], saved["current_revision"]["id"])["source"].lower()
            sources = session.scalars(select(ResearchFinding).where(ResearchFinding.venture_id == vid,
                ResearchFinding.source == "Sample")).all()
            assert len(sources) == 3
            assert all(json.loads(source.evidence)["evidence_kind"] == "sample"
                and json.loads(source.evidence)["page_verified"] is False for source in sources)
        else:
            assert seen and all(not report.get("sample") for report in reports)
    if mode == "local":
        messages = request(client, "GET", f"/ventures/{vid}/chat").json()["items"]
        assert any(message["kind"] == "idea_validation_ready" and "sample" in message["body_md"].lower() for message in messages)
    confirmed = request(client, "POST", base + "/confirm", json={
        "revision_id": saved["current_revision"]["id"], "start_research": False})
    assert confirmed.status_code == 200, confirmed.text
    assert request(client, "GET", base).json()["state"] == "confirmed"


@pytest.mark.parametrize("idea,answer", [
    ({"summary": "An app that schedules industrial heat pumps", "offer": "Schedule industrial heat pumps",
      "customer": "energy managers at food manufacturing plants", "problem": "high peak electricity bills"},
     "I would ask food manufacturing energy managers for short research interviews through industry associations and personal introductions, and I have not contacted anyone yet."),
    ({"summary": "A meal planner for parents", "offer": "Plan toddler dinners", "customer": "parents",
      "problem": "planning toddler dinners"},
     "I would ask parents for short research interviews through cooking groups and personal introductions, and I have not contacted anyone yet."),
])
def test_requested_next_question_survives_notice_cap(client, seen, idea, answer):
    from sqlalchemy import select
    from app.chat.milestones import DAILY_MESSAGE_LIMIT, post_milestone
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack import office_hours
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    vid, wid, base = start(client, idea)
    row_id = base.rsplit("/", 1)[-1]
    append_message(vid, role="user", body_md=answer)
    with db()() as session:
        row = session.get(IdeaSession, row_id)
        pending = office_hours.pending(session, row)
        assert pending is not None
        original, _ = pending
        next_turn = ClarifierTurn(id=new_id("ict"), session_id=row.id, venture_id=vid, workspace_id=wid,
            revision_id=original.revision_id, turn=original.turn + 1, role="agent", kind="question",
            text=original.text, options=original.options, created_at=utcnow())
        session.add(next_turn)
        session.flush()
        session.add_all(ChatMessage(id=new_id("msg"), venture_id=vid, workspace_id=wid,
            role="assistant", kind="text", body_md="Your saved reply. Ask your next question.", created_at=utcnow())
            for _ in range(DAILY_MESSAGE_LIMIT))
        session.flush()
        assert not post_milestone(vid, key="unrequested-notice", body_md="Review your saved work.",
            kind="task_completed", session=session)
        office_hours.chat_question(session, row, next_turn)
        session.flush()
        delivered = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid,
            ChatMessage.kind == "idea_clarification_ready")).all()
        assert any(next_turn.id in (message.actions_json or "") for message in delivered)
        count = len(delivered)
        office_hours.chat_question(session, row, next_turn)
        session.flush()
        assert len(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid,
            ChatMessage.kind == "idea_clarification_ready")).all()) == count


@pytest.mark.parametrize("domain,offer,audience", [
    ("invoice-harbor.io", "Match supplier invoices", "Finance teams at manufacturers"),
    ("pantry-bloom.app", "Save recipes and plan dinners", "Home cooks planning weekly dinners"),
])
@pytest.mark.parametrize("new_flow", ["on", "off"])
def test_live_product_customer_goal_does_not_autostart_pressure_testing(client, monkeypatch, seen, domain, offer, audience, new_flow):
    from app.config import settings
    from app.db.models import Job, Venture
    from app.state import db

    monkeypatch.setattr(settings, "new_user_flow", new_flow)
    vid, _, sid = venture(client)
    with db()() as session:
        session.get(Venture, vid).raw_idea = f"I run {domain}. Help me get more paying customers."
        session.commit()
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": {**IDEA, "offer": offer, "customer": audience}})
    request(client, "POST", base + "/propose")
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    assert saved["state"] == "reviewing"
    assert not any(isinstance(turn.get("options"), dict) and turn["options"].get("phase") == "gstack" for turn in saved["turns"])
    assert not any("short GStack office-hours session" in messages[0]["content"] for messages in seen)
    with db()() as session:
        assert not session.query(Job).filter(Job.venture_id == vid, Job.kind == "idea.validate").count()


def test_new_flow_proposal_starts_gstack_without_another_founder_approval(client, monkeypatch, seen):
    from app.config import settings
    monkeypatch.setattr(settings, "new_user_flow", "on")
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    request(client, "POST", base + "/propose")
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    assert saved["state"] == "validating"
    question = saved["turns"][-1]
    assert question["kind"] == "question" and question["text"].strip()
    assert question["options"]["phase"] == "gstack"
    assert saved["gstack_answers"] == []


@pytest.mark.parametrize("idea", [
    {"summary": "Track delayed deliveries for freight dispatchers", "problem": "delayed delivery updates",
     "customer": "freight dispatchers", "offer": "An order tracking board for freight dispatchers"},
    {"summary": "Plan safe walking routes for weekend hikers", "problem": "planning safe walking routes",
     "customer": "weekend hikers", "offer": "A route planner for weekend hikers"},
])
def test_new_flow_invalid_picker_resumes_before_clarification_job_succeeds(client, monkeypatch, idea):
    from app.config import settings
    from app.core.jobs import RetryableError
    from app.db.models import Job
    from app.idea import validation
    from app.state import db

    monkeypatch.setattr(settings, "new_user_flow", "on")
    calls = []

    def provider(messages, kwargs):
        calls.append(messages)
        return completion(proposal=True, understanding=idea)

    install_provider(monkeypatch, provider)
    original_questions = validation._run_questions
    interrupted = False

    def questions(payload):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RetryableError("question_stage_interrupted")
        return original_questions(payload)

    monkeypatch.setattr(validation, "_run_questions", questions)
    created = request(client, "POST", "/ventures", json={"raw_idea": idea["summary"], "start": False}).json()
    vid = created["id"]
    sid = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()["id"]
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "PATCH", base + "/revision", json={"patch": idea}).status_code == 200
    assert request(client, "POST", base + "/propose").status_code == 200
    job_id = run_queued("idea.clarify_turn")
    with db()() as session:
        job = session.get(Job, job_id)
        assert job.status == "retry_wait"
        job.run_after = job.scheduled_for = None
        session.commit()
    run_queued("idea.clarify_turn")
    saved = request(client, "GET", base).json()
    question = saved["turns"][-1]
    assert saved["state"] == "validating" and question["kind"] == "question"
    assert question["options"]["phase"] == "gstack"
    assert len(question["options"]["options"]) == 3
    assert all(idea["customer"] not in option and idea["problem"] not in option for option in question["options"]["options"])
    assert question["options"]["requires_detail"] == question["options"]["options"][1:]
    assert not _gstack_jobs(vid)
    call_count = len(calls)
    assert any("short GStack office-hours session" in messages[0]["content"] for messages in calls)
    with db()() as session:
        job = session.get(Job, job_id)
        assert job.status == "succeeded"
        job.status = "scheduled"
        session.commit()
    run_queued("idea.clarify_turn")
    assert len(calls) == call_count
    assert [turn["id"] for turn in request(client, "GET", base).json()["turns"]] == [turn["id"] for turn in saved["turns"]]


TINYPLATE = {"summary": "TinyPlate suggests weekly dinners their kids will actually eat",
    "problem": "Picky toddlers reject dinner and busy parents spend hours looking for meals",
    "customer": "Busy US parents of picky toddlers", "offer": "An app that suggests weekly toddler dinners",
    "workflow": "Parents choose meals and get a weekly dinner plan", "first_user_path": "Invite parents from a parenting group",
    "desired_outcome": "Parents pay monthly after trying a dinner plan", "pricing_hypothesis": "$5/month"}


def tinyplate_proposal(client, monkeypatch, *, evidence=None, provenance="founder_stated", evidence_question=None, evidence_options=None):
    from app.config import settings
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    monkeypatch.setattr(settings, "new_user_flow", "on")
    response = request(client, "POST", "/ventures", json={"name": "TinyPlate QA",
        "raw_idea": "A meal-planning app for busy US parents of picky toddlers that suggests weekly dinners their kids will actually eat, $5/month", "start": False})
    assert response.status_code == 201
    vid = response.json()["id"]
    opened = request(client, "POST", f"/ventures/{vid}/idea/sessions")
    assert opened.status_code == 201
    sid = opened.json()["id"]
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "PATCH", base + "/revision", json={"patch": TINYPLATE}).status_code == 200
    if evidence is not None:
        from app.idea.service import _next_turn_number
        rid = _revision(client, base)
        with db()() as session:
            for role, kind, text, options in [
                ("agent", "question", evidence_question or "What's the strongest signal these parents would actively use and pay for TinyPlate?", {"field": "desired_outcome", "options": evidence_options or ["A few people"]}),
                ("founder", "answer", evidence, {"provenance": provenance, "founder_text": evidence if provenance == "founder_stated" else "", "suggested_text": evidence if provenance == "suggested_choice" else ""}),
            ]:
                session.add(ClarifierTurn(id=new_id("ict"), session_id=sid, venture_id=vid,
                    workspace_id=response.json()["workspace_id"], turn=_next_turn_number(session, sid),
                    role=role, kind=kind, text=text, options=json.dumps(options), revision_id=rid, created_at=utcnow()))
                session.flush()
            session.commit()
    assert request(client, "POST", base + "/propose").status_code == 200
    run_queued("idea.clarify_turn")
    return vid, base


@pytest.mark.parametrize("raw_idea_only", [False, True])
def test_u9b_runner_and_saved_question_renders_use_consumer_audience(client, monkeypatch, seen, raw_idea_only):
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.gstack import office_hours as oh
    from app.state import db
    vid, base = tinyplate_proposal(client, monkeypatch)
    if raw_idea_only:
        rid = _revision(client, base)
        with db()() as session:
            revision = session.get(IdeaRevision, rid)
            append_idea_revision(session, revision.id,
                structured=json.dumps({**json.loads(revision.structured), "customer": ""}))
            session.commit()
    for answer in ["No one has tried TinyPlate yet", "Parents ask friends and search online for toddler dinners"]:
        assert request(client, "POST", base + "/messages", json={"text": answer}).status_code == 200
    assert not any(turn["kind"] == "question" and turn["options"].get("gstack_id") == "Q3"
        for turn in request(client, "GET", base).json()["turns"])
    from core.clock import utcnow
    with db()() as session:
        row = session.get(IdeaSession, base.rsplit("/", 1)[-1])
        row.state = "validating"
        legacy = next(pick for pick in oh.default_picks(TINYPLATE) if pick["gstack_id"] == "Q3")
        oh.add_question_turn(session, row, legacy, [], 3, 3, utcnow())
        session.commit()
    question = request(client, "GET", base).json()["turns"][-1]
    assert question["options"]["gstack_id"] == "Q3"
    assert "picky toddlers" in question["text"].lower() and "urgent" in question["text"]  # consumer audience; asks urgency, never re-asks the audience
    with db()() as session:
        turn = session.get(ClarifierTurn, question["id"])
        opts = json.loads(turn.options)
        opts.update(options=["A broad category (SMBs, marketers, developers)", "A specific role"],
                    why="Job titles and promotions")
        turn.text, turn.options = "What's their title? What gets them promoted/fired?", json.dumps(opts)
        session.commit()
    for text in ["What's their title? What gets them promoted/fired?", " "]:
        with db()() as session:
            session.get(ClarifierTurn, question["id"]).text = text
            session.commit()
        rendered = request(client, "GET", base).json()["turns"][-1]
        body, actions = oh.question_reply(question["id"])
        assert "picky toddlers" in rendered["text"].lower() and "picky toddlers" in body.lower()
        assert not re.search(r"promot|fired|SMBs|marketers|developers", json.dumps([rendered, body, actions]), re.I)
    response = request(client, "POST", base + "/messages", json={"text": "not sure"})
    assert response.status_code == 200 and response.json()["job_id"]
    saved = request(client, "GET", base).json()
    assert saved["turns"][-1]["role"] == "founder" and saved["turns"][-1]["text"] == "not sure"
    assert saved["gstack_answers"][-1]["assumed"]


def test_u27_runner_status_quo_and_queue_use_consumer_workarounds(client, monkeypatch, seen):
    from app.gstack import office_hours as oh
    from app.state import db
    vid, base = tinyplate_proposal(client, monkeypatch)
    saved = request(client, "GET", base).json()
    queued = saved["turns"][-1]["options"]["queue"][0]
    assert queued["gstack_id"] == "Q2"
    with db()() as session:
        snapshot = oh.saved_snapshot(session, vid, saved["current_revision"]["id"])
    assert queued["options"] == oh.contextual_choices("Q2", snapshot)
    assert request(client, "POST", base + "/messages", json={"text": "No parents have tried TinyPlate yet"}).status_code == 200
    question = request(client, "GET", base).json()["turns"][-1]
    body, actions = oh.question_reply(question["id"])
    assert question["options"]["gstack_id"] == "Q2"
    assert question["options"]["options"] == queued["options"]
    assert not re.search(r"spreadsheet|inbox|agency|duct-taped", json.dumps([queued, question, body, actions]), re.I)
    raw = json.dumps({"questions": [{"id": "Q1", "question": "What demand evidence do you have?", "options": ["No one yet", "A few people"]},
        {"id": "Q2", "question": "Which agency do they hire?", "options": ["A spreadsheet", "An agency"]}]})
    fallback = next(pick for pick in oh.parse_picks(raw, TINYPLATE) if pick["gstack_id"] == "Q2")
    assert len(fallback["options"]) == 3 and all("parents" not in option.lower() for option in fallback["options"])


@pytest.mark.parametrize("evidence,provenance,reused", [
    ("Four parents tried my dinner plan and two offered to pay $5/month", "founder_stated", True),
    ("No one has used or paid for it yet", "founder_stated", True),
    ("A few people", "suggested_choice", True),
    ("not sure", "founder_stated", False),
    ("Skip", "founder_stated", False),
])
def test_u26_runner_reuses_clarification_evidence_without_promoting_suggestions(client, monkeypatch, seen, evidence, provenance, reused):
    from app.gstack import office_hours as oh
    from app.db.supymem_models import IdeaSession
    from app.state import db
    _, base = tinyplate_proposal(client, monkeypatch, evidence=evidence, provenance=provenance)
    saved = request(client, "GET", base).json()
    question = saved["turns"][-1]
    assert question["options"]["gstack_id"] == "Q2"
    assert question["options"]["total"] == 1
    with db()() as session:
        row = session.get(IdeaSession, saved["id"])
        assert [pick["gstack_id"] for pick in oh.unanswered_picks(session, row, oh.default_picks(TINYPLATE))] == ["Q2"]
    if reused:
        answer = saved["gstack_answers"][0]
        assert answer["id"] == "Q1" and answer["answer"] == evidence
        assert answer["provenance"] == provenance and answer["assumed"] is (provenance == "suggested_choice")
        skipped = request(client, "POST", base + "/messages", json={"text": "Skip the questions"})
        assert skipped.status_code == 200 and skipped.json()["job_id"] is None
        final = request(client, "GET", base).json()
        assert final["state"] == "reviewing" and final["validation_runs"] == []
        demand = final["gstack_answers"][0]
        assert demand == answer
    else:
        assert not any(answer["id"] == "Q1" for answer in saved["gstack_answers"])


def test_u66_q1_workaround_skips_q2_and_preserves_founder_provenance(client, seen):
    vid, _, base = start(client)
    text = "Two GMs lose 2-3 hours on the rota. One pays $40/month for 7shifts but still swaps by text. Nobody has paid us yet."
    response = request(client, "POST", base + "/messages", json={"text": text})
    assert response.status_code == 200
    saved = request(client, "GET", base).json()
    assert saved["turns"][-1]["options"]["gstack_id"] == "Q4"
    answer = next(answer for answer in saved["gstack_answers"] if answer["id"] == "Q2")
    assert answer["answer"] == "One pays $40/month for 7shifts but still swaps by text."
    assert answer["provenance"] == "founder_stated" and not answer["assumed"]
    assert "Nothing exists" not in response.text


@pytest.mark.parametrize("question", ["Which best describes your progress?", "When do first-time dog owners feel most frustrated?"])
@pytest.mark.parametrize("evidence", ["Some evidence to describe", "No evidence yet"])
def test_u26_evidence_options_reuse_q1_regardless_of_legacy_wording(client, monkeypatch, seen, question, evidence):
    _, base = tinyplate_proposal(client, monkeypatch, evidence=evidence, provenance="suggested_choice",
        evidence_question=question, evidence_options=["No evidence yet", "Some evidence to describe"])
    saved = request(client, "GET", base).json()
    assert saved["turns"][-1]["options"]["gstack_id"] == "Q2"
    answer = saved["gstack_answers"][0]
    assert answer["id"] == "Q1" and answer["answer"] == evidence
    assert answer["assumed"] and answer["provenance"] == "suggested_choice" and not answer["founder_text"]


@pytest.mark.parametrize("deferred", [False, True])
def test_new_flow_chat_answers_current_gstack_id_without_assuming_the_rest(client, monkeypatch, seen, deferred):
    from app.config import settings
    from app.db.models import Workspace
    from app.state import db
    monkeypatch.setattr(settings, "new_user_flow", "on")
    vid, wid, sid = venture(client)
    with db()() as session:
        workspace = session.get(Workspace, wid)
        workspace.settings_json = json.dumps({"chat_entry_defer_clarification": deferred, "entry_goal": "startup"})
        session.commit()
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    request(client, "PATCH", base + "/revision", json={"patch": IDEA})
    request(client, "POST", base + "/propose")
    run_queued("idea.clarify_turn")
    answers = ["Three bakeries have tried a spreadsheet version and asked for a shared order board",
               "Bakeries use paper and spreadsheets for their custom cake orders",
               "Bakery owners need a shared order board for their growing custom cake orders"]
    for index, answer in enumerate(answers):
        saved = request(client, "GET", base).json()
        turns = saved["turns"]
        assert len({turn["turn"] for turn in turns}) == len(turns)
        question = turns[-1]
        assert question["options"]["index"] == index + 1
        response = request(client, "POST", f"/ventures/{vid}/chat", json={
            "message": answer, "question_id": question["id"], "reply_to_question": True})
        assert response.status_code == 200, response.text
        assert "already answered" not in response.text
        saved = request(client, "GET", base).json()
        assert len(saved["gstack_answers"]) == index + 1
        assert all(not item["assumed"] for item in saved["gstack_answers"])
    run_queued("idea.validate")
    saved = request(client, "GET", base).json()
    assert saved["validation_runs"] and all(run["state"] == "complete" for run in saved["validation_runs"])
    assert saved["next_href"] == f"/v/{vid}/context"
    from app.chat.store import list_messages
    from app.tenancy import bind
    with bind(workspace_id=wid, venture_id=vid):
        validation = next(message for message in reversed(list_messages(vid)) if message["kind"] == "idea_validation_ready")
    assert validation["actions"][0]["label"] == "Confirm your idea"
    assert validation["actions"][0]["action"] == "idea.confirm"
    assert not any(action["label"] in {"Continue to research", "Build my landing page now"} for action in validation["actions"])


@pytest.mark.parametrize("idea", [
    {"summary": "Track delayed deliveries for freight dispatchers", "problem": "delayed delivery updates",
     "customer": "freight dispatchers", "offer": "An order tracking board for freight dispatchers"},
    {"summary": "Plan safe walking routes for weekend hikers", "problem": "planning safe walking routes",
     "customer": "weekend hikers", "offer": "A route planner for weekend hikers"},
])
@pytest.mark.parametrize("fails", [False, True], ids=["review-ready", "review-failed"])
def test_final_chat_answer_delivers_review_once_above_notice_cap(client, monkeypatch, seen, idea, fails):
    from sqlalchemy import select
    from app.chat.milestones import DAILY_MESSAGE_LIMIT
    from app.config import settings
    from app.core.jobs import FatalError
    from app.db.models import ChatMessage, Job
    from app.research import metered
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    from tests.idea_fixtures import validation_completion

    monkeypatch.setattr(settings, "new_user_flow", "on")
    strength = f"You have narrowed your offer to {idea['customer']}"
    risk = f"You have not tested demand among {idea['customer']}"
    hypothesis = f"One of your {idea['customer']} will pay to solve {idea['problem']}"

    def provider(messages, kwargs):
        if "short GStack office-hours session" in messages[0]["content"]:
            return reply({"questions": [
                {"id": "Q1", "question": "What evidence of demand do you have?", "options": ["No evidence yet", "Interest to describe"]},
                {"id": "Q2", "question": f"How do {idea['customer']} handle {idea['problem']} today?",
                 "options": ["A manual workaround", "Another tool"]},
            ]})
        if "requested_methods" in json.loads(messages[-1]["content"]):
            response = validation_completion(messages)
            report = json.loads(response.choices[0].message.content)
            report.update(strengths=[strength], biggest_risk=risk)
            report["proposed_tests"][0]["hypothesis"] = hypothesis
            return reply(report)
        return completion(proposal=True, understanding=idea, message=idea["summary"])

    calls = install_provider(monkeypatch, provider)
    created = request(client, "POST", "/ventures", json={"raw_idea": idea["summary"], "start": False}).json()
    vid = created["id"]
    sid = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()["id"]
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "PATCH", base + "/revision", json={"patch": idea}).status_code == 200
    assert request(client, "POST", base + "/propose").status_code == 200
    run_queued("idea.clarify_turn")
    for index in range(2):
        question = request(client, "GET", base).json()["turns"][-1]
        assert question["options"]["index"] == index + 1 and question["options"]["total"] == 2
        response = request(client, "POST", f"/ventures/{vid}/chat", json={
            "message": f"I spoke with {idea['customer']} about {idea['problem']} and their manual workaround",
            "question_id": question["id"], "reply_to_question": True})
        assert response.status_code == 200, response.text
    pending = response.json()
    assert pending["kind"] == "idea_clarification_pending" and "reviewing your idea" in pending["body_md"]
    with db()() as session:
        conversation_id = session.get(ChatMessage, pending["id"]).conversation_id
        session.add_all(ChatMessage(id=new_id("msg"), venture_id=vid, workspace_id=created["workspace_id"],
            conversation_id=conversation_id, role="assistant", kind="text", body_md="Your saved chat history.",
            created_at=utcnow()) for _ in range(DAILY_MESSAGE_LIMIT))
        session.commit()
    if fails:
        def fail_review(*args, **kwargs):
            raise FatalError("validation_invalid_output")
        monkeypatch.setattr(metered, "synthesize", fail_review)
    job_id = run_queued("idea.validate")
    kind = "idea_validation_failed" if fails else "idea_validation_ready"
    with db()() as session:
        job = session.get(Job, job_id)
        assert job.status == ("failed_dead" if fails else "succeeded"), job.last_error
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == kind)).all()
        assert len(messages) == 1
        message = messages[0]
        assert message.conversation_id == conversation_id
        actions = json.loads(message.actions_json)
        assert "\u2014" not in message.body_md and "the founder" not in message.body_md.lower()
        if fails:
            assert "did not finish" in message.body_md and "your saved answers" in message.body_md
            assert {"job.retry", "recovery.alternative"} <= {action.get("action") for action in actions}
            assert any(action["label"] == "Do it another way" for action in actions)
        else:
            assert hypothesis in message.body_md and risk not in message.body_md
            assert all(label in message.body_md for label in ("What we found", "Your opening", "What I'd do next"))
            assert any(action.get("action") == "idea.confirm" for action in actions)
        message_id = message.id
        job.status = "scheduled"
        session.commit()
    call_count = len(calls)
    run_queued("idea.validate")
    assert len(calls) == call_count
    with db()() as session:
        assert session.scalars(select(ChatMessage.id).where(ChatMessage.venture_id == vid, ChatMessage.kind == kind)).all() == [message_id]


def _revision(client, base):
    return request(client, "GET", base).json()["current_revision"]["id"]


def looks_right(client, vid, base, revision_id=None):
    payload = {"session_id": base.rsplit("/", 1)[1], "revision_id": revision_id or _revision(client, base)}
    response = request(client, "POST", f"/ventures/{vid}/chat/confirm",
                       json={"action": "idea.proposal.approve", "payload": payload})
    assert response.status_code == 200, response.text
    return response.json()


def _gstack_jobs(vid):
    from sqlalchemy import select
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        return [j for j in session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "idea.validate")).all()
                if json.loads(j.payload_json).get("stage") == "gstack_questions"]


def proposed(client):
    vid, wid, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    assert request(client, "PATCH", base + "/revision", json={"patch": IDEA}).status_code == 200
    assert request(client, "POST", base + "/propose").status_code == 200
    run_queued("idea.clarify_turn")
    return vid, base


def test_proposal_without_looks_right_never_queues_gstack(client, seen):
    vid, base = proposed(client)
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing" and state["turns"][-1]["kind"] == "proposal"
    assert _gstack_jobs(vid) == []
    from app.chat.idea_proposal import proposal_actions
    actions = proposal_actions("ids_x", "rev_x")
    assert [a["action"] for a in actions] == ["idea.proposal.approve", "idea.proposal.edit"]
    assert not any("href" in a for a in actions)  # both stay in chat; neither jumps to the Idea page


def test_looks_right_once_queues_one_gstack_run_and_replays_are_no_ops(client, seen):
    vid, base = proposed(client)
    rid = _revision(client, base)
    first = looks_right(client, vid, base, rid)
    assert "starting the review" in first["body_md"] and first["kind"] == "idea_clarification_pending"
    assert len(_gstack_jobs(vid)) == 1
    second = looks_right(client, vid, base, rid)  # double-click
    assert "already approved" in second["body_md"]
    assert len(_gstack_jobs(vid)) == 1
    run_queued("idea.validate")
    looks_right(client, vid, base, rid)  # replay after the run finished
    assert len(_gstack_jobs(vid)) == 1


@pytest.mark.parametrize("summary,customer,relevant", [
    ("B2B SaaS developer tools customer feedback manual beta workflow", "API platform teams", True),
    ("Unrelated quantum neutrino crystals", "Neutrino physicists", False),
])
def test_summary_and_looks_right_show_only_relevant_offline_references(client, seen, monkeypatch,
                                                                    summary, customer, relevant):
    from app.chat.store import list_messages
    from app.knowledge import pmf, yc_video_advice

    monkeypatch.setattr("app.idea.service._chat_entry_deferred", lambda *args, **kwargs: True)
    vid, _, sid = venture(client)
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    idea = {**IDEA, "summary": summary, "customer": customer, "problem": summary,
            "offer": summary, "workflow": summary, "pricing_hypothesis": "",
            "first_user_path": customer, "desired_outcome": ""}
    assert request(client, "PATCH", base + "/revision", json={"patch": idea}).status_code == 200
    assert request(client, "POST", base + "/propose").status_code == 200
    run_queued("idea.clarify_turn")
    messages = list_messages(vid)
    proposal = next(message for message in reversed(messages) if message["kind"] == "idea_clarification_ready")
    assert [action["label"] for action in proposal["actions"]] == ["Looks right", "Edit"]
    assert _gstack_jobs(vid) == []
    calls = len(seen)
    approved = looks_right(client, vid, base)
    assert len(seen) == calls and len(_gstack_jobs(vid)) == 1
    for message in (proposal, approved, list_messages(vid)[-1]):
        assert bool(message.get("founder_references")) is relevant
        assert message["citations"] == []
        if not relevant:
            continue
        references = message["founder_references"]
        assert 2 <= len(references["startups"]) <= 4
        assert 1 <= len(references["yc_advice"]) <= 2
        assert {case["name"] for case in references["startups"]}.isdisjoint({"Ring", "Canva", "Airbnb"})
        for case in references["startups"]:
            row = pmf.get_record(case["id"])
            values = pmf.fields(row)
            assert case["what_they_did"] == row["documented_action"]
            assert case["outcome"] == values["Why it worked"]
            assert case["lesson"] == values["Reusable playbook"]
            assert case["url"] in row["source_urls"]
        for advice in references["yc_advice"]:
            row = yc_video_advice.get_card(advice["id"])
            assert row["venture_stage"] in {"idea", "mixed", "n/a"}
            assert advice["title"] == row["title"] and advice["line"] == row["action_discussed"]
            assert advice["url"] == row["source_video"]["url"] and "&t=" not in advice["url"]
    replay = looks_right(client, vid, base)
    assert "founder_references" not in replay and len(_gstack_jobs(vid)) == 1


def test_stale_proposal_is_rejected(client, seen):
    vid, base = proposed(client)
    stale = looks_right(client, vid, base, "rev_not_current")
    assert "replaced by a newer version" in stale["body_md"]
    assert _gstack_jobs(vid) == []


def test_edit_stays_in_chat_changes_revision_and_requires_fresh_looks_right(client, seen, monkeypatch):
    monkeypatch.setattr("app.chat.idea_bridge._chat_entry_deferred", lambda *a, **k: True)  # chat-started venture
    vid, base = proposed(client)
    sid, old = base.rsplit("/", 1)[1], _revision(client, base)
    edit = request(client, "POST", f"/ventures/{vid}/chat/confirm",
                   json={"action": "idea.proposal.edit", "payload": {"session_id": sid, "revision_id": old}})
    assert edit.status_code == 200 and "What should change" in edit.json()["body_md"]
    turn = request(client, "GET", base).json()["turns"][-1]
    assert turn["kind"] == "question" and turn["role"] == "agent"
    assert "replaced by a newer version" in looks_right(client, vid, base, old)["body_md"]  # Edit voids the old approval
    # The founder types the correction in chat; it answers the saved Edit question.
    from app.chat.idea_bridge import pending_question
    pending = pending_question(vid, "The customer is independent bakeries with two to five staff members", force=True)
    assert pending is not None and pending[1] == turn["id"]
    assert request(client, "POST", base + "/messages", json={"text": "The customer is independent bakeries with two to five staff"}).status_code == 200
    assert request(client, "POST", base + "/messages", json={"text": "just propose"}).status_code == 200
    run_queued("idea.clarify_turn")
    state = request(client, "GET", base).json()
    new = state["current_revision"]["id"]
    assert new != old and state["state"] == "reviewing" and state["turns"][-1]["kind"] == "proposal"
    assert "replaced by a newer version" in looks_right(client, vid, base, old)["body_md"]
    assert _gstack_jobs(vid) == []
    looks_right(client, vid, base, new)
    assert len(_gstack_jobs(vid)) == 1


def test_vendored_licence_pin_and_questions_come_from_the_skill_text():
    from app.gstack import methodology as m
    assert "MIT License" in (VENDORED / "LICENSE").read_text() and "Garry Tan" in (VENDORED / "LICENSE").read_text()
    notice = (VENDORED / "NOTICE").read_text()
    assert "Garry Tan" in notice
    assert re.fullmatch(r"[0-9a-f]{40}", m.pinned_sha()) and m.pinned_sha() in notice
    skill = (VENDORED / "openclaw/skills/gstack-openclaw-office-hours/SKILL.md").read_text()
    asks = re.findall(r'\*\*Ask:\*\* "([^"]+)"', skill)
    assert len(asks) == 6
    assert [q["prompt"] for q in m.FORCING_QUESTIONS.values()] == asks
    assert "Specificity is the only currency" in m.office_hours_text()
    assert "Zero silent failures" in m.CEO_PRIME_DIRECTIVES[0]
    assert "Zero silent failures" in (VENDORED / "openclaw/skills/gstack-openclaw-ceo-review/SKILL.md").read_text()


@pytest.mark.skipif(not PRIVATE_SKILLS_MANIFEST.is_file(), reason="Private skills manifest is not in the public export")
def test_vendored_pin_matches_private_manifest():
    from app.gstack import methodology as m

    assert m.pinned_sha() in PRIVATE_SKILLS_MANIFEST.read_text()


def test_office_hours_questions_then_answers_then_critique_and_feed_forward(client, monkeypatch, seen):
    vid, wid, base = start(client)
    state = request(client, "GET", base).json()
    assert state["state"] == "validating" and not state["validation_runs"]
    turn = state["turns"][-1]
    assert turn["kind"] == "question" and turn["options"]["phase"] == "gstack" and turn["options"]["gstack_id"] == "Q1"
    assert turn["options"]["index"] == 1 and turn["options"]["total"] == 3
    assert 2 <= len(turn["options"]["options"]) <= 4 and turn["options"]["heading"].startswith("Let's pressure-test")
    picker_system = next(m[0]["content"] for m in seen if "short GStack office-hours session" in m[0]["content"])
    assert "Specificity is the only currency" in picker_system  # the real skill text drives the picker

    from sqlalchemy import select
    from app.db.models import ChatMessage
    from app.state import db
    with db()() as session:
        posted = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.kind == "idea_clarification_ready")).all()
    assert len(posted) == 1 and "pressure-test your idea" in posted[0].body_md and "GStack" not in posted[0].body_md
    buttons = json.loads(posted[0].actions_json)
    assert [b["text"] for b in buttons][:-1] == turn["options"]["options"] and buttons[-1]["text"].startswith("Skip")

    answers = ["One pays already", "Email and paper", "Order form only"]
    for index, text in enumerate(answers):
        result = request(client, "POST", base + "/messages", json={"text": text})
        assert result.status_code == 200, result.text
        assert bool(result.json()["job_id"]) == (index == 2)
        if index < 2:
            nxt = result.json()["session"]["turns"][-1]
            assert nxt["options"]["phase"] == "gstack" and nxt["options"]["index"] == index + 2
    run_queued("idea.validate")

    state = request(client, "GET", base).json()
    runs = state["validation_runs"]
    assert len(runs) == 3 and all(r["state"] == "complete" for r in runs)
    report = runs[0]["report"]
    assert [f["founder_answer"] for f in report["forcing_questions"]] == answers  # filled from saved answers, not the model
    assert [f["question_id"] for f in report["forcing_questions"]] == ["Q1", "Q2", "Q4"]
    assert report["biggest_risk"] == RISK and report["strengths"]
    critique = next(m for m in seen if "requested_methods" in m[-1]["content"])
    assert json.loads(critique[-1]["content"])["gstack_answers"][0]["answer"] == "One pays already"
    assert "Specificity is the only currency" in critique[0]["content"] and "Zero silent failures" in critique[0]["content"]
    assert "lead with what you found" in critique[0]["content"]

    # Confirm and feed forward into research.
    assert request(client, "POST", base + "/confirm", json={"revision_id": state["current_revision"]["id"]}).status_code == 200
    install_research(monkeypatch)
    from llm_gateway import sole
    inner, research_messages = sole._primary_chat, []

    async def spy(messages, **kwargs):
        research_messages.append(messages)
        return await inner(messages, **kwargs)
    monkeypatch.setattr(sole, "_primary_chat", spy)
    run_queued("research.deep")
    review = next(m for m in research_messages if "You review evidence" in m[0]["content"])
    brief = json.loads(review[-1]["content"])["snapshot"]["gstack_brief"]
    assert brief["biggest_risk"] == RISK and brief["unverified_gaps"][0] == "No customer payment evidence was supplied."
    assert brief["proposed_tests"][0]["hypothesis"] == "A bakery prepays a trial"
    assert "gstack_brief" in review[0]["content"]

    # ...and into the plan prompt as constraints.
    assert request(client, "PUT", f"/ventures/{vid}/resources", json={"assets": [], "complete": True}).status_code == 200
    calls = install_planner(monkeypatch)
    assert request(client, "POST", f"/ventures/{vid}/plans/compile", json={}).status_code == 200
    run_queued("plan.compile")
    planner = calls[0][0]
    context = json.loads(planner[-1]["content"])
    assert context["gstack_brief"]["biggest_risk"] == RISK
    assert context["gstack_brief"]["proposed_tests"][0]["action"] == "Ask one bakery for a paid trial"
    assert "must test the" in planner[0]["content"] and "biggest_risk" in planner[0]["content"]


def test_skip_runs_the_review_with_what_the_founder_has(client, seen):
    vid, wid, base = start(client)
    result = request(client, "POST", base + "/messages", json={"text": SKIP_TEXT})
    assert result.status_code == 200 and result.json()["job_id"]
    run_queued("idea.validate")
    state = request(client, "GET", base).json()
    assert len(state["validation_runs"]) == 3
    critique = next(m for m in seen if "requested_methods" in m[-1]["content"])
    sent = json.loads(critique[-1]["content"])["gstack_answers"]
    assert sent == []  # skipped questions are not sent as answers
    report = state["validation_runs"][0]["report"]
    assert not any(f["founder_answer"] for f in report["forcing_questions"])
    assert report["not_answered"] == [x["name"] for x in state["gstack_answers"]] and len(report["not_answered"]) == 3
    assert any(turn["text"] == "Skip the questions" for turn in state["turns"] if turn["role"] == "founder")
    assert not any(turn["text"] == SKIP_TEXT for turn in state["turns"] if turn["role"] == "founder")


def _founder_turns(client, base):
    return [t["text"] for t in request(client, "GET", base).json()["turns"] if t["role"] == "founder"]


@pytest.mark.parametrize("idea", [None, TINYPLATE])
def test_confused_reply_advances_with_an_assumption_without_repeating(client, seen, idea):
    vid, wid, base = start(client, idea)
    first = request(client, "GET", base).json()["turns"][-1]
    result = request(client, "POST", base + "/messages", json={"text": "I'm confused"})
    assert result.status_code == 200 and not result.json()["job_id"]
    nxt = result.json()["session"]["turns"][-1]
    assert nxt["text"] != first["text"] and "retry" not in nxt["options"]
    assert "I'm confused" in _founder_turns(client, base)
    assert nxt["options"]["index"] == 2 and nxt["options"]["note"].startswith("I assumed")
    assert 2 <= len(nxt["options"]["options"]) <= 3
    assert "idk" not in _founder_turns(client, base)
    assert request(client, "GET", base).json()["gstack_answers"][0]["assumed"]
    from app.gstack import office_hours
    from app.state import db
    with db()() as session:
        got = office_hours.answers_for(session, request(client, "GET", base).json()["id"])
    assert len(got) == 1 and got[0]["assumed"]


@pytest.mark.parametrize("via_chat", [False, True])
def test_explicit_assumed_pick_advances_without_retry_and_preserves_text(client, seen, via_chat):
    vid, wid, base = start(client)
    if via_chat:
        from app.db.models import Workspace
        from app.state import db
        with db()() as session:
            workspace = session.get(Workspace, wid)
            workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"),
                                                  "chat_entry_defer_clarification": True, "entry_goal": "startup"})
            session.commit()
    first = request(client, "GET", base).json()["turns"][-1]
    answer = "Assumed: I'm not sure\nI have not contacted a bakery yet"
    if via_chat:
        result = request(client, "POST", f"/ventures/{vid}/chat", json={
            "message": answer, "question_id": first["id"], "reply_to_question": True})
    else:
        result = request(client, "POST", base + "/messages", json={"text": answer})
    assert result.status_code == 200, result.text
    state = request(client, "GET", base).json()
    assert state["turns"][-1]["options"]["index"] == 2
    assert not state["turns"][-1]["options"].get("retry")
    assert state["turns"][-2]["text"] == answer
    assert state["gstack_answers"][0]["assumed"]
    assert state["gstack_answers"][0]["answer"] == answer
    assert state["turns"][-2]["options"]["provenance"] == "suggested_choice"
    assert state["gstack_answers"][0]["founder_text"] == "I have not contacted a bakery yet"


@pytest.mark.parametrize("legacy_chip", [False, True])
def test_generated_choice_stays_an_assumption_in_validation_and_research(client, seen, legacy_chip):
    vid, wid, base = start(client)
    state = request(client, "GET", base).json()
    first = state["turns"][-1]
    choice = first["options"]["options"][0]
    answer = "Assumed: " + choice
    result = request(client, "POST", base + "/messages", json={"chip": choice} if legacy_chip else {"text": answer})
    assert result.status_code == 200, result.text
    state = result.json()["session"]
    assert state["turns"][-2]["options"]["provenance"] == "suggested_choice"
    assert state["gstack_answers"][0]["assumed"]
    assert state["gstack_answers"][0]["assumption_note"] == "Founder selected this suggested option; treat as an untested assumption"
    typed = "A bakery manager told me their current workaround uses paper orders"
    result = request(client, "POST", base + "/messages", json={"text": typed})
    assert result.status_code == 200, result.text
    assert result.json()["session"]["turns"][-2]["options"]["provenance"] == "founder_stated"
    request(client, "POST", base + "/messages", json={"text": "Order form only"})
    run_queued("idea.validate")
    critique = next(messages for messages in seen if "requested_methods" in messages[-1]["content"])
    payload = json.loads(critique[-1]["content"])
    assert payload["gstack_answers"][0]["provenance"] == "suggested_choice"
    assert "Founder selected this suggested option; treat as an untested assumption" in critique[0]["content"]
    from app.db.supymem_models import ResearchDossier
    from app.gstack.office_hours import brief
    from app.state import db
    from sqlalchemy import select
    state = request(client, "GET", base).json()
    with db()() as session:
        dossier = session.scalars(select(ResearchDossier).where(ResearchDossier.venture_id == vid)).all()[-1]
        snapshot = json.loads(dossier.summary)["_runtime"]["snapshot"]
        assert snapshot["suggested_assumptions"][0]["provenance"] == "suggested_choice"
        assert "untested assumption" in snapshot["assumption_note"]
        context = brief(session, state["id"], state["current_revision"]["id"])
        assert context["suggested_assumptions"][0]["provenance"] == "suggested_choice"


@pytest.mark.parametrize("via_chat", [False, True])
def test_picked_option_and_typed_conversations_keep_separate_provenance_in_real_save(client, seen, via_chat):
    vid, wid, base = start(client)
    if via_chat:
        from app.db.models import Workspace
        from app.state import db
        with db()() as session:
            workspace = session.get(Workspace, wid)
            workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"),
                "chat_entry_defer_clarification": True, "entry_goal": "startup"})
            session.commit()
    state = request(client, "GET", base).json()
    choice = state["turns"][-1]["options"]["options"][1]
    words = "Two bakery owners I spoke to said paper orders get lost."
    answer = f"Assumed: {choice}\n{words}"
    response = request(client, "POST", f"/ventures/{vid}/chat", json={"message": answer,
        "question_id": state["turns"][-1]["id"], "reply_to_question": True}) if via_chat else request(client, "POST", base + "/messages", json={"text": answer})
    assert response.status_code == 200, response.text
    saved = request(client, "GET", base).json()["gstack_answers"][0]
    assert saved["answer"] == words and saved["founder_text"] == words
    assert not saved["assumed"] and saved["provenance"] == "founder_stated"
    assert not saved["suggested_text"]
    request(client, "POST", base + "/messages", json={"text": "A named workaround. They use paper order books."})
    request(client, "POST", base + "/messages", json={"text": "Order form only"})
    run_queued("idea.validate")
    critique = next(messages for messages in seen if "requested_methods" in messages[-1]["content"])
    payload = json.loads(critique[-1]["content"])
    assert payload["gstack_answers"][0]["answer"] == words
    assert not payload["gstack_answers"][0]["assumed"]
    assert payload["gstack_answers"][0]["founder_text"] == words
    assert not payload["gstack_answers"][0]["suggested_text"]
    assert "even when the same answer includes an assumed pick" in critique[0]["content"]
    assert "Assess those typed words as the founder's statement" in critique[0]["content"]


@pytest.mark.parametrize("idea", [None, TINYPLATE])
def test_off_topic_is_assumed_once_without_repeating(client, seen, idea):
    vid, wid, base = start(client, idea)
    one = request(client, "POST", base + "/messages", json={"text": "purple monkey dishwasher"}).json()
    assert one["session"]["turns"][-1]["options"]["index"] == 2 and one["job_id"] is None
    assert "retry" not in one["session"]["turns"][-1]["options"]
    assert "purple monkey dishwasher" in _founder_turns(client, base)
    assert request(client, "GET", base).json()["gstack_answers"][0]["assumed"]


def test_skip_this_one_leaves_it_unanswered_and_asks_the_next(client, seen):
    vid, wid, base = start(client)
    result = request(client, "POST", base + "/messages", json={"text": "skip this one"}).json()
    last = result["session"]["turns"][-1]["options"]
    assert last["index"] == 2 and "I assumed" not in last["note"] and "You skipped" in last["note"]
    saved = result["session"]["gstack_answers"]
    assert len(saved) == 1 and saved[0]["skipped"] and saved[0]["assumed"] and saved[0]["answer"] == ""


@pytest.mark.parametrize("idea", ["bakery", "toddler"])
def test_skip_never_saves_an_option_as_the_founder_answer(client, seen, monkeypatch, idea):
    from sqlalchemy import select
    from app.chat import milestones
    from app.db.supymem_models import ClarifierTurn
    from app.gstack import office_hours
    from app.state import db
    posted = []
    real = milestones.post_milestone
    monkeypatch.setattr(milestones, "post_milestone", lambda *a, **k: (posted.append(k["body_md"]), real(*a, **k))[1])
    vid, _, base = start(client, TINYPLATE if idea == "toddler" else None)
    question = request(client, "GET", base).json()["turns"][-1]["options"]
    options = [o for q in [question, *question.get("queue", [])] for o in q["options"]]
    calls_before = len(seen)
    result = request(client, "POST", base + "/messages", json={"text": "Skip the questions"}).json()
    assert result["job_id"] is None
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing" and state["validation_runs"] == []
    with db()() as session:
        founder = [t.text for t in session.scalars(select(ClarifierTurn).where(
            ClarifierTurn.session_id == state["id"], ClarifierTurn.role == "founder"))]
        saved = office_hours.answers_for(session, state["id"])
        facts = office_hours.saved_snapshot(session, vid, state["current_revision"]["id"]).get("founder_facts", {})
    stored = json.dumps([founder, saved, facts])
    assert saved and all(a["skipped"] and a["answer"] == "" and a["provenance"] == "not_answered" for a in saved)
    assert not [o for o in options if o in stored]
    texts = [result.get("note") or "", state["turns"][-1]["text"], *posted]
    assert not any("I assumed" in t or "Assumed: " in t for t in texts)
    assert len(saved) == len([question, *question.get("queue", [])])
    assert len(seen) == calls_before


def test_question_call_failure_asks_the_standard_questions_never_silently_skips(client, monkeypatch, empty_brain_memory):
    calls = []

    def callback(messages, kwargs):
        if "short GStack office-hours session" in messages[0]["content"]:
            calls.append("picker")
            return reply({"questions": []})  # invalid: fewer than two
        data = json.loads(messages[-1]["content"])
        if "requested_methods" in data:
            return critique_reply(data)
        return completion(proposal=data["force_propose"], case=data.get("explicit_case") or "know", understanding=data["understanding"])
    install_provider(monkeypatch, callback)
    vid, wid, base = start(client)
    assert calls == ["picker"]
    state = request(client, "GET", base).json()
    options = state["turns"][-1]["options"]
    assert state["state"] == "validating" and options["phase"] == "gstack" and options["gstack_id"] == "Q1" and options["total"] == 2
    assert options["index"] == 1 and 2 <= len(options["options"]) <= 4
    assert request(client, "POST", base + "/messages", json={"text": SKIP_TEXT}).json()["job_id"]
    run_queued("idea.validate")
    assert len(request(client, "GET", base).json()["validation_runs"]) == 3


def test_questions_are_not_asked_twice_for_the_same_idea(client, seen):
    from app.gstack import office_hours
    from app.state import db
    vid, wid, base = start(client)
    sid = base.rsplit("/", 1)[1]
    with db()() as session:
        assert office_hours.asked_before(session, sid)
        assert not office_hours.asked_before(session, "ids_absent")
    assert [p["gstack_id"] for p in office_hours.default_picks()] == ["Q1", "Q2", "Q3"]
    assert all(not p["options"] and len(p["text"]) <= 400 for p in office_hours.default_picks())


def test_old_validation_rows_without_structured_fields_still_serialize_and_brief(client):
    from app.db.supymem_models import ValidationRun
    from app.gstack.office_hours import brief
    from app.idea.service import serialize_validation
    from app.state import db
    from core.clock import utcnow
    vid, wid, sid = venture(client)
    request(client, "GET", f"/ventures/{vid}/idea/sessions/{sid}")
    with db()() as session:
        from app.db.supymem_models import IdeaRevision
        rev = IdeaRevision(id="rev_old", session_id=sid, workspace_id=wid, venture_id=vid, rev=99, source="founder",
                           summary="old", structured="{}", created_at=utcnow())
        session.add(rev)
        session.flush()
        run = ValidationRun(id="vlr_old", session_id=sid, revision_id="rev_old", workspace_id=wid, venture_id=vid,
            method="gstack_office_hours", state="complete", verdict="needs_evidence", created_at=utcnow(),
            report=json.dumps({"summary": "old", "findings": [{"method": "gstack_office_hours", "assessment": "Q1 Demand Reality: none."}],
                               "gaps": ["No payment evidence."], "proposed_tests": [{"hypothesis": "h", "action": "a", "success_measure": "s"}]}))
        session.add(run)
        session.commit()
        assert serialize_validation(run)["report"]["summary"] == "old"
        old = brief(session, sid, "rev_old")
        assert old["unverified_gaps"] == ["No payment evidence."] and old["risks"] == [] and old["biggest_risk"] is None


def test_parse_accepts_old_report_shape_without_gstack_answers():
    from app.idea import validation_agent as agent
    revision = {"id": "rev_one", "text": json.dumps({"problem": "p"})}
    raw = json.dumps({"verdict": "needs_evidence", "summary": "s",
        "findings": [{"method": "gstack_ceo_review", "assessment": "a", "source_ids": ["rev_one"]}],
        "gaps": ["g"], "proposed_tests": [], "questions": []})
    parsed = agent.parse_result(raw, revision, [], ["gstack_ceo_review"])
    assert parsed.forcing_questions == [] and parsed.biggest_risk == ""
    with pytest.raises(agent.ValidationPolicyError):  # an asked question must be assessed
        agent.parse_result(raw, revision, [], ["gstack_ceo_review"], gstack_answers=[{"id": "Q1", "answer": "x"}])


def test_research_queries_target_the_riskiest_assumption():
    from app.research.deep import queries_for
    snapshot = {"problem": "Bakeries lose orders", "customer": "Bakery owners", "gstack_brief": {"biggest_risk": RISK}}
    assert any(key == "gstack_risk" and "paid for order intake" in query for key, query in queries_for(snapshot, "demand"))
    assert not any(key == "gstack_risk" for key, _ in queries_for({"problem": "p", "customer": "c"}, "demand"))


@pytest.mark.parametrize("idea", [
    {"summary": "A delivery record tool", "offer": "Reconcile delivery records", "customer": "freight coordinators", "problem": "delayed delivery reviews"},
    {"summary": "A swimming lesson planner", "offer": "Plan swimming lessons", "customer": "parents", "problem": "scheduling swimming lessons"},
])
def test_compound_skip_to_idea_summary_finishes_the_questions(client, seen, idea):
    from app.gstack.office_hours import skips_all_questions

    _, _, base = start(client, idea)
    message = "Skip the remaining questions and show my idea summary."
    assert skips_all_questions(message)
    skipped = request(client, "POST", base + "/messages", json={"text": message})
    assert skipped.status_code == 200, skipped.text
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing" and state["turns"][-1]["kind"] == "proposal"
    assert skipped.json()["skipped_questions"] and skipped.json()["job_id"] is None
    assert state["turns"][-2]["options"]["provenance"] == "not_answered"


@pytest.mark.parametrize("idea", [
    {"summary": "A stock reconciliation tool", "offer": "Reconcile stock deliveries", "customer": "bookshop purchasing managers", "problem": "missing delivery receipts"},
    {"summary": "A practice journal", "offer": "Plan short music practice sessions", "customer": "adults learning the violin", "problem": "inconsistent practice routines"},
])
def test_skip_with_explanation_keeps_unanswered_questions_blank(client, seen, idea):
    from app.gstack.office_hours import (
        is_skip,
        skips_all_questions,
        skips_only_this_question,
    )

    for message in ("If I have enough information, skip all questions.",
                    "Skip all questions if I have enough information.",
                    "Do not skip all questions. I want to answer them.",
                    "Skip all questions. Only if I have enough information.",
                    "Skip all questions. Actually, don't skip them.",
                    "Skip all questions. I need more time. Only if I am ready.",
                    "Skip all questions. I do not want you to skip anything.",
                    "Skip this question if I have enough information.",
                    "Skip this question. Only if I have enough information.",
                    "Skip the tutorial. I already understand it."):
        assert not skips_all_questions(message), message
        assert not is_skip(message), message
    assert not skips_only_this_question("Skip pricing if I have enough information.", field="pricing_hypothesis")
    venture_id, _, base = start(client, idea)
    message = f"Skip all questions. I need to ask {idea['customer']} about {idea['problem']}."
    assert skips_all_questions(message) and is_skip(message)
    response = request(client, "POST", f"/ventures/{venture_id}/chat", json={"message": message})
    assert response.status_code == 200, response.text
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing" and state["turns"][-1]["kind"] == "proposal"
    assert state["current_revision"]["structured"]["_questions_skipped"]
    assert state["turns"][-2]["options"]["provenance"] == "not_answered"
    assert not state["turns"][-2]["options"]["founder_text"]
    assert all(answer["skipped"] and not answer["answer"] for answer in state["gstack_answers"])
    assert idea["customer"] in response.json()["body_md"] and idea["offer"] in response.json()["body_md"]
    assert "Nothing changed" not in response.json()["body_md"]


@pytest.mark.parametrize("idea,answer,command", [
    ({"summary": "A maintenance ledger", "offer": "Track equipment servicing",
      "customer": "factory maintenance supervisors", "problem": "lost service records"},
     "Factory maintenance supervisors track servicing in spreadsheets, but I have not interviewed anyone.",
     "Skip all questions. I need interviews to avoid guessing about demand."),
    ({"summary": "A family trail planner", "offer": "Plan short family hikes",
      "customer": "parents hiking with children", "problem": "finding suitable local trails"},
     "Parents hiking with children compare local trails, but nobody has tried this idea yet.",
     "Skip the balance of the questions, I need to learn how families choose routes."),
])
def test_answer_then_question_skip_is_one_message_and_reports_saved_changes(client, seen, idea, answer, command):
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.gstack.office_hours import question_reply
    from app.state import db
    from sqlalchemy import select

    venture_id, _, base = start(client, idea)
    question = request(client, "GET", base).json()["turns"][-1]
    question_body, question_actions = question_reply(question["id"])
    append_message(venture_id, role="assistant", body_md=question_body,
                   kind="idea_clarification_ready", actions=question_actions)
    with db()() as session:
        before = set(session.scalars(select(ChatMessage.id).where(
            ChatMessage.venture_id == venture_id, ChatMessage.role == "user")))
    message = f"{answer} {command}"
    response = request(client, "POST", f"/ventures/{venture_id}/chat", json={"message": message})
    assert response.status_code == 200, response.text
    with db()() as session:
        messages = session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.role == "user",
            ChatMessage.id.not_in(before))).all()
        assert [row.body_md for row in messages] == [message]
    state = request(client, "GET", base).json()
    assert state["state"] == "reviewing" and state["turns"][-1]["kind"] == "proposal", response.json()["body_md"]
    assert any(turn["options"].get("founder_text") == answer for turn in state["turns"] if turn["role"] == "founder")
    assert state["current_revision"]["structured"]["_questions_skipped"]
    assert any(not item["skipped"] for item in state["gstack_answers"])
    assert any(item["skipped"] for item in state["gstack_answers"])
    assert all(not item["answer"] and item["provenance"] == "not_answered"
               for item in state["gstack_answers"] if item["skipped"])
    body = response.json()["body_md"]
    assert idea["customer"] in body and idea["offer"] in body
    assert "nothing changed" not in body.casefold()


def test_skip_match_requires_a_whole_word():
    from app.gstack.office_hours import is_skip
    assert is_skip(SKIP_TEXT) and is_skip("Skip the questions")
    assert not is_skip("Skipping lunch is the problem")
    assert not is_skip("skipper needs an order board")


def test_gstack_skip_reopen_asks_only_assumptions_and_runs_fresh_review(client, seen):
    from sqlalchemy import select
    from app.db.models import Job
    from app.state import db
    from app.gstack.office_hours import saved_snapshot, venture_context
    vid, _, base = start(client)
    request(client, "POST", base + "/messages", json={"text": "One pays already"})
    request(client, "POST", base + "/messages", json={"text": SKIP_TEXT})
    run_queued("idea.validate")
    rid = _revision(client, base)
    assert request(client, "POST", base + "/confirm", json={"revision_id": rid}).status_code == 200
    with db()() as session:
        old_job = session.scalar(select(Job).where(Job.dedupe_key.like("%:gstack:%"))).dedupe_key
    reopened = request(client, "POST", base + "/reopen-questions", json={"revision_id": rid})
    assert reopened.status_code == 200, reopened.text
    state = reopened.json()
    assert state["id"] != base.rsplit("/", 1)[-1]
    assert state["state"] == "validating"
    assert request(client, "GET", base).json()["state"] == "confirmed"
    duplicate = request(client, "POST", base + "/reopen-questions", json={"revision_id": rid})
    assert duplicate.status_code == 200 and duplicate.json()["id"] == state["id"]
    stale = request(client, "POST", base + "/reopen-questions", json={"revision_id": "revision_stale"})
    assert stale.status_code == 404  # unknown revisions are rejected by the shared authorization layer
    other_vid, _, _ = venture(client)
    foreign = request(client, "POST", f"/ventures/{other_vid}/idea/sessions/{state['id']}/reopen-questions",
                      json={"revision_id": state["current_revision"]["id"]})
    assert foreign.status_code == 404
    latest = state["turns"][-1]
    assert latest["options"]["gstack_id"] == "Q2"
    assert latest["options"]["total"] == 2
    new_base = f"/ventures/{vid}/idea/sessions/{state['id']}"
    replay = request(client, "POST", new_base + "/reopen-questions", json={"revision_id": state["current_revision"]["id"]})
    assert replay.status_code == 200 and replay.json()["turns"][-1]["id"] == latest["id"]
    one = request(client, "POST", new_base + "/messages", json={"text": "Email and paper"}).json()
    assert one["session"]["turns"][-1]["options"]["gstack_id"] == "Q4"
    with db()() as session:
        job = venture_context(saved_snapshot(session, vid, state["current_revision"]["id"]))[1]
    two = request(client, "POST", new_base + "/messages", json={"text": f"{job}. Start with a form and reminders."}).json()
    assert two["job_id"]
    with db()() as session:
        assert session.get(Job, two["job_id"]).dedupe_key != old_job
    while_running = request(client, "POST", new_base + "/reopen-questions", json={"revision_id": state["current_revision"]["id"]})
    assert while_running.status_code == 409
    run_queued("idea.validate")
    updated = request(client, "GET", new_base).json()
    assert len(updated["validation_runs"]) == 3
    assert not any(answer["assumed"] for answer in updated["gstack_answers"])
    assert request(client, "POST", new_base + "/confirm", json={"revision_id": updated["current_revision"]["id"]}).json()["dossier_job_id"]
    assert request(client, "POST", new_base + "/reopen-questions", json={"revision_id": updated["current_revision"]["id"]}).status_code == 422


@pytest.mark.parametrize("inactive", ["archived_at", "archived_stage", "purged"])
@pytest.mark.parametrize("reopened_first", [False, True])
def test_archived_owner_cannot_reopen_assumed_questions(client, seen, inactive, reopened_first):
    from sqlalchemy import func, select
    from app.db.models import Job, Venture, Workspace
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.idea.service import reopen_assumed_questions
    from app.state import db
    from core.clock import utcnow

    vid, wid, base = start(client)
    request(client, "POST", base + "/messages", json={"text": SKIP_TEXT})
    run_queued("idea.validate")
    revision_id = _revision(client, base)
    assert request(client, "POST", base + "/confirm", json={"revision_id": revision_id}).status_code == 200
    session_id = base.rsplit("/", 1)[1]
    targets = [(base, session_id, revision_id)]
    if reopened_first:
        opened = request(client, "POST", base + "/reopen-questions", json={"revision_id": revision_id})
        assert opened.status_code == 200
        current = opened.json()
        targets.append((f"/ventures/{vid}/idea/sessions/{current['id']}", current["id"], current["current_revision"]["id"]))

    def counts(session):
        return {model.__tablename__: session.scalar(select(func.count()).select_from(model).where(model.venture_id == vid))
                for model in (IdeaSession, IdeaRevision, ClarifierTurn, Job)}

    with db()() as session:
        row = session.get(Venture, vid)
        if inactive == "archived_at":
            row.archived_at = utcnow()
        else:
            row.stage = "archived" if inactive == "archived_stage" else "purged"
        session.get(Workspace, wid).archived_at = utcnow()
        before = counts(session)
        session.commit()
    for target, sid, rid in targets:
        denied = request(client, "POST", target + "/reopen-questions", json={"revision_id": rid})
        assert denied.status_code == 404
        with pytest.raises(KeyError):
            reopen_assumed_questions(sid, venture_id=vid, revision_id=rid)
    with db()() as session:
        assert counts(session) == before


def test_office_hours_replaces_fabricated_evidence_options_with_neutral_choices():
    from types import SimpleNamespace
    from app.gstack.office_hours import _reply, question_turn_options
    long_option = "I spoke to the independent bakery owners and they currently pay for a different order management tool."
    question = {"gstack_id": "Q1", "gstack_name": "Demand", "text": "What evidence do you have?",
                "why": "Check real demand", "options": [long_option, "No evidence yet"]}
    opts = question_turn_options(question, [], 1, 1)
    assert not opts["recommended_option"] and not opts["recommendation_reason"]
    body, actions = _reply(SimpleNamespace(id="ict_synthetic", text=question["text"], options=json.dumps(opts)))
    assert [action["label"] for action in actions[:-1]] == ["No evidence yet", "Some evidence to describe"]
    assert actions[0]["payload"]["recommended"] is False
    assert "Suggested:" not in body


@pytest.mark.parametrize("options,expected", [
    (["I'm not sure", "I've had a couple of owners promise to pay verbally"], "I'm not sure"),
    (["I've interviewed shop owners", "No evidence yet", "They promised to pay"], "No evidence yet"),
    (["I've talked to owners", "They promised to pay"], ""),
])
def test_office_hours_does_not_trust_evidence_option_order(options, expected):
    from app.gstack.office_hours import SYSTEM, _reply, assumed_text, question_turn_options
    question = {"gstack_id": "Q1", "gstack_name": "Demand", "text": "Have you talked to shop owners?",
                "why": "Check demand", "options": options}
    opts = question_turn_options(question, [], 1, 1)
    assert not opts["recommended_option"]
    assert assumed_text(question, "skipped").startswith(f"Assumed: {expected or 'no evidence yet'}")
    opts.update(recommended_option=options[-1], recommendation_reason="Use this conservative assumption until you have evidence.")
    body, actions = _reply(SimpleNamespace(id="ict_saved", text=question["text"], options=json.dumps(opts)))
    assert [action["payload"].get("recommended", False) for action in actions[:-1]] == [False, False]
    assert [action["label"] for action in actions[:-1]] == ["No one yet", "A few people"]
    assert "conservative assumption" not in body
    assert "explicit no-evidence" in SYSTEM


def test_legacy_future_question_recommends_only_an_existing_option():
    from app.gstack.office_hours import _reply
    opts = {"options": ["Order form only", "Form plus reminders"], "index": 1, "total": 1,
            "gstack_name": "Narrowest Wedge"}
    body, actions = _reply(SimpleNamespace(id="ict_saved", text="What should we build first?", options=json.dumps(opts)))
    assert actions[0]["payload"]["recommended"] is False
    assert "Suggested:" not in body


@pytest.mark.parametrize("field,text", [("question", "Which customer? " * 40), ("options", "I spoke to a customer. " * 20)])
def test_overlong_office_hours_text_rejects_instead_of_clipping(field, text):
    from pydantic import ValidationError
    from app.gstack.office_hours import parse_picks
    questions = [{"id": "Q1", "question": "Who have you spoken to?", "options": ["One customer", "No evidence yet"]},
                 {"id": "Q2", "question": "What do they do today?", "options": ["Paper calendars", "No workaround yet"]}]
    questions[0][field] = [text, "No evidence yet"] if field == "options" else text
    with pytest.raises(ValidationError):
        parse_picks(json.dumps({"questions": questions}))


def test_recommendation_lives_on_option_not_in_question_body():
    from app.gstack.office_hours import _reply
    reason = "You can reach the independent gym owners you already described."
    pick = "A tight niche I know, even if I don't have a name yet"
    opts = {"options": [pick, "A specific named customer"], "index": 3, "total": 3, "gstack_name": "Who needs this most",
            "recommended_option": pick, "recommendation_reason": reason}
    body, actions = _reply(SimpleNamespace(id="ict_saved", text="Who needs this most?", options=json.dumps(opts)))
    assert "Suggested:" not in body and reason not in body
    rec = [a for a in actions if a["payload"].get("recommended")]
    assert len(rec) == 1 and rec[0]["payload"]["recommendation_reason"]


def test_venture_specific_candidates_use_metered_picker_and_contextual_fallback(client, monkeypatch):
    from sqlalchemy import select
    from app.credits import metered
    from app.config import settings
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack import office_hours
    from app.idea.service import _next_turn_number
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    domains = [
        {"audience": "freight dispatchers", "problem": "late delivery updates", "idea": "Track late delivery updates for freight dispatchers",
         "tools": ["Check Samsara for delayed vehicles", "Phone drivers for delivery updates", "Reconcile dispatch notes in Excel"],
         "moments": ["A vehicle misses its loading slot", "A customer calls about an overdue delivery", "A dispatcher hands over an unfinished route"],
         "earlier": "I want to focus on a dispatcher handling one depot."},
        {"audience": "weekend hikers", "problem": "planning safe walking routes", "idea": "Help weekend hikers plan safe walking routes",
         "tools": ["Browse AllTrails for route reviews", "Download a Komoot route before leaving", "Ask a hiking group about trail conditions"],
         "moments": ["A weather warning arrives before a hike", "A trail closure blocks the planned route", "A friend joins who needs a shorter walk"],
         "earlier": "I want to start with people planning their first weekend hike."},
    ]
    captured = []
    original_capture = metered.capture_model_receipt

    def capture(meter, response):
        captured.append((meter.venture_id, meter.purpose))
        return original_capture(meter, response)

    monkeypatch.setattr(metered, "capture_model_receipt", capture)
    current, picker_inputs = {}, []

    def provider(messages, kwargs):
        if "short GStack office-hours session" in messages[0]["content"]:
            snapshot = json.loads(messages[-1]["content"])["founder_idea"]
            picker_inputs.append(snapshot)
            return reply({"questions": [
                {"id": "Q2", "question": f"What do {current['audience']} use today for {current['problem']}?",
                 "options": current["tools"], "requires_detail": [], "detail_prompt": ""},
                {"id": "Q3", "question": f"When does {current['problem']} become urgent for {current['audience']}?",
                 "options": current["moments"], "requires_detail": [], "detail_prompt": ""}]})
        snapshot = json.loads(messages[-1]["content"])
        return completion(proposal=True, understanding=snapshot["understanding"])

    install_provider(monkeypatch, provider)
    actual, fallbacks = [], []
    for current in domains:
        new_flow = current is domains[1]
        monkeypatch.setattr(settings, "new_user_flow", "on" if new_flow else "off")
        created = request(client, "POST", "/ventures", json={"name": current["audience"], "raw_idea": current["idea"], "start": False}).json()
        vid = created["id"]
        sid = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()["id"]
        base = f"/ventures/{vid}/idea/sessions/{sid}"
        patch = {"summary": current["idea"], "problem": current["problem"], "customer": current["audience"], "offer": current["idea"]}
        assert request(client, "PATCH", base + "/revision", json={"patch": patch}).status_code == 200
        with db()() as session:
            row = session.get(IdeaSession, sid)
            session.add(ClarifierTurn(id=new_id("ict"), session_id=sid, venture_id=vid, workspace_id=row.workspace_id,
                turn=_next_turn_number(session, sid), role="founder", kind="answer", text=current["earlier"],
                revision_id=row.current_revision_id, created_at=utcnow()))
            session.commit()
        assert request(client, "POST", base + "/propose").status_code == 200
        run_queued("idea.clarify_turn")
        if not new_flow:
            looks_right(client, vid, base)
            run_queued("idea.validate")
        saved = request(client, "GET", base).json()["turns"][-1]["options"]
        assert saved["options"] == office_hours.contextual_choices("Q2", picker_inputs[-1])
        assert f"Offer: {current['idea']}" in picker_inputs[-1]["idea"]
        assert picker_inputs[-1]["question_context"] == {"audience": current["audience"], "job": current["problem"]}
        assert picker_inputs[-1]["customer"] == current["audience"]
        assert any(answer["founder_text"] == current["earlier"] for answer in picker_inputs[-1]["earlier_answers"])
        assert "requires_detail" in office_hours.response_schema()["$defs"]["Picked"]["required"]
        assert saved["queue"] == []
        assert not saved["recommended_option"]
        assert (vid, "idea.clarify_turn") in captured
        actual.append(saved["options"])
        fallback = office_hours.default_picks({"idea": current["idea"], **patch})
        for question in fallback:
            assert len(question["options"]) == 3
            assert all(current["audience"] not in option and current["problem"] not in option for option in question["options"])
        fallbacks.append(fallback[1]["options"])
    assert actual[0] == actual[1] == fallbacks[0] == fallbacks[1]


def test_detail_tag_accepts_selections_without_extra_text_and_keeps_them_assumptions(client, monkeypatch):
    from sqlalchemy import select
    from app.api.clarify import AnswerItem, _normalized_answer
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack.office_hours import answers_for, question_turn_options
    from app.idea.service import _next_turn_number
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    install_provider(monkeypatch)
    captured = []
    monkeypatch.setattr("app.ventures.founder_facts.schedule", lambda venture_id, text, *args, **kwargs: captured.append(text))
    for phase, audience, idea, description in [
        ("gstack", "freight dispatchers", "A tool for freight dispatchers to track delayed deliveries", "On Tuesday a driver missed a loading slot and the dispatcher had to call the customer."),
        ("clarification", "weekend hikers", "Plan safe walking routes for weekend hikers", "On Saturday my friend reached a closed trail and had to find a shorter route home."),
    ]:
        created = request(client, "POST", "/ventures", json={"name": audience, "raw_idea": idea, "start": False}).json()
        vid = created["id"]
        sid = request(client, "POST", f"/ventures/{vid}/idea/sessions").json()["id"]
        label = "A specific moment you can describe" if phase == "gstack" else "I heard someone describe a concrete incident"
        prompt = "Describe that moment: who, when, what happened?"
        question = {"gstack_id": "Q3", "gstack_name": "Urgency", "text": f"When does this become urgent for {audience}?",
            "why": "Identify the triggering moment", "options": [label, "A recurring interruption", "You have not checked yet"],
            "requires_detail": [] if phase == "gstack" else [label], "detail_prompt": prompt, "contextual_options": phase != "gstack"}
        with db()() as session:
            row = session.get(IdeaSession, sid)
            row.state = "validating" if phase == "gstack" else "clarifying"
            options = question_turn_options(question, [], 1, 1) if phase == "gstack" else {**question, "field": "problem"}
            question_id = new_id("ict")
            session.add(ClarifierTurn(id=question_id, session_id=sid, venture_id=vid, workspace_id=row.workspace_id,
                turn=_next_turn_number(session, sid), role="agent", kind="question", text=question["text"], options=json.dumps(options),
                revision_id=row.current_revision_id, created_at=utcnow()))
            session.commit()
        message = label if phase == "gstack" else f"Assumed: {label}\n{label}"
        assert request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "idea.review"}).status_code == 200
        response = request(client, "POST", f"/ventures/{vid}/chat", json={"message": message,
            "question_id": question_id, "reply_to_question": True})
        assert response.status_code == 200, response.text
        state = request(client, "GET", f"/ventures/{vid}/idea/sessions/{sid}").json()
        saved = next(turn for turn in reversed(state["turns"]) if turn["role"] == "founder")
        assert saved["text"] == message
        assert saved["options"]["provenance"] == "suggested_choice"
        assert saved["options"]["founder_text"] == ""
        assert not any(turn["role"] == "agent" and turn["text"] == prompt for turn in state["turns"])
        assert label not in captured
        form_question = {"options": [{"id": "opt_a", "label": label, "requires_detail": True}]}
        for other in (None, label):
            assert _normalized_answer(AnswerItem(question_id=question_id, option_id="opt_a", other_text=other), form_question) == ("opt_a", label)
        assert _normalized_answer(AnswerItem(question_id=question_id, option_id="opt_a", other_text=description), form_question) == (description, description)
        if phase == "gstack":
            with db()() as session:
                assert answers_for(session, sid)[0]["provenance"] == "suggested_choice"
