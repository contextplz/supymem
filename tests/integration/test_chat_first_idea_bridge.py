"""A chat-started idea continues from its saved question in venture chat."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/chat_bridge.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    from tests.company_os_harness import company_os_test_client
    with company_os_test_client(main.create_app()) as api:
        yield api


def _turn_jobs(venture_id):
    from app.db.models import Job
    from app.state import db

    with db()() as session:
        return session.scalar(select(func.count()).select_from(Job).where(
            Job.venture_id == venture_id, Job.kind == "idea.clarify_turn"))


def _chat_started_venture(client, idea="An order tracking tool for independent bookshops", *, pending=True, field="customer"):
    preview = client.post("/entry/chat/preview", json={
        "text": idea,
    })
    assert preview.status_code == 200, preview.text
    committed = client.post("/entry/chat/confirm", json={
        "proposal_id": preview.json()["proposal"]["id"],
    })
    assert committed.status_code == 200, committed.text
    venture_id = committed.json()["venture_id"]
    opened = client.post(f"/ventures/{venture_id}/idea/sessions")
    assert opened.status_code == 201, opened.text
    from tests.security.test_durable_journey_boundary import run_queued
    run_queued("idea.clarify_turn")
    if pending:
        saved = client.get(f"/ventures/{venture_id}/idea/sessions/{opened.json()['id']}").json()
        if saved["turns"][-1]["kind"] == "proposal":
            edited = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.proposal.edit",
                "payload": {"session_id": saved["id"], "revision_id": saved["current_revision"]["id"]}})
            assert edited.status_code == 200, edited.text
            import json
            from app.db.supymem_models import ClarifierTurn
            from app.state import db
            with db()() as session:
                question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == saved["id"])
                    .order_by(ClarifierTurn.turn.desc()).limit(1))
                question.text = "Who should the changed idea serve?" if field == "customer" else "How should the changed idea work?"
                question.options = json.dumps({**json.loads(question.options or "{}"), "field": field})
                session.commit()
        shown = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review", "payload": {}})
        assert shown.status_code == 200, shown.text
    return venture_id


def test_new_chat_confirmation_always_reaches_nonempty_gstack_questions(client, monkeypatch):
    from app.config import settings
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued
    monkeypatch.setattr(settings, "new_user_flow", "on")
    install_provider(monkeypatch, lambda messages, kwargs: completion(proposal=True, understanding={
        "summary": "Collision repair estimating", "problem": "Manual collision repair estimating",
        "customer": "Collision repair shop owners", "offer": "A tool that helps shops prepare estimates"}))
    preview = client.post("/entry/chat/preview", json={"text": "A tool that helps collision repair shops prepare repair estimates"})
    assert preview.status_code == 200, preview.text
    proposal = preview.json()["proposal"]
    confirmed = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert confirmed.status_code == 200, confirmed.text
    vid = confirmed.json()["venture_id"]
    run_queued("idea.clarify_turn")
    sessions = client.get(f"/ventures/{vid}/idea/sessions").json()
    sid = sessions["items"][0]["id"]
    saved = client.get(f"/ventures/{vid}/idea/sessions/{sid}").json()
    assert saved["state"] == "validating"
    assert saved["turns"][-1]["text"].strip()
    assert saved["turns"][-1]["options"]["phase"] == "gstack"
    assert saved["gstack_answers"] == []
    assert _turn_jobs(vid) == 1


def test_offline_core_questions_keep_answers_and_advance_after_two_followups(client):
    from app.idea.service import get_session, post_message
    from tests.security.test_durable_journey_boundary import run_queued

    created = client.post("/ventures", json={"raw_idea": "A generic AI app", "start": False})
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    opened = client.post(f"/ventures/{vid}/idea/sessions")
    assert opened.status_code == 201, opened.text
    sid = opened.json()["id"]
    run_queued("idea.clarify_turn")
    assert get_session(sid)["turns"][-1]["options"]["field"] == "offer"
    for answer, expected in [("something", "offer"), ("anything", "customer"),
                             ("everyone", "customer"), ("people", "customer"), ("skip", None)]:
        queued = post_message(sid, answer, reply_in_chat=True)
        run_queued("idea.clarify_turn", through_job_id=queued["job_id"])
        saved = get_session(sid)
        assert any(turn["role"] == "founder" and turn["text"] == answer for turn in saved["turns"])
        assert (saved["turns"][-1].get("options") or {}).get("field") == expected
    assert saved["state"] == "reviewing"
    assert len(saved["current_revision"]["structured"]["assumptions"]) == 2
    assert saved["current_revision"]["structured"]["offer"] == ""
    assert saved["current_revision"]["structured"]["customer"] == ""


@pytest.mark.parametrize("offer,audience", [
    ("A tool that tracks flour inventory", "Neighbourhood bakery owners"),
    ("A tool that tracks flour inventory", "Not sure"),
    ("Not sure", "Not sure"),
])
def test_smalltalk_followup_can_reach_a_persisted_venture(client, offer, audience):
    before = client.get("/ventures").json()["items"]
    preview = client.post("/entry/chat/preview", json={"text": "hello"})
    assert preview.status_code == 200, preview.text
    hello = preview.json()
    assert hello["status"] == "clarify" and hello["onboarding_field"] == "context"  # small talk starts discovery
    assert hello["onboarding_question"]
    assert client.get("/ventures").json()["items"] == before

    text = f"hello\nClarification: {offer}"
    preview = client.post("/entry/chat/preview", json={"text": text})
    assert preview.status_code == 200, preview.text
    next_question = preview.json().get("proposal") or preview.json()
    if offer == "Not sure":  # only small talk so far: nothing to suggest from, so onboarding moves on
        assert next_question["onboarding_field"] == "context"
        assert client.get("/ventures").json()["items"] == before
        return  # discovery keeps going until they name an idea
    assert next_question["onboarding_field"] == "customer"
    text += f"\nAudience: {audience}"
    preview = client.post("/entry/chat/preview", json={"text": text})
    assert preview.status_code == 200, preview.text
    if audience == "Not sure":
        next_question = preview.json().get("proposal") or preview.json()
        assert next_question["onboarding_field"] == "customer" and next_question["onboarding_question"]
        assert next_question["onboarding_choices"] == []
        assert client.get("/ventures").json()["items"] == before
        text += "\nAudience: Neighbourhood bakery owners"
        preview = client.post("/entry/chat/preview", json={"text": text})
        assert preview.status_code == 200, preview.text
    proposal = preview.json()["proposal"]
    assert proposal["state"] == "pending" and not proposal["onboarding_question"]
    committed = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": "QA Bakery"})
    assert committed.status_code == 200, committed.text
    receipt = committed.json()
    assert receipt["status"] == "committed" and receipt["venture_id"]
    saved = client.get(f"/ventures/{receipt['venture_id']}").json()
    assert saved["name"] == "QA Bakery" and saved["raw_idea"] == text
    history = client.get("/entry/chat/history").json()["items"]
    assert any(item["text"] == text and (item["proposal"].get("receipt") or {}).get("venture_id") == saved["id"]
               for item in history)


def test_chat_answer_queues_one_idea_turn_without_generic_task(client):
    from app.db.models import PlannedTask
    from app.state import db

    venture_id = _chat_started_venture(client)
    first = _turn_jobs(venture_id)  # the first question's own model turn
    answer = "Independent bookshops that still manage orders on paper"
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": answer})
    assert sent.status_code == 200, sent.text
    assert sent.json()["kind"] == "idea_clarification_pending"
    history = client.get(f"/ventures/{venture_id}/chat").json()["items"]
    assert history[-2]["role"] == "user" and history[-2]["body_md"] == answer
    assert history[-1]["kind"] == "idea_clarification_pending"
    assert _turn_jobs(venture_id) == first + 1
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(PlannedTask).where(
            PlannedTask.venture_id == venture_id)) == 0


@pytest.mark.parametrize("offer,audience,place", [
    ("Stock reconciliation software", "Independent hardware shop owners", "Winnipeg"),
    ("Weekly meal kit subscriptions", "Parents cooking after work", "Adelaide"),
])
@pytest.mark.parametrize("with_request", [False, True])
def test_mixed_answer_background_question_stays_behind_continue_chip(client, monkeypatch, offer, audience, place, with_request):
    import json

    from app.chat import router
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued

    venture_id = _chat_started_venture(client, f"{offer} for {audience}", field="workflow")
    _, turns, before_chat = _ideas(venture_id)
    question_id = turns[-1].id
    answer = f"Open a daily self-serve workflow for {audience}"
    request = f"find me 6 {audience} in {place} to talk to"
    with db()() as session:
        question = session.get(ClarifierTurn, question_id)
        options = json.loads(question.options or "{}")
        options["options"] = [answer]
        question.options = json.dumps(options)
        session.commit()

    def response(messages, kwargs):
        result = completion(proposal=False, message=f"You can refine the daily workflow for {audience}.",
            text="What should the first daily workflow include?",
            options=["One daily checklist", "A weekly overview"], confidence=0.4,
            understanding={"summary": offer, "offer": offer, "customer": audience,
                           "problem": "Manual coordination of daily work"})
        body = json.loads(result.choices[0].message.content)
        body["question"]["field"] = "workflow"
        result.choices[0].message.content = json.dumps(body)
        return result

    install_provider(monkeypatch, response)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    routed = []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append(text)
        return {"kind": "next_up", "body_md": "Your search is queued. Review the results next.",
                "actions": [], "citations": []}

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": answer + ". Also, " + request if with_request else answer,
        "reply_to_question": True, "question_id": question_id})
    assert sent.status_code == 200, sent.text
    assert routed == (["Also, " + request] if with_request else [])
    if with_request:
        assert sent.json()["actions"] == []
    else:
        assert sent.json()["kind"] == "idea_clarification_pending"
    _, saved_turns, _ = _ideas(venture_id)
    assert saved_turns[-2].id == question_id and saved_turns[-1].text == answer
    run_queued("idea.clarify_turn")
    _, saved_turns, after_chat = _ideas(venture_id)
    assert saved_turns[-1].role == "agent" and saved_turns[-1].kind == ("proposal" if offer == "Weekly meal kit subscriptions" else "question")
    assert len(after_chat) == len(before_chat) + 1  # the next question is shown, not left behind a chip
    continued = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"})
    assert continued.status_code == 200, continued.text
    assert continued.json()["kind"] == ("confirmation" if offer == "Weekly meal kit subscriptions" else "idea_clarification_ready")
    assert all(action.get("payload", {}).get("question_id") == saved_turns[-1].id
               for action in continued.json()["actions"] if action.get("action") == "chat.reply")


def test_unrelated_chat_command_does_not_answer_idea_question(client):
    from app.chat.idea_bridge import pending_question

    venture_id = _chat_started_venture(client)
    first = _turn_jobs(venture_id)
    assert pending_question(venture_id, "Show my credits") is None
    assert pending_question(venture_id, "What should I do next?") is None
    for answer in (
        "Clinic front-desk staff at small physio clinics phone patients from a paper waitlist.",
        "Recruiters who post jobs on LinkedIn and screen applicants by hand today.",
        "Freelancers who track invoices in Excel and chase payments by email.",
    ):
        assert pending_question(venture_id, answer) is not None, answer
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": "Show my credits"})
    assert sent.status_code == 200, sent.text
    assert _turn_jobs(venture_id) == first


def test_informational_requests_leave_the_saved_question_pending(client, monkeypatch):
    from app.chat.idea_bridge import pending_question
    from app.idea import service

    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    venture_id = _chat_started_venture(client)
    question = pending_question(venture_id, "Owned descriptive answer about independent bookshops")
    assert question is not None
    before = _ideas(venture_id)[1]
    first = _turn_jobs(venture_id)
    answer_calls = []
    original = service.post_message

    def recorded_answer(*args, **kwargs):
        answer_calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "post_message", recorded_answer)
    for text in (
        "Explain what information is saved for this venture.",
        "Please describe the idea and what remains unknown.",
        "Could you summarize our current assumptions in a few sentences.",
        "Please compare the saved idea with the available research evidence.",
        "List the information you can rely on for this venture.",
        "Tell me what this venture is meant to do and what is still unproven.",
        "Help me understand the current venture without taking any actions.",
        "Without running any tasks, explain the saved purpose and remaining unknowns.",
        "Please, without starting new work, summarize the available evidence.",
        "Do not change anything. Tell me what the current saved state means.",
        "Advice only: explain the current assumptions.",
        "I want to understand which information is supported by saved evidence.",
    ):
        assert pending_question(venture_id, text) is None, text
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": text})
        assert response.status_code == 200, response.text
        assert response.json()["kind"] != "idea_clarification_pending", text
        assert "saved your answer" not in response.json()["body_md"].lower(), text
        assert answer_calls == [], text
        assert pending_question(venture_id, "Owned descriptive answer about independent bookshops") == question
        assert _turn_jobs(venture_id) == first
    assert [(row.id, row.role, row.kind, row.text) for row in _ideas(venture_id)[1]] == [
        (row.id, row.role, row.kind, row.text) for row in before]


def test_read_request_words_inside_descriptive_answers_remain_answers(client):
    from app.chat.idea_bridge import informational_request, pending_question

    venture_id = _chat_started_venture(client)
    for answer in (
        "Independent bookshops explain what customers should do when their ordered books arrive.",
        "People who summarize long reading lists and compare new editions by hand today.",
        "A list of independent bookshops whose staff tell me about missing customer orders.",
        "List of independent bookshops whose staff track current orders on paper today.",
        "Without spreadsheets, independent bookshop staff compare paper records and explain delayed orders.",
    ):
        assert not informational_request(answer), answer
        assert pending_question(venture_id, answer) is not None, answer
    # A selected saved option retains its explicit answer authority even when
    # its wording looks like an informational request. Foreign ownership does not.
    option = "Explain the ordering process to customers at the counter"
    assert pending_question(venture_id, option) is None
    assert pending_question(venture_id, option, force=True) is not None
    assert pending_question("ven_missing", option, force=True) is None


def test_switching_existing_venture_to_gtm_stops_old_idea_answer_capture(client):
    from app.chat.idea_bridge import pending_question

    venture_id = _chat_started_venture(client)
    answer = "Independent bookshops that still manage orders on paper"
    assert pending_question(venture_id, answer) is not None
    preview = client.post("/entry/chat/preview", json={
        "text": "Our live booking app serves bookshops; help us find customers",
        "target_venture_id": venture_id,
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["proposal"]["route"] == "gtm"
    committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
    assert committed.status_code == 200, committed.text
    assert pending_question(venture_id, answer) is None


def test_legacy_chat_entry_preserves_defer_flag_and_scopes_intake_marker(client):
    import json

    from app.chat.idea_bridge import pending_question
    from app.db.models import Venture, VentureDocument, Workspace
    from app.idea.service import _chat_entry_deferred
    from app.state import db

    venture_id = _chat_started_venture(client)
    answer = "Independent bookshops that still manage orders on paper"
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
        session.delete(session.get(VentureDocument, f"intake_decision:{venture_id}"))
        session.commit()
    for goal, marker, captures in [("startup", None, True), ("personal_build", venture_id, True),
                                  ("startup", "ven_sibling", False), ("gtm", venture_id, False)]:
        with db()() as session:
            workspace = session.get(Workspace, workspace_id)
            settings = json.loads(workspace.settings_json)
            settings.update(entry_goal=goal, intake_venture_id=marker)
            workspace.settings_json = json.dumps(settings)
            session.commit()
            assert _chat_entry_deferred(session, workspace_id) is True
        assert bool(pending_question(venture_id, answer)) is captures


def test_option_reply_answers_pending_question_even_if_it_looks_like_a_command(client):
    import json

    from app.chat.idea_bridge import pending_question
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    venture_id = _chat_started_venture(client, field="current_solution")
    option = "Open the paper list and call patients one by one"
    with db()() as session:
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.venture_id == venture_id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        options = json.loads(question.options or "{}")
        options["options"] = [option]
        question.options = json.dumps(options)
        expected = question.session_id, question.id
        session.commit()
    assert pending_question(venture_id, option) == expected
    # The flag skips only the wording heuristics; ownership checks still apply.
    assert pending_question("ven_missing", option, force=True) is None
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": option, "reply_to_question": True})
    assert sent.status_code == 200, sent.text
    assert sent.json()["kind"] == "idea_clarification_pending"
    assert "No product build" not in sent.json()["body_md"] and "answer is saved" in sent.json()["body_md"]


def test_repeat_chip_click_is_not_saved_twice_or_applied_to_a_newer_question(client):
    from app.chat.idea_bridge import pending_question

    venture_id = _chat_started_venture(client)
    session_id, question_id = pending_question(venture_id, "", force=True)
    saved = client.get(f"/ventures/{venture_id}/idea/sessions/{session_id}").json()
    body = {"message": saved["current_revision"]["structured"]["customer"], "reply_to_question": True, "question_id": question_id}
    assert client.post(f"/ventures/{venture_id}/chat", json=body).json()["kind"] == "idea_clarification_pending"
    before = len(client.get(f"/ventures/{venture_id}/chat").json()["items"])
    again = client.post(f"/ventures/{venture_id}/chat", json=body)
    assert again.status_code == 200 and "already answered" in again.json()["body_md"]
    assert len(client.get(f"/ventures/{venture_id}/chat").json()["items"]) == before  # no duplicate in chat
    assert client.post(f"/ventures/{venture_id}/chat", json={**body, "question_id": "ict_other"}).json()["id"].startswith("stale-")


def test_question_options_become_chat_reply_actions(client):
    from app.idea.runner import _question_reply

    body, actions = _question_reply("Which?", {"options": ["A", "B" * 100], "selection_mode": "single"})
    assert body.endswith("Select any that fit, then submit, or type or speak your own answer below.")
    assert "Suggested:" not in body
    assert actions[0]["payload"]["recommended"] is True
    assert actions[1]["payload"]["recommended"] is False
    assert [a["action"] for a in actions] == ["chat.reply"] * 2
    assert actions[1]["text"] == actions[1]["label"] == "B" * 100
    assert "Select any that fit" in _question_reply("Which?", {"options": ["A"], "selection_mode": "multiple"})[0]
    assert _question_reply("Which?", {"options": []}) == ("Which?\n\nReply here with your answer.", None)


def _ideas(venture_id):
    from app.db.models import ChatMessage
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db

    with db()() as session:
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id))
        turns = list(session.scalars(select(ClarifierTurn).where(
            ClarifierTurn.session_id == idea.id).order_by(ClarifierTurn.turn)))
        chat = list(session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.kind == "idea_clarification_ready")))
        session.expunge_all()
    return idea, turns, chat


def _model_first_question(client, monkeypatch, **kwargs):
    from tests.idea_fixtures import completion, install_provider
    from tests.security.test_durable_journey_boundary import run_queued

    def response(*args):
        import json
        result = completion(proposal=False, **kwargs)
        data = json.loads(result.choices[0].message.content)
        data["question"]["field"] = "workflow"
        result.choices[0].message.content = json.dumps(data)
        return result
    calls = install_provider(monkeypatch, response)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    preview = client.post("/entry/chat/preview", json={"text": "An order tracking tool for independent bookshops"})
    assert preview.status_code == 200, preview.text
    committed = client.post("/entry/chat/confirm", json={
        "proposal_id": preview.json()["proposal"]["id"], "name": "BookHarbor",
    })
    assert committed.status_code == 200, committed.text
    venture_id = committed.json()["venture_id"]
    assert client.post(f"/ventures/{venture_id}/idea/sessions").status_code == 201
    run_queued("idea.clarify_turn")
    return venture_id, calls


def test_first_question_is_model_generated_with_idea_specific_options(client, monkeypatch):
    import json

    venture_id, calls = _model_first_question(
        client, monkeypatch, text="Do your bookshops take special orders by phone or in person?",
        options=["By phone", "In person at the counter", "Both"], message="Independent bookshops is a clear place to start.")
    # The founder supplied a name; naming suggestions are opt-in. This fixture
    # has no preventure credit account, so the paid call is the clarification.
    clarification = [(messages, kwargs) for messages, kwargs in calls
                     if kwargs.get("purpose") == "idea.clarify_turn"]
    assert len(clarification) == 1
    assert "independent bookshops" in json.loads(clarification[0][0][-1]["content"])["original_idea"]
    assert len(calls) == 1
    assert client.get(f"/ventures/{venture_id}").json()["name"] == "BookHarbor"
    idea, turns, chat = _ideas(venture_id)
    question = turns[-1]
    assert question.kind == "question" and "bookshops" in question.text and idea.questions_asked == 1
    # The job question was replaced by the saved template, so choices written for the model's wording are gone.
    assert not {"By phone", "In person at the counter", "Both"} & set(json.loads(question.options)["options"])
    assert len(chat) == 1
    assert "**Help me understand your idea better**" in chat[0].body_md and chat[0].body_md.startswith("<!--employee:")
    assert "clear place to start" in chat[0].body_md and question.text in chat[0].body_md
    labels = [a["label"] for a in json.loads(chat[0].actions_json)]
    assert labels == ["Skip the questions"]


def test_first_review_is_deterministic_when_the_model_is_unavailable(client, monkeypatch):
    venture_id = _chat_started_venture(client, pending=False)
    idea, turns, chat = _ideas(venture_id)
    assert turns[-1].kind == "proposal" and idea.state == "reviewing"
    assert idea.questions_asked == 0 and len(chat) == 1
    assert "No model interpretation was available" in chat[0].body_md
    assert "Who is it for?" not in chat[0].body_md and chat[0].body_md.startswith("<!--employee:")


def test_confident_startup_model_stops_only_with_known_core_fields(client, monkeypatch):
    from app.idea.runner import _enough_answers

    venture_id, _ = _model_first_question(client, monkeypatch, confidence=0.9)
    idea, turns, _chat = _ideas(venture_id)
    assert turns[-1].kind == "proposal" and idea.questions_asked == 0
    assert not _enough_answers({"project_intent": "startup", "questions_asked": 1})
    assert not _enough_answers({"project_intent": "startup", "questions_asked": 2})
    assert _enough_answers({"project_intent": "startup", "questions_asked": 0,
        "answered_fields": {"offer": "A tool to organize shipment bills", "customer": "Freight coordinators"}})
    assert _enough_answers({"project_intent": "personal", "questions_asked": 0})


def test_question_budget_is_a_ceiling(client, monkeypatch):
    import json

    from app.db.supymem_models import IdeaSession
    from app.idea.service import post_message
    from app.state import db
    from tests.security.test_durable_journey_boundary import run_queued

    venture_id, calls = _model_first_question(client, monkeypatch, confidence=0.2)
    idea, _turns, _chat = _ideas(venture_id)
    with db()() as session:
        session.get(IdeaSession, idea.id).question_budget = 1
        session.commit()
    post_message(idea.id, "Shops taking special orders by phone")
    run_queued("idea.clarify_turn")
    assert json.loads(calls[-1][0][-1]["content"])["force_propose"] is True
    _idea, turns, chat = _ideas(venture_id)
    assert turns[-1].kind == "proposal"  # model asked again; the ceiling overrides
    assert "Your idea is ready. Review it, then validate it when you're happy." in chat[-1].body_md
    assert "I have not started a product build or outreach" not in chat[-1].body_md


def test_old_saved_turn_without_confidence_still_parses():
    import json

    from app.idea.agent import Clarification, response_schema
    from tests.idea_fixtures import completion

    data = json.loads(completion().choices[0].message.content)
    assert "confidence" not in data
    assert Clarification.model_validate_json(json.dumps(data)).confidence == 0.0
    assert "confidence" in response_schema()["required"]


def test_descriptive_start_with_answer_preserves_constraints_without_generic_work(client):
    from app.db.models import PlannedTask, CreditReservation
    from app.db.supymem_models import ClarifierTurn
    from app.chat.idea_bridge import pending_question
    from app.state import db

    venture_id = _chat_started_venture(client)
    answer = (
        "Start with first-time solo technical SaaS founders preparing a public launch within seven days, "
        "with a public landing page and an already working signup path. This is an ICP hypothesis, not validated pain. "
        "Recruit only owners who explicitly agree to a public-flow pilot. The first test should check whether three "
        "owners find a reproducible issue and use the report to fix it; no signup submissions, private credentials "
        "or conversion guarantees.")
    assert pending_question(venture_id, answer) is not None
    first = _turn_jobs(venture_id)
    with db()() as session:
        before = session.scalar(select(func.count()).select_from(CreditReservation))
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": answer})
    assert response.status_code == 200
    assert response.json()["kind"] == "idea_clarification_pending"
    assert _turn_jobs(venture_id) == first + 1
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(PlannedTask).where(PlannedTask.venture_id == venture_id)) == 0
        assert session.scalar(select(func.count()).select_from(CreditReservation)) == before
        assert any(answer in (turn.text or "") for turn in session.scalars(select(ClarifierTurn)).all())
