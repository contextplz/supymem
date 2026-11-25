"""Every reply in the first minutes leaves a founder a step that really moves them on."""
import json

import pytest
from sqlalchemy import select

from tests.integration.test_chat_first_idea_bridge import client as client
from tests.unit.chat.test_founder_intake_progress import DOMAINS as INTAKE_DOMAINS, _click, _send
from tests.unit.chat.test_skip_confirm_experiment_gate import _flow_on

DOMAINS = [item[0] for item in INTAKE_DOMAINS]  # one B2B, one consumer
SKIP = "Skip the questions"


@pytest.fixture(autouse=True)
def _new_flow(client, monkeypatch):
    from tests.company_os_harness import configure_local_gtm

    configure_local_gtm(monkeypatch)
    _flow_on(monkeypatch)


def _venture(client, idea):
    from app.idea.service import get_session
    from tests.security.test_durable_journey_boundary import run_queued

    preview = client.post("/entry/chat/preview", json={"text": idea})
    committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
    assert committed.status_code == 200, committed.text
    venture_id = committed.json()["venture_id"]
    run_queued("idea.clarify_turn")
    sid = client.get(f"/ventures/{venture_id}/idea/sessions").json()["items"][0]["id"]
    turn = get_session(sid)["turns"][-1]
    assert turn["kind"] == "question", turn
    return venture_id, sid, turn["id"]


def _set_state(sid, state):
    from app.db.supymem_models import IdeaSession
    from app.state import db

    with db()() as session:
        session.get(IdeaSession, sid).state = state
        session.commit()


def _clarification_died(venture_id, *, sid, state=None):
    """The founder answered and the clarification job then failed for good, with no new question."""
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaSession
    from app.idea.service import _next_turn_number
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    now, job_id = utcnow(), new_id("job")
    with db()() as session:
        idea = session.get(IdeaSession, sid)
        session.add(ClarifierTurn(id=new_id("ict"), session_id=sid, workspace_id=idea.workspace_id, venture_id=venture_id,
            turn=_next_turn_number(session, sid), role="founder", kind="answer", text="Two people told me this hurts.",
            options="{}", created_at=now))
        session.add(Job(id=job_id, workspace_id=idea.workspace_id, venture_id=venture_id, kind="idea.clarify_turn",
            payload_json=json.dumps({"session_id": sid, "revision_id": idea.current_revision_id}),
            status="failed_dead", last_error="clarification_invalid_output", created_at=now))
        session.flush()
        session.add(IdeaClarificationRun(id=new_id("icr"), job_id=job_id, session_id=sid, workspace_id=idea.workspace_id,
            venture_id=venture_id, revision_id=idea.current_revision_id, state="failed", attempt=1, snapshot="{}",
            error_code="clarification_invalid_output", created_at=now, updated_at=now))
        idea.state = state or idea.state
        session.commit()


def _visible_actions(reply):
    return [*reply["actions"], *((reply.get("next_moves") or {}).get("actions") or [])]


def _names(reply):
    return [item.get("action") for item in _visible_actions(reply)]


@pytest.mark.parametrize("idea", DOMAINS)
def test_written_reply_does_not_compose_unrequested_work(monkeypatch, idea):
    from types import SimpleNamespace
    from app.chat import next_moves

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Written replies must not compose unrelated work")

    monkeypatch.setattr(next_moves, "compose", forbidden)
    row = SimpleNamespace(kind="text", body_md=f"Your interview plan for {idea}",
                          actions_json="[]", citations_json="[]")
    assert not next_moves.attach("ven_written_reply", row,
        request="Write an interview plan and questions. Do not contact anyone.")
    assert not calls
    assert row.actions_json == "[]" and row.citations_json == "[]"


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("message", [
    "Design a small market test. Give me an interview script, a schedule and a pass-or-fail rule. Do not contact anyone or build anything.",
    "Write a short interview plan and questions. I will send any messages myself; do not send or publish anything.",
    "I have not interviewed anyone yet. Give me a day-by-day plan and a clear pass or fail decision. Do not contact anyone.",
    "Please prepare the market test and its recruiting drafts now, without sending anything.",
])
def test_requested_written_work_is_delivered_in_chat(client, monkeypatch, idea, confirmed, message):
    from app.chat import assistant, llm_router
    from app.db.models import CreditReservation, Job, PlannedTask
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    venture_id, sid, _question_id = _venture(client, idea)
    if confirmed:
        _set_state(sid, "confirmed")

    def snapshot():
        with db()() as session:
            return [list(session.scalars(select(model.id).order_by(model.id)))
                    for model in (CreditReservation, Job, PlannedTask, ClarifierTurn)]

    before = snapshot()
    calls = []
    written = (f"Your interview plan for {idea}\n\n"
        "1. Day 1: Choose consenting participants and record your assumptions.\n"
        "2. Day 2: Ask what happened last time and how they handled it.\n"
        "3. Day 3: Record commitments separately from compliments.\n\n"
        "Pass if participants commit to a paid trial; fail if they decline.\n\n"
        "Your next step: reserve time for the first conversation. Nothing has been sent.")

    async def complete(messages, **kwargs):
        calls.append(messages)
        return written

    async def forbidden(*args, **kwargs):
        raise AssertionError("A written request must not become an execution-routing decision")

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", forbidden)
    reply = _send(client, venture_id, message)
    assert len(calls) == 1
    assert message in calls[0][-1]["content"]
    assert "Write the requested material" in calls[0][-1]["content"]
    assert "Attached next steps" not in calls[0][-1]["content"]
    assert written in reply["body_md"]
    assert not any(action.get("confirmation") for action in _visible_actions(reply))
    assert not set(_names(reply)) & {"journey.jump_to", "experiment.run", "gtm.find", "plan.generate"}
    assert snapshot() == before
    from app.chat.store import list_messages
    assert written in list_messages(venture_id)[-1]["body_md"]


@pytest.mark.parametrize("idea,message", [
    (DOMAINS[0], "Prepare a troubleshooting handbook for inspection teams. Return it as a document."),
    (DOMAINS[1], "Write a welcome note for families cooking together."),
])
@pytest.mark.parametrize("prefix", ["", "Please "])
def test_document_requests_preserve_pending_setup(client, monkeypatch, idea, message, prefix):
    from app.chat import assistant, llm_router
    from app.chat.idea_bridge import pending_question, written_request
    from app.db.models import CreditReservation, Job, PlannedTask
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db

    message = prefix + message
    venture_id, sid, question_id = _venture(client, idea)

    def snapshot():
        with db()() as session:
            return ([list(session.scalars(select(model.id).order_by(model.id)))
                     for model in (CreditReservation, Job, PlannedTask, ClarifierTurn)],
                    session.get(IdeaSession, sid).state)

    before = snapshot()
    calls = []
    routes = []
    written = "Your document is ready. Your next step: review it with your first users."

    async def complete(messages, **kwargs):
        calls.append(messages)
        return written

    async def route(venture_id, text, *args, **kwargs):
        routes.append(text)
        return llm_router.Decision([("office_hours", text)])

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", route)
    reply = _send(client, venture_id, message)
    assert len(calls) == 1, reply["body_md"]
    assert not routes
    assert written_request(message)
    assert message in calls[0][-1]["content"]
    assert "Write the requested material" in calls[0][-1]["content"]
    assert written in reply["body_md"]
    assert not any(action.get("confirmation") for action in _visible_actions(reply))
    assert not set(_names(reply)) & {"journey.jump_to", "experiment.run", "gtm.find", "plan.generate"}
    assert snapshot() == before
    assert pending_question(venture_id, "", force=True) == (sid, question_id)


@pytest.mark.parametrize("idea,lookup,original,reply_to_question", [
    (DOMAINS[0], "Show me the interview questions you drafted earlier, not a replacement.",
     "1. How do you record failed inspections?\n2. Who checks the repair log?\n\nYou can choose an inspection team to interview next.", False),
    (DOMAINS[1], "What were the questions you wrote for our previous interviews? Retrieve the original.",
     "1. How do you choose meals together?\n2. What happened the last time dinner plans changed?\n\nYou can choose a family to interview next.", True),
], ids=["b2b", "consumer"])
def test_saved_interview_questions_are_retrieved_before_setup_parking(client, monkeypatch, idea, lookup, original, reply_to_question):
    from app.chat import assistant, llm_router
    from app.chat.idea_bridge import pending_question
    from app.db.models import CreditReservation, Job, PlannedTask
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db

    venture_id, sid, question_id = _venture(client, idea)
    calls = []
    routes = []

    async def complete(messages, **kwargs):
        calls.append(messages)
        return original

    async def route(venture_id, text, *args, **kwargs):
        routes.append(text)
        return llm_router.Decision([("office_hours", text)])

    monkeypatch.setattr(assistant, "_complete", complete)
    written = _send(client, venture_id, "Write two interview questions for this idea.")
    assert original in written["body_md"]
    assert len(calls) == 1
    shown = _send(client, venture_id, "Continue clarifying")
    assert any((action.get("payload") or {}).get("question_id") == question_id for action in shown["actions"])

    def snapshot():
        with db()() as session:
            return ([list(session.scalars(select(model.id).order_by(model.id)))
                     for model in (CreditReservation, Job, PlannedTask, ClarifierTurn)],
                    session.get(IdeaSession, sid).state)

    before = snapshot()
    monkeypatch.setattr(llm_router, "route", route)
    reply = _send(client, venture_id, lookup, reply_to_question=reply_to_question)
    assert original in reply["body_md"]
    assert len(calls) == 2
    assert not routes
    assert lookup in calls[-1][-1]["content"]
    assert original in "\n".join(message["content"] for message in calls[-1])
    assert not any(action.get("confirmation") for action in _visible_actions(reply))
    assert not any((action.get("payload") or {}).get("question_id") for action in _visible_actions(reply))
    assert snapshot() == before
    assert pending_question(venture_id, "", force=True) == (sid, question_id)


@pytest.mark.parametrize("idea,audience", [
    ("Track machine servicing for repair shops", "repair shop owners"),
    ("Arrange local craft workshops for families", "families learning crafts"),
], ids=["b2b", "consumer"])
def test_plain_document_formatting_delivers_written_material_during_setup(client, monkeypatch, idea, audience):
    from app.chat import assistant, llm_router
    from app.chat.idea_bridge import pending_question, written_request
    from app.db.models import CreditReservation, Job, PlannedTask
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db

    venture_id, session_id, question_id = _venture(client, idea)
    message = f"Could you put this idea into a one-page investor brief for {audience}?"
    written = "Your brief is ready with unknown facts labelled as assumptions. You can review those assumptions next."
    calls = []
    routes = []

    def snapshot():
        with db()() as session:
            return ([list(session.scalars(select(model.id).order_by(model.id)))
                     for model in (CreditReservation, Job, PlannedTask, ClarifierTurn)],
                    session.get(IdeaSession, session_id).state)

    async def complete(messages, **kwargs):
        calls.append(messages)
        return written

    async def route(venture_id, text, *args, **kwargs):
        routes.append(text)
        return llm_router.Decision([("office_hours", text)])

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", route)
    before = snapshot()
    reply = _send(client, venture_id, message)
    assert len(calls) == 1, reply["body_md"]
    assert not routes
    assert written_request(message)
    assert "Write the requested material" in calls[0][-1]["content"]
    assert "assumptions" in calls[0][-1]["content"].lower()
    assert written in reply["body_md"]
    assert not any(action.get("confirmation") for action in _visible_actions(reply))
    assert snapshot() == before
    assert pending_question(venture_id, "", force=True) == (session_id, question_id)
    assert not written_request(message + " Then publish it.")


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("copy_context", ["as text", "in text"])
def test_written_copy_is_not_a_bulk_send(idea, copy_context):
    from app.chat.guard import _bulk_outreach, guard_reply

    message = f"Write an interview script and $120 pilot ask {copy_context} for {idea}. Include an interview schedule."
    assert not _bulk_outreach(message)
    assert guard_reply("", message, read_history=False) is None
    for request in (f"Text 120 people about {idea}.", f"Write an interview script and send 120 messages about {idea}."):
        assert _bulk_outreach(request)
        assert guard_reply("", request, read_history=False)["kind"] == "refusal"


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("state", ["recon", "clarifying"])
def test_continue_clarifying_shows_the_open_question_in_every_open_state(client, idea, state):
    """The deterministic question is written while the session is still in recon."""
    venture_id, sid, question_id = _venture(client, idea)
    _set_state(sid, state)
    reply = _send(client, venture_id, "Continue clarifying")
    assert reply["kind"] == "idea_clarification_ready"
    assert any((item.get("payload") or {}).get("question_id") == question_id for item in reply["actions"])
    assert "idea.review" not in _names(reply)


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("state", [None, "recon"])
def test_failed_clarification_offers_steps_that_move_on(client, idea, state):
    from app.core.local_mode import is_local

    venture_id, sid, question_id = _venture(client, idea)
    _clarification_died(venture_id, sid=sid, state=state)

    reply = _send(client, venture_id, "Continue clarifying")
    assert "idea.review" not in _names(reply)
    assert "—" not in reply["body_md"]
    assert any((item.get("payload") or {}).get("text") == SKIP for item in reply["actions"])
    assert "job.retry" in _names(reply)

    review = _click(client, venture_id, reply, "chat.reply", text=SKIP)
    assert "idea.confirm" in _names(review)
    confirmed = _click(client, venture_id, review, "idea.confirm")
    if is_local():
        assert confirmed["kind"] == "navigation"
        assert "whats_next" in _names(confirmed)
        saved_review = client.get(f"/ventures/{venture_id}/context")
        assert saved_review.status_code == 200, saved_review.text
        assert saved_review.json()["revision"] and not saved_review.json()["complete"]
    else:
        assert _visible_actions(confirmed)


@pytest.mark.parametrize("idea", DOMAINS)
def test_retry_from_failed_clarification_starts_a_new_attempt(client, idea):
    from app.db.models import Job
    from app.state import db

    venture_id, sid, question_id = _venture(client, idea)
    _clarification_died(venture_id, sid=sid)
    reply = _send(client, venture_id, "Continue clarifying")
    started = _click(client, venture_id, reply, "job.retry")
    from app.core.local_mode import is_local

    if not is_local():
        assert any(action.get("confirmation", {}).get("action") == "job.retry" for action in started["actions"])
        started = _click(client, venture_id, started, "job.retry")
    assert not any(action.get("confirmation") for action in started["actions"])
    assert "asking the question again" in started["body_md"].lower()
    with db()() as session:
        assert session.scalars(select(Job.status).where(Job.venture_id == venture_id, Job.kind == "idea.clarify_turn")
                               .order_by(Job.created_at.desc())).first() != "failed_dead"


@pytest.mark.parametrize("idea", DOMAINS)
def test_reply_that_failed_still_offers_a_step_other_than_retry(client, idea):
    """Sending the same words again cannot be the only way forward."""
    venture_id, sid, question_id = _venture(client, idea)
    _clarification_died(venture_id, sid=sid)
    first = _send(client, venture_id, "What should I do now?")
    names = _names(first)
    assert "job.retry" in names and "idea.review" not in names
    assert any((item.get("payload") or {}).get("text") == SKIP for item in _visible_actions(first))


@pytest.mark.parametrize("idea", DOMAINS)
def test_first_question_that_never_arrived_is_not_described_as_being_read(client, idea):
    from app.chat.next_moves import compose
    from app.db.models import Job
    from app.state import db

    preview = client.post("/entry/chat/preview", json={"text": idea})
    venture_id = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]}).json()["venture_id"]
    with db()() as session:
        job = session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == "idea.clarify_turn")).one()
        job.status, job.last_error = "failed_dead", "clarification_invalid_output"
        session.commit()
    card = compose(venture_id)
    assert "reading it" not in card["body_md"]
    assert any(move["action"] == "chat.reply" and move["payload"]["text"] == SKIP for move in card["actions"])
    assert not card.get("first_question_pending")
    review = _send(client, venture_id, SKIP)
    assert "idea.confirm" in _names(review)


@pytest.mark.parametrize("idea", DOMAINS)
def test_answer_to_the_live_question_saves_without_the_reply_flag(client, idea):
    from app.idea.service import get_session

    venture_id, sid, question_id = _venture(client, idea)
    reply = _send(client, venture_id, "Fleet and family customers both told me this hurts.", question_id=question_id)
    assert "already answered" not in reply["body_md"]
    assert any(turn["role"] == "founder" and "both told me" in turn["text"] for turn in get_session(sid)["turns"])


@pytest.mark.parametrize("idea", DOMAINS)
def test_stale_question_rejection_gives_the_current_step(client, idea):
    venture_id, sid, question_id = _venture(client, idea)
    stale = _send(client, venture_id, "An old answer", question_id="ict_old", reply_to_question=True)
    assert "did not save" in stale["body_md"]
    assert any((item.get("payload") or {}).get("question_id") == question_id for item in stale["actions"])

    _send(client, venture_id, SKIP)
    with_card = _send(client, venture_id, "An old answer", question_id=question_id, reply_to_question=True)
    assert "did not save" in with_card["body_md"]
    _click(client, venture_id, with_card, "idea.confirm")  # the card still works from the rejection
    closed = _send(client, venture_id, "An old answer", question_id=question_id, reply_to_question=True)
    assert "did not save" in closed["body_md"]
    assert "whats_next" in _names(closed)


@pytest.mark.parametrize("idea", DOMAINS)
def test_offered_moves_pass_the_same_check_as_their_handlers(client, idea):
    """Questions still open: no button that its own handler would turn away."""
    from app.chat.build_path import research_prerequisites
    from app.chat.experiments import _idea_gate
    from app.chat.next_moves import compose

    venture_id, sid, question_id = _venture(client, idea)
    reply = compose(venture_id, requested=True)
    actions = [move["action"] for move in reply["actions"]]
    assert "status" in actions and "idea.review" not in actions
    assert "already in your chat" in reply["body_md"]
    if _idea_gate(venture_id):
        assert not {"experiment.start", "experiment.list", "experiment.recommendation.prepare"} & set(actions)
    if research_prerequisites(venture_id):
        assert "research.prepare" not in actions


@pytest.mark.parametrize("idea,audience,summary", INTAKE_DOMAINS)
def test_confirmed_idea_with_open_review_leads_with_the_review(client, monkeypatch, idea, audience, summary):
    from tests.company_os_harness import append_idea_revision
    from app.chat import experiments
    from app.chat.build_path import research_prerequisites
    from app.chat.next_moves import compose
    from app.core.local_mode import is_local
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from tests.unit.chat.test_skip_confirm_experiment_gate import _skip_and_confirm

    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    planned = []

    async def plan(system, request, venture_id, **kwargs):
        planned.append(request)
        return {"hypothesis": f"{audience} need {summary}.",
                "action": "Prepare your interview script for five consenting people. Nothing is sent.",
                "success_measure": "Pass with three recent costly workarounds; missing interviews are inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", plan)
    venture_id, sid, question_id = _venture(client, idea)
    _send(client, venture_id, "I spoke with two people about this last week.", question_id=question_id, reply_to_question=True)
    _skip_and_confirm(client, venture_id)
    with db()() as session:
        revision = session.get(IdeaRevision, session.get(IdeaSession, sid).current_revision_id)
        fields = json.loads(revision.structured)
        fields.pop("_questions_skipped")
        append_idea_revision(session, revision.id, structured=json.dumps(fields))
        session.commit()
    assert research_prerequisites(venture_id)
    moves = compose(venture_id)["actions"]
    actions = [move["action"] for move in moves]
    if is_local():
        assert moves[0]["href"] == "/settings/local#coding-tools"
        assert moves[0]["label"] == "Connect your coding tool"
    else:
        assert actions[0] == "startup.review"
    assert "research.prepare" not in actions
    assert "experiment.start" in actions
    assert not planned
    experiment = _send(client, venture_id, "Design an experiment")
    from app.core.local_mode import is_local, is_test_mode

    assert f"Prepared your {'Sample ' if not is_local() and is_test_mode() else ''}experiment" in experiment["body_md"]
    assert not any(action.get("confirmation") for action in experiment["actions"])
    assert len(planned) == 1 and "Design an experiment" in planned[0]
