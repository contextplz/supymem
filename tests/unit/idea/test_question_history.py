import json

import pytest
from sqlalchemy import select

from tests.integration.test_chat_first_idea_bridge import client as base_client, _chat_started_venture
from tests.idea_fixtures import completion, install_provider
from tests.security.test_durable_journey_boundary import run_queued


DOMAINS = [
    ("A delivery invoice platform", "Independent freight coordinators",
     "Import delivery records", "Review unmatched invoices", "Export matched invoices"),
    ("A neighborhood leisure app", "Families exploring nearby trails",
     "Choose a nearby route", "Invite family members", "Save favorite walks"),
]


@pytest.fixture()
def client(base_client, monkeypatch):
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    from app.intake import application
    from app.intake.smart import classify_fallback
    from app.ventures import founder_facts

    async def interpret(text, **kwargs):
        return classify_fallback(text).to_dict()

    async def extract(*args, **kwargs):
        return {}

    monkeypatch.setattr(application, "interpret", interpret)
    monkeypatch.setattr(founder_facts, "extract", extract)
    return base_client


@pytest.fixture()
def local_mode(monkeypatch):
    monkeypatch.setenv("COS_TEST_APP_REUSE", "0")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)


def clarifier_venture(client, idea, *, field="workflow"):
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db

    venture_id = _chat_started_venture(client, idea, field=field)
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        assert question.role == "agent" and question.kind == "question"
        question.text = "Who should the changed idea serve?" if field == "customer" else "How should the changed idea work?"
        question.options = json.dumps({"field": field, "options": []})
        row.state = "clarifying"
        session.commit()
    return venture_id


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.parametrize("project_intent", ["startup", "personal"])
def test_apply_completes_when_guard_turns_model_question_into_proposal(client, offer, audience, first, second, third, project_intent):
    from app.clarify.dilemmas import ground_question
    from app.db.models import Job, Workspace
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaSession
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.question_history import CONFIRM, answered_questions, repeated_question
    from app.idea.runner import _apply
    from app.idea.service import _append_revision
    from app.state import db
    from core.clock import utcnow

    created = client.post("/ventures", json={"raw_idea": offer, "start": False})
    assert created.status_code == 201
    venture_id, workspace_id = created.json()["id"], created.json()["workspace_id"]
    now = utcnow()
    understanding = Understanding(summary=offer, offer=offer, customer=audience, problem=first)
    structured = {**understanding.model_dump(), "project_intent": project_intent,
        "_answered_fields": {"offer": offer, "customer": audience, "problem": first}}
    model_question = Question(text="How often does this difficulty occur?", field="problem", why="",
        options=[first, second, third])
    grounded = ground_question(model_question.model_dump(), structured)
    assert grounded["text"] != model_question.text
    result = Clarification(case="know", action="question", message="Your answers are saved.",
        understanding=understanding, question=model_question)
    payload = {"session_id": "idea_guard", "venture_id": venture_id, "workspace_id": workspace_id,
        "_job_id": "job_guard", "_worker_id": "guard-worker", "_job_attempt": 1, "reply_in_chat": True}

    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        workspace.settings_json = json.dumps({**json.loads(workspace.settings_json or "{}"), "project_intent": project_intent})
        row = IdeaSession(id=payload["session_id"], workspace_id=workspace_id, venture_id=venture_id,
            case="know", state="clarifying", questions_asked=1, question_budget=2, created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        revision = _append_revision(session, row, structured, source="founder")
        session.add(ClarifierTurn(id="ict_guard_question", session_id=row.id, workspace_id=workspace_id,
            venture_id=venture_id, turn=1, role="agent", kind="question", text=grounded["text"],
            options=json.dumps(grounded), revision_id=revision.id, created_at=now))
        session.add(ClarifierTurn(id="ict_guard_answer", session_id=row.id, workspace_id=workspace_id,
            venture_id=venture_id, turn=2, role="founder", kind="answer", text=first,
            options=json.dumps({"question_id": "ict_guard_question"}), revision_id=revision.id, created_at=now))
        session.add(Job(id=payload["_job_id"], kind="idea.clarify_turn", workspace_id=workspace_id,
            venture_id=venture_id, payload_json=json.dumps(payload), status="leased", locked_by=payload["_worker_id"],
            attempts=1, created_at=now))
        session.flush()
        session.add(IdeaClarificationRun(id="icr_guard", job_id=payload["_job_id"], session_id=row.id,
            workspace_id=workspace_id, venture_id=venture_id, revision_id=revision.id, state="responded",
            worker_id=payload["_worker_id"], attempt=1, response=result.model_dump_json(), usage="{}",
            snapshot=json.dumps({"understanding": structured, "project_intent": project_intent,
                "answered_fields": structured["_answered_fields"]}), created_at=now, updated_at=now))
        session.flush()
        history = answered_questions(session, row.id)
        assert not repeated_question(model_question.model_dump(), history)
        assert repeated_question(grounded, history)
        session.commit()

    _apply("icr_guard", payload)

    with db()() as session:
        row = session.get(IdeaSession, payload["session_id"])
        turn = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        assert session.get(IdeaClarificationRun, "icr_guard").state == "completed"
        assert row.state == "reviewing"
        assert turn.kind == "proposal" and turn.text == CONFIRM and turn.options is None
        assert turn.revision_id == row.current_revision_id
        assert session.scalar(select(Job).where(Job.venture_id == venture_id, Job.kind == "idea.validate")) is None


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
def test_latest_correction_wins_over_a_later_question(client, offer, audience, first, second, third):
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack.office_hours import saved_snapshot
    from app.clarify.dilemmas import venture_context
    from app.idea.question_history import answered_questions
    from app.state import db
    from core.clock import utcnow

    venture_id = clarifier_venture(client, f"Offer: {offer}\nAudience: {audience}")
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        now = utcnow()
        for offset, role, kind, text, options in [
            (1, "founder", "answer", first, {"question_id": question.id}),
            (2, "agent", "question", "What task should come next?", {"field": "workflow"}),
            (3, "founder", "answer", second, {"question_id": "ict_later_question"}),
            (4, "founder", "answer_correction", third, {"question_id": question.id}),
        ]:
            session.add(ClarifierTurn(id="ict_later_question" if offset == 2 else f"ict_correction_{offset}",
                session_id=row.id, venture_id=venture_id, workspace_id=row.workspace_id, turn=question.turn + offset,
                role=role, kind=kind, text=text, options=json.dumps(options), revision_id=row.current_revision_id,
                created_at=now))
        session.flush()
        history = answered_questions(session, row.id)
        assert history[-1]["founder_text"] == third
        assert history[-1]["field"] == "workflow"
        assert venture_context(saved_snapshot(session, venture_id, row.current_revision_id)) == (audience, third)


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
def test_reused_pressure_test_fact_is_not_a_new_founder_answer(client, offer, audience, first, second, third):
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.clarify.dilemmas import venture_context
    from app.gstack.office_hours import saved_snapshot
    from app.idea.question_history import answered_questions
    from app.idea.runner import _founder_messages
    from app.state import db
    from core.clock import utcnow

    venture_id = clarifier_venture(client, f"Offer: {offer}\nAudience: {audience}")
    previous = "Our customers are museum curators. They use collection cards."
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        for offset, role, kind, text, options in [
            (1, "founder", "answer", first, {"question_id": question.id}),
            (2, "agent", "question", "What do they use today?", {"phase": "gstack", "gstack_id": "Q2", "gstack_name": "Workarounds", "options": []}),
            (3, "founder", "answer", previous, {"reused": True, "question_id": "ict_reused_question"}),
        ]:
            session.add(ClarifierTurn(id="ict_reused_question" if offset == 2 else f"ict_reused_{offset}",
                session_id=row.id, venture_id=venture_id, workspace_id=row.workspace_id, turn=question.turn + offset,
                role=role, kind=kind, text=text, options=json.dumps(options), revision_id=row.current_revision_id,
                created_at=utcnow()))
        session.flush()
        history = answered_questions(session, row.id)
        assert history[-1]["reused"]
        assert venture_context(saved_snapshot(session, venture_id, row.current_revision_id)) == (audience, first)
        assert previous not in _founder_messages({"answered_questions": history,
            "conversation": [{"role": "founder", "kind": "answer", "text": previous, "reused": True}]})


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.parametrize("source", ["clarifier", "gstack"])
@pytest.mark.parametrize("mismatch", ["text", "options"])
def test_wrong_audience_and_job_never_reach_chat_or_continue(client, monkeypatch, offer, audience, first, second, third, source, mismatch):
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack import office_hours
    from app.idea.service import get_session
    from app.state import db
    from core.clock import utcnow

    old_audience, old_job = "Museum curators", "catalogue artifacts"
    snapshots = []

    def response(messages, kwargs):
        snapshots.append(json.loads(messages[-1]["content"]))
        result = completion(proposal=False, text="Which workflow should you start with?" if len(snapshots) == 1 else
            "What would be your first step?" if mismatch == "options" else f"What do {old_audience} do today to {old_job}?",
            options=[] if len(snapshots) == 1 else
            ["Catalogue exhibit objects", "Arrange museum displays", "Restore old paintings"], confidence=0.4,
            understanding={"offer": "An archival collection platform", "customer": old_audience})
        body = json.loads(result.choices[0].message.content)
        body["question"]["field"] = "workflow" if len(snapshots) == 1 else "first_user_path"
        result.choices[0].message.content = json.dumps(body)
        return result

    install_provider(monkeypatch, response)
    venture_id = _chat_started_venture(client, f"Offer: An archival collection platform\nAudience: {old_audience}", pending=False)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    with db()() as session:
        row = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        session_id = row.id
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        question_id = question.id
        if mismatch == "options":
            question.options = json.dumps({**json.loads(question.options), "field": "customer"})
            session.commit()
    answer = f"Offer: {offer}\nAudience: {audience}\nWorkflow: {first}"
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": answer,
        "reply_to_question": True, "question_id": question_id})
    assert sent.status_code == 200, sent.text
    run_queued("idea.clarify_turn")
    if source == "gstack":
        with db()() as session:
            row = session.get(IdeaSession, session_id)
            snapshot = office_hours.saved_snapshot(session, venture_id, row.current_revision_id)
            picks = office_hours.parse_picks(json.dumps({"questions": [{"id": "Q2",
                "question": "What do they do today?" if mismatch == "options" else f"What do {old_audience} do today to {old_job}?",
                "options": ["Catalogue exhibit objects", "Arrange museum displays", "Restore old paintings"]},
                {"id": "Q4", "question": "What is your smallest useful version?",
                 "options": ["One useful task", "One first step", "One clear result"]}]}), snapshot)
            turn = office_hours.add_question_turn(session, row, picks[0], [], 1, 1, utcnow())
            office_hours.chat_question(session, row, turn)
            session.commit()
    saved = get_session(session_id)
    turn = saved["turns"][-1]
    assert old_audience not in turn["text"] and old_job not in turn["text"]
    review = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"})
    assert review.status_code == 200, review.text
    visible = review.json()["body_md"] + json.dumps(review.json()["actions"])
    assert old_audience not in visible and old_job not in visible and "museum" not in visible.casefold()
    assert review.json()["kind"] == "idea_clarification_ready"
    with db()() as session:
        snapshot = office_hours.saved_snapshot(session, venture_id, session.get(IdeaSession, session_id).current_revision_id)
        from app.clarify.dilemmas import venture_context
        assert venture_context(snapshot)[0] == audience
        assert first.casefold() in json.dumps(snapshot).casefold()


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.parametrize("repeat", ["text", "normalized", "field", "saved"])
@pytest.mark.parametrize("with_request", [False, True])
def test_answered_question_never_reaches_chat_or_continue(client, monkeypatch, offer, audience, first, second, third, repeat, with_request):
    from app.core.local_mode import is_local
    from app.chat import router
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaSession
    from app.state import db

    local = is_local()
    question_text = f"What task should your product help {audience} do first?"
    local_field = "workflow"
    snapshots = []

    def response(messages, kwargs):
        snapshot = json.loads(messages[-1]["content"])
        snapshots.append(snapshot)
        intermediate = repeat == "text" and with_request and len(snapshots) == 2
        text = (("What should you change about this?" if local else "Which task belongs in the initial workflow?")
                if repeat == "field" and len(snapshots) > 1
                else "  " + question_text.upper().replace("?", " !") + "  " if repeat == "normalized" and len(snapshots) > 1
                else question_text)
        result = completion(proposal=False, text="How should you pay for the first version?" if intermediate else text,
            options=["One recurring plan", "Pay for each use", "Usage plus a fixed fee"] if intermediate else [first, second, third], confidence=0.4,
            message="Your choices are saved.", understanding={"offer": offer, "customer": audience})
        body = json.loads(result.choices[0].message.content)
        body["question"]["field"] = "pricing_hypothesis" if intermediate else "constraints" if len(snapshots) > 1 and repeat != "field" else local_field
        result.choices[0].message.content = json.dumps(body)
        return result

    install_provider(monkeypatch, response)
    venture_id = _chat_started_venture(client, f"Offer: {offer}\nAudience: {audience}", pending=False)
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        session_id = idea.id
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        assert question.kind == "question"
        if local:
            question_text = question.text
            local_field = json.loads(question.options)["field"]
        else:
            assert question.text == question_text
        question_id = question.id

    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")

    async def dispatch(*args, **kwargs):
        return {"kind": "text", "body_md": "Your request is saved.", "actions": [], "citations": []}

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": first + "\nAlso, show my next steps" if with_request else first,
        "reply_to_question": True, "question_id": question_id})
    assert sent.status_code == 200, sent.text
    run_queued("idea.clarify_turn")
    if repeat == "text" and with_request:
        pending = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"})
        assert pending.status_code == 200, pending.text
        action = next(action for action in pending.json()["actions"] if action.get("text") == "One recurring plan")
        sent = client.post(f"/ventures/{venture_id}/chat", json={"message": "One recurring plan. Also, show my next steps",
            "reply_to_question": True, "question_id": action["payload"]["question_id"]})
        assert sent.status_code == 200
        run_queued("idea.clarify_turn")
    assert len(snapshots) == (3 if repeat == "text" and with_request else 2)
    history = client.get(f"/ventures/{venture_id}/chat").json()["items"]
    assert sum(question_text in message["body_md"] for message in history if message["role"] == "assistant") == 1

    if repeat == "saved":
        with db()() as session:
            turn = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id)
                .order_by(ClarifierTurn.turn.desc()).limit(1))
            turn.kind, turn.text = "question", question_text
            turn.options = json.dumps({"field": "constraints", "options": [first, second, third]})
            repeated_question_id = turn.id
            session.get(IdeaSession, session_id).state = "clarifying"
            session.commit()
        from app.idea.question_history import guard_pending_question
        from app.tenancy import bind
        with db()() as session, monkeypatch.context() as viewer:
            row = session.get(IdeaSession, session_id)
            viewer.setattr("app.workspaces.service.role_of", lambda *args: "viewer")
            with bind(workspace_id=row.workspace_id, venture_id=row.venture_id, actor="usr_viewer"):
                assert not guard_pending_question(session, row)
            assert session.get(ClarifierTurn, repeated_question_id).kind == "question"

    continued = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"})
    assert continued.status_code == 200, continued.text
    assert question_text not in continued.json()["body_md"]
    assert all(action.get("payload", {}).get("question_id") != question_id for action in continued.json()["actions"])
    saved = client.get(f"/ventures/{venture_id}/idea/sessions/{session_id}").json()
    if local and saved["turns"][-1]["kind"] == "question":
        from app.idea.question_history import repeated_question
        following = saved["turns"][-1]
        assert saved["state"] == "clarifying"
        assert not repeated_question({**following["options"], "text": following["text"]}, snapshots[-1]["answered_questions"])
    else:
        assert saved["turns"][-1]["kind"] == "proposal"
        assert saved["state"] == "reviewing"
        assert any(action["action"] == "idea.proposal.approve" for action in continued.json()["actions"])
    answered = next(pair for pair in snapshots[-1]["answered_questions"] if pair["question_id"] == question_id)
    assert answered["question"] == question_text and answered["answer"] == first
    with db()() as session:
        runs = session.scalars(select(IdeaClarificationRun).where(IdeaClarificationRun.session_id == session_id)).all()
        assert all(run.state == "completed" for run in runs)
    if repeat == "saved" and with_request:
        if local and saved["turns"][-1]["kind"] == "question":
            skipped = client.post(f"/ventures/{venture_id}/chat", json={"message": "Skip the questions"})
            assert skipped.status_code == 200 and skipped.json()["kind"] == "confirmation", skipped.text
            saved = client.get(f"/ventures/{venture_id}/idea/sessions/{session_id}").json()
        from app.chat.idea_proposal import EDIT_QUESTION
        for _ in range(2):
            edited = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.proposal.edit",
                "payload": {"session_id": session_id, "revision_id": saved["current_revision"]["id"]}})
            assert edited.status_code == 200 and EDIT_QUESTION in edited.json()["body_md"]
            saved = client.get(f"/ventures/{venture_id}/idea/sessions/{session_id}").json()
            sent = client.post(f"/ventures/{venture_id}/chat", json={"message": f"Keep the first version focused on {first.lower()}",
                "reply_to_question": True, "question_id": saved["turns"][-1]["id"]})
            assert sent.status_code == 200, sent.text
            run_queued("idea.clarify_turn")
            saved = client.get(f"/ventures/{venture_id}/idea/sessions/{session_id}").json()
            assert saved["turns"][-1]["kind"] == "proposal"
        edited = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.proposal.edit",
            "payload": {"session_id": session_id, "revision_id": saved["current_revision"]["id"]}})
        assert edited.status_code == 200, edited.text
        saved = client.get(f"/ventures/{venture_id}/idea/sessions/{session_id}").json()
        sent = client.post(f"/ventures/{venture_id}/chat", json={"message": "I want to change my idea. A different project.",
            "reply_to_question": True, "question_id": saved["turns"][-1]["id"]})
        assert sent.status_code == 200, sent.text
        with db()() as session:
            from app.idea.question_history import answered_questions
            assert answered_questions(session, session_id) == []


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.parametrize("match", ["text", "field", "workflow"])
def test_office_hours_cannot_repeat_an_answered_clarifier_question(client, monkeypatch, offer, audience, first, second, third, match):
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack import office_hours
    from app.state import db
    from core.clock import utcnow

    field = "customer" if match == "field" else "workflow"
    venture_id = clarifier_venture(client, f"Offer: {offer}\nAudience: {audience}", field=field)
    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        session.add(ClarifierTurn(id="ict_history_answer", session_id=idea.id, workspace_id=idea.workspace_id,
            venture_id=venture_id, turn=question.turn + 1, role="founder", kind="answer", text=audience if match == "field" else first,
            options=json.dumps({"question_id": question.id, "field": field}), created_at=utcnow()))
        session.flush()
        repeated = {**office_hours.default_picks()[2 if match == "field" else 1],
            **({"text": question.text} if match == "text" else {}),
            "options": [first, second, third], "contextual_options": True}
        if match == "workflow":
            repeated.update(gstack_id="Q4", gstack_name="Narrowest Wedge", text="Which task should your first release complete?")
        assert office_hours.unanswered_picks(session, idea, [repeated]) == []
        turn = office_hours.add_question_turn(session, idea, repeated, [], 1, 1, utcnow())
        assert turn.kind == "proposal"
        assert idea.state == "reviewing"
        office_hours.add_question_turn(session, idea, repeated, [], 1, 1, utcnow())
        reviews = session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == "idea.validate")).all()
        assert len(reviews) == 1 and json.loads(reviews[0].payload_json).get("stage") != "gstack_questions"
        next_pick = office_hours.default_picks()[0]
        following = office_hours.add_question_turn(session, idea, repeated, [repeated, next_pick], 1, 3, utcnow())
        assert following.kind == "question" and json.loads(following.options)["gstack_id"] == next_pick["gstack_id"]
        posted = []
        monkeypatch.setattr("app.chat.milestones.post_milestone", lambda *args, **kwargs: posted.append(kwargs))
        office_hours.chat_question(session, idea, question)
        assert posted == [] and question.kind == "question"


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
def test_vague_answer_gets_distinct_bounded_followups(client, offer, audience, first, second, third):
    from app.idea.question_history import question_field, unanswered_question

    question = {"text": f"Who would use {offer}?", "field": "customer", "options": [audience]}
    history = [{"question": question["text"], "field": "audience", "answer": "everyone", "resolved": False}]
    for _ in range(2):
        following = unanswered_question(question, history)
        assert following and following["text"] not in [pair["question"] for pair in history]
        assert question_field(following) == "audience"
        history.append({"question": following["text"], "field": "audience", "answer": "people", "resolved": False})
    assert unanswered_question(question, history) is None


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.usefixtures("local_mode")
def test_local_idea_save_shows_only_the_first_question(client, monkeypatch, offer, audience, first, second, third):
    from app.db.models import Job
    from app.idea import runner
    from app.state import db

    def respond(messages, kwargs):
        snapshot = json.loads(messages[-1]["content"])
        understanding = {**snapshot["understanding"]}
        if understanding.get("current_workaround"):
            understanding["current_workaround"] = understanding["current_workaround"][:2000]
        if snapshot["force_propose"]:
            return completion(understanding=understanding, message="Review your saved idea.")
        with db()() as session:
            job = session.scalar(select(Job).where(Job.kind == runner.PURPOSE, Job.status == "leased"))
            payload = {**json.loads(job.payload_json), "_job_id": job.id,
                "_worker_id": job.locked_by, "_job_attempt": job.attempts}
        runner._post_progress(payload, 1, runner.PURPOSE)
        reply = completion(proposal=False, understanding=understanding,
            text=f"How should {audience} complete their first task?", options=[first, second, third],
            message="Your answers are saved.", confidence=0.95)
        data = json.loads(reply.choices[0].message.content)
        data["question"]["field"] = "requirements" if snapshot.get("answered_questions") else "workflow"
        reply.choices[0].message.content = json.dumps(data)
        return reply

    install_provider(monkeypatch, respond)
    venture_id = _chat_started_venture(client,
        idea=f"A tool that helps {audience} {first.lower()}", pending=False)
    messages = client.get(f"/ventures/{venture_id}/chat").json()["items"]
    assistant = [message for message in messages if message["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["kind"] == "idea_clarification_ready"
    assert "answers are saved" not in assistant[0]["body_md"].lower()
    assert "?" in assistant[0]["body_md"]
    from app.db.supymem_models import IdeaSession
    from app.idea.service import get_session
    with db()() as session:
        idea_id = session.scalar(select(IdeaSession.id).where(IdeaSession.venture_id == venture_id))
    assert get_session(idea_id)["turns"][-1]["options"]["field"] == "current_workaround"
    answer = (f"Today {audience} use a shared spreadsheet and spend an hour comparing their notes manually. "
        + "They copy each result into the spreadsheet and compare entries by hand. " * 50).strip()
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": answer})
    assert sent.status_code == 200, sent.text
    run_queued("idea.clarify_turn")
    saved = get_session(idea_id)
    assert saved["turns"][-1]["options"]["field"] == "evidence"
    evidence = f"I spoke with three people among {audience} and watched one of them spend an hour doing this manually."
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": evidence})
    assert sent.status_code == 200, sent.text
    run_queued("idea.clarify_turn")
    proposed = client.post(f"/ventures/{venture_id}/idea/sessions/{idea_id}/propose")
    assert proposed.status_code == 200, proposed.text
    run_queued("idea.clarify_turn")
    final = client.get(f"/ventures/{venture_id}/chat").json()["items"][-1]["body_md"]
    assert answer in final and evidence in final
    saved = get_session(idea_id)
    assert saved["state"] == "reviewing" and saved["turns"][-1]["kind"] == "proposal"
    assert any(turn["role"] == "founder" and turn["text"] == answer for turn in saved["turns"])
    assert any(turn["role"] == "founder" and turn["text"] == evidence for turn in saved["turns"])


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.usefixtures("local_mode")
def test_local_duplicate_job_question_uses_a_remaining_unknown(client, offer, audience, first, second, third):
    from app.idea.question_history import repeated_question, unanswered_question

    history = [{"question": f"Which task should {audience} complete first?", "field": "workflow",
        "answer": first, "founder_text": first, "resolved": True}]
    question = {"text": f"Which capabilities would let {audience} do that?", "field": "requirements", "options": []}
    assert repeated_question(question, history)
    snapshot = {"project_intent": "startup", "original_idea": offer,
        "understanding": {"offer": first, "customer": audience}, "answered_questions": history,
        "answered_fields": {"workflow": first}, "question_fields_asked": ["workflow"]}
    following = unanswered_question(question, history, snapshot=snapshot)
    assert following and following["field"] == "current_workaround"
    assert not repeated_question(following, history)
    snapshot["answered_fields"]["offer"] = first
    unresolved = [{**history[0], "resolved": False}]
    following = unanswered_question(question, unresolved, snapshot=snapshot)
    assert following and following["field"] == "current_workaround"
    flat = {**snapshot["understanding"], "_answered_fields": snapshot["answered_fields"], "idea": offer}
    assert unanswered_question(question, unresolved, snapshot=flat)["field"] == "current_workaround"
    from app.idea.agent import Clarification, Question, Understanding
    from app.idea.runner import _core_clarification
    snapshot = {"project_intent": "startup", "original_idea": f"A platform for {audience}",
        "understanding": {"customer": audience}, "answered_questions": [{**unresolved[0], "field": "offer"}],
        "conversation": [{"role": "founder", "kind": "answer", "field": "offer", "text": first}],
        "question_fields_asked": ["offer"], "active_core_field": "offer", "questions_asked": 1}
    result = _core_clarification(snapshot, Clarification(case="know", action="question", message="Choose your next step.",
        understanding=Understanding(offer=first, customer=audience), question=Question.model_validate({**question, "why": ""})))
    assert result.question and result.question.field == "current_workaround"


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.usefixtures("local_mode")
def test_confirm_summary_includes_all_saved_answers(client, offer, audience, first, second, third):
    from app.db.supymem_models import IdeaSession
    from app.idea.agent import Understanding
    from app.idea.runner import _understanding_summary
    from app.idea.service import _append_revision, patch_locked
    from app.state import db
    from core.clock import utcnow

    created = client.post("/ventures", json={"raw_idea": offer, "start": False}).json()
    answers = {"offer": offer, "customer": audience, "problem": first, "workflow": second,
        "first_user_path": third, "pricing_hypothesis": "A monthly subscription of 29 dollars",
        "current_workaround": "Compare printed notes by hand", "why_now": "Their existing tool stops working next month",
        "evidence": "Three people asked to try it", "desired_outcome": "Finish the task in less than ten minutes",
        "requirements": "Import existing records without losing any notes",
        "constraints": "Work without an internet connection"}
    with db()() as session:
        row = IdeaSession(id="idea_all_answers", workspace_id=created["workspace_id"], venture_id=created["id"],
            case="know", state="clarifying", created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        session.flush()
        revision = _append_revision(session, row, {"_answered_fields": answers}, source="agent")
        structured = json.loads(revision.structured)
        summary = _understanding_summary(Understanding.model_validate(
            {key: value for key, value in structured.items() if key in Understanding.model_fields}))
        assert all(value in summary for value in answers.values())
        session.commit()
        patch = {"requirements": ["Keep every saved note when importing"], "current_workaround": "Compare printed notes by hand"}
        corrected = json.loads(patch_locked(session, row, patch).structured)
        assert all(corrected[field] == corrected["_answered_fields"][field] == value for field, value in patch.items())


@pytest.mark.parametrize("offer,audience,first,second,third", DOMAINS)
@pytest.mark.usefixtures("local_mode")
def test_answer_to_job_does_not_discard_original_idea_context(client, offer, audience, first, second, third):
    from app.idea.runner import _founder_messages

    original = f"{offer} for {audience}. It must work without an internet connection."
    snapshot = {"founder_idea": original, "original_idea": original,
        "answered_fields": {"offer": first}, "conversation": [{"role": "founder", "kind": "answer", "text": first}]}
    sources = _founder_messages(snapshot)
    assert original in sources and first in sources
