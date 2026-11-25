import asyncio
import json
from copy import deepcopy

import pytest
from sqlalchemy import delete, select

from tests.company_os_harness import append_idea_revision, load_company_os_app, local_chat as local_chat
from tests.unit.chat.test_next_moves import DOMAINS, _saved_state, env, env_fixture
from tests.unit.chat.test_founder_intake_progress import DOMAINS as INTAKE_DOMAINS, bridge_client, client as intake_client
from tests.integration.test_entry_chat_api import client as entry_client, _preview
from tests.unit.chat.test_customers_in_chat import customer_chat, journey_db  # noqa: F401

CLARIFICATION_REPLY = (
    "The next step on deck is a quick clarification to sharpen the idea before we move forward. "
    "Wanna jump into that, or tell me the specific open questions you think we should nail first?"
)


@pytest.mark.usefixtures("customer_chat")
def test_returning_continue_opens_the_offered_question_during_recon(request, monkeypatch):
    from app.chat import llm_router
    from app.chat.conversations import bind_conversation
    from app.chat.store import append_message
    from app.db.supymem_models import IdeaSession
    from app.idea.service import get_session
    from app.state import db

    from tests.unit.chat.test_founder_intake_progress import _send
    from tests.unit.chat.test_onboarding_intents import _pending_question

    client, data = request.getfixturevalue("customer_chat")
    _pending_question(data)
    venture_id, session_id, question_id = data["vid"], "customer_idea", "intent_question"
    with db()() as session:
        session.get(IdeaSession, session_id).state = "recon"
        session.commit()
    conversation = client.post(f"/ventures/{venture_id}/chat/conversations",
        json={"title": "Your next visit", "idempotency_key": "return-continue"})
    assert conversation.status_code == 200, conversation.text
    with bind_conversation(venture_id, conversation.json()["id"]):
        offered = append_message(venture_id, role="assistant", body_md="You can continue with your next question.",
            actions=[{"label": "Continue clarifying", "action": "idea.review", "style": "primary"}])
        assert [action["action"] for action in offered["actions"]] == ["idea.review"]
    before = get_session(session_id)
    calls = []

    async def classify(*args, **kwargs):
        calls.append(args)
        return llm_router.Decision(acts=[], clarify="What do you want to continue? Choose your next step.")

    monkeypatch.setattr(llm_router, "route", classify)
    reply = _send(client, venture_id, "Continue", conversation_id=conversation.json()["id"])
    assert not calls, reply
    assert reply["kind"] == "idea_clarification_ready", reply
    assert any(action.get("payload", {}).get("question_id") == question_id for action in reply["actions"])
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert "credit" not in reply["body_md"].lower()
    assert get_session(session_id)["turns"] == before["turns"]
    clicked = client.post(f"/ventures/{venture_id}/chat/confirm",
        json={"action": "idea.review", "conversation_id": conversation.json()["id"]})
    assert clicked.status_code == 200, clicked.text
    assert clicked.json()["kind"] == reply["kind"]
    assert clicked.json()["actions"] == reply["actions"]


@pytest.mark.parametrize("idea", ["Track factory safety inspections", "Arrange neighborhood swimming lessons"])
@pytest.mark.parametrize("state,action", [("paused", "resume"), ("no_idea", "status"),
                                         ("ready", "blocker.explain")])
def test_continue_does_not_execute_unavailable_or_diagnostic_steps(request, monkeypatch, idea, state, action):
    client = request.getfixturevalue("intake_client")

    from app.chat import router
    from app.chat.store import list_messages
    from app.ventures.lifecycle import pause

    from tests.unit.chat.test_founder_intake_progress import _pending_questions

    if state == "no_idea":
        created = client.post("/ventures", json={"raw_idea": idea, "start": False})
        assert created.status_code == 201, created.text
        venture_id = created.json()["id"]
    else:
        venture_id, _, _ = _pending_questions(client, idea, idea, idea)
        if state == "paused":
            pause(venture_id)
    before = list_messages(venture_id)
    monkeypatch.setattr("app.chat.store.list_messages", lambda *args, **kwargs: [
        {"role": "assistant", "actions": [{"action": action}]}])

    async def forbidden(*args, **kwargs):
        pytest.fail("Continue must not execute an unavailable or diagnostic step")

    monkeypatch.setattr(router, "execute_action", forbidden)
    assert asyncio.run(router._continue_offered_step(venture_id, "continue", None)) is None
    assert list_messages(venture_id) == before


@pytest.mark.parametrize("idea,audience", [
    ("Schedule equipment calibration", "Laboratory supervisors"),
    ("Record amateur astronomy sessions", "Backyard stargazers"),
], ids=["b2b", "consumer"])
def test_continue_follows_the_only_offered_clarification(request, monkeypatch, idea, audience):
    client = request.getfixturevalue("intake_client")

    from app.chat import llm_router
    from app.chat.llm_router import Decision
    from app.chat.store import append_message
    from app.idea.service import get_session

    from tests.unit.chat.test_founder_intake_progress import _pending_questions, _send

    venture_id, _session_id, question_id = _pending_questions(client, idea, audience, idea)
    append_message(venture_id, role="assistant", body_md="Your next step is to clarify your idea. You can continue here.",
                   actions=[{"label": "Continue clarifying", "action": "idea.review", "style": "primary"}])
    calls = []

    async def classify(*args, **kwargs):
        calls.append(args)
        return Decision(acts=[], clarify="Choose the work you want next.")

    monkeypatch.setattr(llm_router, "route", classify)
    reply = _send(client, venture_id, "Continue")
    assert not calls, reply
    assert reply["kind"] == "idea_clarification_ready", reply
    assert any(action.get("payload", {}).get("question_id") == question_id for action in reply["actions"])
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert "credit" not in reply["body_md"].lower()
    before = get_session(_session_id)
    append_message(venture_id, role="assistant", body_md="Do you want to review your progress or people who signed up? Choose a step.",
                   actions=[{"label": "Review progress", "action": "status", "style": "primary"},
                            {"label": "Review signups", "action": "leads", "style": "secondary"}],
                   citations=[{"question": True}])
    reply = _send(client, venture_id, "Continue")
    assert not calls, reply
    assert "Choose one" in reply["body_md"], reply
    assert get_session(_session_id)["turns"] == before["turns"]


@pytest.mark.parametrize("audience", ["Industrial procurement teams", "Family cycling groups"])
def test_conn_regression_intake_keeps_requested_permission_and_reads(audience):
    load_company_os_app()
    from app.chat.next_moves import intake_actions

    permission = {"label": "Check permission", "action": "gtm.eligibility", "payload": {"contact_id": audience}}
    progress = {"label": "Show customers and drafts", "action": "gtm.status"}
    unsolicited = {"label": "Find customers", "action": "gtm.find"}
    card = {"intake_pending": True, "first_question_pending": True, "actions": []}
    assert intake_actions(card, [permission, progress, unsolicited]) == [permission, progress]


@pytest.mark.parametrize("audience,mode", [
    ("commercial bakery purchasing managers", "b2b"),
    ("amateur stargazers planning weekend outings", "consumer"),
])
def test_pending_questions_keep_actionable_customer_search_recovery(audience, mode):
    load_company_os_app()
    from app.chat import customers, next_moves

    view = {"icp": {"who": audience, "mode": mode}, "state": "needs_input",
            "search_request": f"Locate seven {audience} for a feedback conversation.",
            "error_code": "public_search_blocked", "source_access": "unavailable"}
    empty = customers.empty_search_reply(view)
    failed = customers._search_failure(view)
    recovery = [*empty["actions"], *failed["actions"]]
    original = deepcopy(recovery)
    for first_question_pending in (False, True):
        card = {"intake_pending": True, "first_question_pending": first_question_pending, "actions": []}
        routine = [{"action": "gtm.find", "payload": {"prepare": True, "note": audience}},
                   {"action": "outreach.draft"}, {"action": "gtm.send"}]
        assert next_moves.intake_actions(card, [*recovery, *routine]) == recovery
        for action in {item["action"] for item in recovery}:
            blocked = {**card, "blocked_actions": [action]}
            assert next_moves.intake_actions(blocked, recovery) == [
                item for item in recovery if item["action"] != action]
        incomplete = [{"action": "openworker_search", "payload": {"search_context": audience}},
                      {"action": "contacts.preview", "payload": {}},
                      {"action": "gtm.find", "payload": {"retry": True}},
                      {"action": "gtm.find", "payload": {"retry": "true", "note": audience}}]
        assert next_moves.intake_actions(card, incomplete) == []
    assert recovery == original


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("draft_count,questions_pending,gmail_connected,typed_search", [
    (1, True, True, False),
    (1, False, False, True),
    (2, True, False, True),
    (2, False, True, False),
])
def test_waiting_draft_has_one_short_next_step_even_after_a_typed_search(
        request, monkeypatch, idea, draft_count, questions_pending, gmail_connected, typed_search):
    client, venture_id, _ = request.getfixturevalue("env")

    from app.chat import customers, next_moves
    from app.org.characters import parse_speaker

    from tests.unit.test_autonomous_experiments import confirm_saved_idea

    confirm_saved_idea(venture_id)
    state = _saved_state(idea, later=True)
    state.update(questions_pending=questions_pending, gmail_connected=gmail_connected,
                 drafts=[(f"draft_{index}", "draft") for index in range(draft_count)],
                 founder={"what_we_need": "Review your answers before you continue."})
    state["idea"]["confirmed"] = True
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setattr(customers, "saved_search_request", lambda _: f"Find customers for {idea}" if typed_search else "")

    card = next_moves.compose(venture_id)

    primary = card["actions"][0]
    assert primary["action"] == "gtm.status"
    assert primary["payload"] == {"show_saved": True}
    assert primary["label"] == ("Review your draft" if draft_count == 1 else "Review your drafts")
    assert primary["style"] == "primary"
    assert card["body_md"] == (
        f"Your draft{' is' if draft_count == 1 else 's are'} ready.\n\n"
        f"Choose {primary['label']} to take your next step.")
    assert len(card["body_md"].split()) <= 25
    assert not any(action.get("confirmation") for action in card["actions"])
    assert "credit" not in card["body_md"].lower() and "\u2014" not in card["body_md"]

    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "What should I do next?"})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert parse_speaker(reply["body_md"])[1] == card["body_md"]
    actions = [*reply["actions"], *reply.get("next_moves", {}).get("actions", [])]
    assert len(actions) == 1 and actions[0] == primary


@pytest.mark.parametrize("idea", DOMAINS)
def test_next_moves_recommendation_matches_the_founders_primary_button(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.core.local_mode import is_local
    from app.db.models import ChatMessage
    from app.state import db

    _, venture_id, _ = env
    state = _saved_state(idea)
    state.update(questions_pending=True, page=True, research={"present": True}, library=[])
    state["idea"]["confirmed"] = True
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    card = next_moves.compose(venture_id)
    assert card["actions"][0]["action"] == "startup.review"
    find = next(move for move in card["actions"] if move["action"] == "gtm.find")
    row = ChatMessage(id="next-moves-regression", venture_id=venture_id, role="assistant", kind=next_moves.KIND,
        body_md=card["body_md"], actions_json=json.dumps([{**find, "style": "primary"}]), citations_json="[]")
    with db()() as session:
        attached = next_moves.attach(venture_id, row, session=session, request="What next?")
    assert attached is not is_local()
    primary = json.loads(row.actions_json)[0]
    assert primary["payload"] == find["payload"]
    if is_local():
        assert json.loads(row.actions_json) == [{**find, "style": "primary"}]
        assert row.body_md == card["body_md"]
        return
    recommendation = next(line for line in row.body_md.splitlines() if "I recommend:" in line)
    assert primary["label"] in recommendation
    assert "Review my answers" not in row.body_md
    assert "you" in recommendation.lower()
    assert "\u2014" not in row.body_md


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("request_text", ["Find customers for {idea}", "Office hours for {idea}"])
def test_requested_or_active_work_keeps_its_actions_without_unrelated_recommendations(env, monkeypatch, idea, active, request_text):
    from app.chat import next_moves
    from app.chat.store import append_message
    from app.db.models import Job, Venture
    from app.state import db
    from core.clock import utcnow

    _, venture_id, _ = env
    if active:
        with db()() as session:
            workspace_id = session.get(Venture, venture_id).workspace_id
            session.add(Job(id="saved-search", kind="gtm.journey.prepare", venture_id=venture_id,
                            workspace_id=workspace_id, status="pending", payload_json="{}", created_at=utcnow()))
            session.commit()
    card = {"kind": next_moves.KIND, "stage": "Outreach", "body_md": "I recommend: Build your website.",
            "actions": [{"action": "pagebuilder.build", "label": "Build your website", "style": "primary"}]}
    monkeypatch.setattr(next_moves, "compose", lambda *args, **kwargs: deepcopy(card))
    action = {"action": "gtm.status", "label": "Show your saved customers", "style": "primary"}
    append_message(venture_id, role="assistant", body_md="Your idea is saved.",
                   citations=[{"next_moves": {"stage": "Idea"}}])
    append_message(venture_id, role="user", body_md="Thanks" if active else request_text.format(idea=idea))
    reply = append_message(venture_id, role="assistant", body_md="You can review the people you saved.", actions=[action])
    assert reply["actions"] == [action]
    assert "next_moves" not in reply


@pytest.mark.parametrize("idea,audience,summary", INTAKE_DOMAINS)
def test_continue_clarifying_only_offers_unshown_questions_and_stale_click_reviews(intake_client, idea, audience, summary):
    from app.chat import next_moves
    from app.idea.service import get_session
    from tests.unit.chat.test_founder_intake_progress import _pending_questions, _send

    venture_id, session_id, question_id = _pending_questions(intake_client, idea, audience, summary)
    shown = _send(intake_client, venture_id, "Continue clarifying")
    assert any(action.get("payload", {}).get("question_id") == question_id for action in shown["actions"]), shown
    assert not any(action.get("action") == "idea.review" for action in shown["actions"])
    greeting = _send(intake_client, venture_id, "hey")
    assert not any(action.get("action") == "idea.review" for action in greeting["actions"])
    assert not any(action.get("action") == "idea.review" for action in next_moves.compose(venture_id)["actions"])
    resumed = intake_client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"}).json()
    # A stale click shows the open question again; it never skips the founder's remaining questions.
    assert resumed["kind"] == "idea_clarification_ready"
    assert any(action.get("payload", {}).get("question_id") == question_id for action in resumed["actions"])
    assert get_session(session_id)["state"] != "reviewing"
    assert not any(action.get("action") == "idea.review" for action in next_moves.compose(venture_id)["actions"])


@pytest.fixture(scope="module")
def reply_moves():
    load_company_os_app()
    from app.chat import next_moves

    return next_moves


@pytest.fixture()
def reply_matcher(reply_moves, monkeypatch):
    monkeypatch.setattr(reply_moves, "experiments_autopilot_enabled", lambda: True)
    return reply_moves.align_reply


def _card(idea):
    return {"stage": "Idea", "state_hash": "saved-state", "body_md": idea, "actions": [
        {"label": "Pressure-test your idea", "action": "ceo_review", "payload": {}, "style": "primary",
         "state_hash": "saved-state"},
        {"label": "Continue clarifying", "action": "idea.review", "payload": {}, "style": "secondary",
         "state_hash": "saved-state"},
        {"label": "Find customers", "action": "gtm.find", "payload": {"prepare": True, "note": idea},
         "style": "secondary", "state_hash": "saved-state"}]}


@pytest.mark.parametrize("idea", DOMAINS)
def test_intake_keeps_safe_navigation_without_exposing_work_actions(reply_moves, idea):
    card = {**_card(idea), "intake_pending": True, "first_question_pending": True}
    context = {"label": "Review your answers: " + idea, "href": "/context"}
    settings = {"label": "Review your settings", "href": "/v/current/settings"}
    blocked = [{"label": "Start work", "action": "research.start", "href": "/context"},
        {"label": "Start work", "name": "research.start", "href": "/context"},
        {"label": "Start work", "href": "/v/current/site?build=1"},
        *({"label": "Unsafe link", "href": href} for href in ("//other.example", "/\\other.example", "/\n/other.example", "javascript:alert(1)"))]
    assert reply_moves.intake_actions(card, [context, settings, *blocked]) == [context, settings]


@pytest.mark.parametrize("idea", DOMAINS)
def test_intake_keeps_nonexecuting_recovery_prefills(reply_moves, idea):
    card = {**_card(idea), "intake_pending": True, "first_question_pending": True}
    prefill = {"label": "Start a new round", "action": "chat.prefill", "text": f"Review my saved result for {idea}."}
    payload_prefill = {"label": "Record your result", "action": "chat.prefill", "payload": {"text": idea}}
    malformed = [{"action": "chat.prefill", "text": ""}, {"action": "chat.prefill", "text": "   "},
                 {"action": "chat.prefill", "text": 5}, {"action": "chat.prefill", "payload": {"text": ""}, "text": idea}]
    work = {"action": "research.start", "text": idea}
    assert reply_moves.intake_actions(card, [prefill, payload_prefill, *malformed, work]) == [prefill, payload_prefill]
    assert reply_moves.intake_actions({**card, "blocked_actions": ["chat.prefill"]}, [prefill]) == []


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("invitation,action", [
    ("Shall we Continue clarifying?", "idea.review"),
    ("Would you like to start research?", "research.prepare"),
    ("Let's design an experiment.", "experiment.start"),
    ("I recommend finding customers. Shall we Find customers?", "gtm.find"),
    ("Would you like to pressure test your idea?", "ceo_review"),
    ("The next step is to research demand. Wanna jump into that?", "research.prepare"),
])
def test_reply_invitation_selects_the_proposed_registered_move(reply_matcher, idea, invitation, action):
    from app.chat.actions import get_action

    card = _card(idea)
    original = deepcopy(card)
    assert "research.prepare" not in [move["action"] for move in card["actions"]]
    assert reply_matcher(card, f"Your saved idea is {idea}. {invitation}", []) is True
    assert card["actions"][0]["action"] == action
    assert [move["style"] for move in card["actions"]] == ["primary"] + ["secondary"] * (len(card["actions"]) - 1)
    assert all(move["state_hash"] == original["state_hash"] and get_action(move["action"]) for move in card["actions"])
    customers = next(move for move in card["actions"] if move["action"] == "gtm.find")
    assert customers["payload"] == {"prepare": True, "note": idea}


@pytest.mark.parametrize("idea", DOMAINS)
def test_exact_existing_experiment_chip_beats_generic_experiment_fallback(reply_matcher, idea):
    card = _card(idea)
    chip = {"label": "Design an experiment", "action": "experiment.recommendation.prepare",
            "payload": {"experiment_id": "saved-experiment"}, "style": "primary"}
    original = deepcopy(chip)
    assert reply_matcher(card, f"Your idea is {idea}. Shall we Design an experiment?", [chip]) is True
    assert card["actions"][0] == {**chip, "state_hash": card["state_hash"], "reply_choice": True}
    assert "experiment.start" not in [move["action"] for move in card["actions"]]
    assert chip == original


@pytest.mark.parametrize("idea", DOMAINS)
def test_single_sentence_stamped_reply_still_promotes_its_next_move(reply_matcher, idea):
    from app.org.characters import stamp_speaker

    card = _card(idea)
    body = stamp_speaker("Shall we Continue clarifying?", {"id": "manager", "name": "Charlie", "title": "Manager"})
    assert reply_matcher(card, body, []) is True
    assert card["actions"][0]["action"] == "idea.review"


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("label,action", [
    ("Interview five people about their daily routine", "chat.reply"),
    ("Start a market and competitor research run", "research.prepare"),
])
def test_explicit_model_next_chip_is_authoritative_without_only_keyword_matching(reply_matcher, idea, label, action):
    card = _card(idea)
    chip = {"label": label, "action": "chat.reply", "payload": {"text": label}, "style": "secondary"}
    assert reply_matcher(card, f"Would you like to {label}?", [chip]) is True
    assert card["actions"][0]["action"] == action
    if action == "chat.reply":
        assert card["actions"][0]["payload"] == chip["payload"]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("second_to", ["", "to "])
def test_either_or_choices_follow_prose_order_before_unrelated_options(reply_matcher, idea, reverse, second_to):
    card = _card(idea)
    choices = [
        {"label": "Start research", "action": "research.prepare", "payload": {}},
        {"label": "Design an experiment", "action": "experiment.start", "payload": {}},
    ]
    if reverse:
        choices.reverse()
    body = f"Your idea is {idea}. Would you prefer to {choices[0]['label']} or {second_to}{choices[1]['label']}?"
    assert reply_matcher(card, body, list(reversed(choices))) is True
    assert [move["action"] for move in card["actions"][:2]] == [move["action"] for move in choices]
    assert [move["action"] for move in card["actions"][2:]] == ["ceo_review", "idea.review", "gtm.find"]
    assert [move["style"] for move in card["actions"]] == ["primary"] + ["secondary"] * 4
    assert [move.get("reply_choice", False) for move in card["actions"]] == [True, True, False, False, False]


@pytest.mark.parametrize("idea", DOMAINS)
def test_either_or_saved_research_remains_an_explicit_second_choice(reply_matcher, idea):
    card = _card(idea)
    research = {"label": "Review saved research", "action": "research", "payload": {}}
    actions = [research]
    assert reply_matcher(card, "Would you prefer Continue clarifying or Review saved research?", actions) is True
    assert [move["action"] for move in card["actions"][:2]] == ["idea.review", "research"]
    assert actions == card["actions"][:2]
    assert [move["style"] for move in actions] == ["primary", "secondary"]
    assert all(move["reply_choice"] for move in actions)


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("body", [
    "Your idea is saved.",
    "Research is complete and the findings are saved.",
    "I already finished the research. Would you like to review what I found?",
    "I don't recommend research.",
    "I recommend not starting research.",
    "I can report that research is complete.",
    "I can report that Start research is complete.",
    "The customer said, 'I recommend research'.",
    "You asked whether I recommend research.",
    "I suggest research next. Your saved idea is unchanged.",
    "Would you like to chat about your plans?",
])
def test_no_proposal_negated_completed_or_reported_research_does_not_change_card(reply_matcher, idea, body):
    card = _card(idea)
    original = deepcopy(card)
    assert reply_matcher(card, body, []) is False
    assert card == original


@pytest.mark.parametrize("idea", DOMAINS)
def test_unknown_action_cannot_be_promoted_and_paused_card_is_unchanged(reply_matcher, idea):
    card = _card(idea)
    original = deepcopy(card)
    unknown = {"label": "Delete everything", "action": "not.registered", "payload": {"confirmed": True}}
    assert reply_matcher(card, "Would you like to Delete everything?", [unknown]) is False
    assert card == original
    approval = [{"label": "Approve research", "action": "research.start", "style": "primary",
                 "confirmation": {"action": "research.start", "resulting_state": {}}}]
    original_approval = deepcopy(approval)
    assert reply_matcher(card, "Shall we Start research?", approval) is False
    assert card == original and approval == original_approval
    paused = {**card, "stage": "Paused", "actions": [
        {"label": "Resume", "action": "resume", "style": "primary", "state_hash": card["state_hash"]}]}
    original = deepcopy(paused)
    assert reply_matcher(paused, "Shall we Start research?", []) is False
    assert paused == original


@pytest.mark.parametrize("idea", DOMAINS)
def test_alignment_does_not_enable_experiments_or_restore_skipped_questions(reply_matcher, reply_moves, monkeypatch, idea):
    monkeypatch.setattr(reply_moves, "experiments_autopilot_enabled", lambda: False)
    card = _card(idea)
    original = deepcopy(card)
    assert reply_matcher(card, "Shall we Design an experiment?", []) is False
    assert card == original
    card["actions"] = [move for move in card["actions"] if move["action"] != "idea.review"]
    original = deepcopy(card)
    assert reply_matcher(card, "Shall we Continue clarifying?", []) is False
    assert card == original


@pytest.mark.parametrize("idea", DOMAINS)
def test_existing_either_or_actions_stay_complete_without_copying_the_whole_card(reply_matcher, idea):
    card = _card(idea)
    actions = [{"label": "See your next action", "action": "whats_next", "style": "primary"},
               {"label": "Continue clarifying", "action": "idea.review", "style": "secondary"}]
    assert reply_matcher(card, "Choose your next action, or continue clarifying later.", actions) is True
    assert [move["action"] for move in actions] == ["whats_next", "idea.review"]
    assert actions == card["actions"][:2]
    assert [move["style"] for move in actions] == ["primary", "secondary"]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("confirmed,pending,skipped", [
    (False, True, []),
    (True, True, []),
    (False, False, []),
    (False, True, ["clarification"]),
])
def test_waiting_draft_is_primary_without_changing_saved_work(
        env, monkeypatch, idea, confirmed, pending, skipped):
    from app.chat import next_moves

    _, vid, _ = env
    state = _saved_state(idea, later=True)
    state["idea"]["confirmed"] = confirmed
    state.update(questions_pending=pending, skipped=skipped)
    original = deepcopy(state)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    card = next_moves.compose(vid)
    assert card["actions"][0]["action"] == "gtm.status"
    assert card["actions"][0]["payload"] == {"show_saved": True}
    assert card["actions"][0]["label"] == "Review your draft"
    assert not any(move["action"] == "idea.review" for move in card["actions"])
    assert all(move["state_hash"] == card["state_hash"] for move in card["actions"])
    assert state == original


@pytest.mark.parametrize("idea", DOMAINS)
def test_questionnaire_reminder_preserves_aligned_primary_and_payload(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.router import _continue_clarifying
    from app.chat.store import append_message

    _, vid, _ = env
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {**_saved_state(idea), "idea_state": "clarifying"})
    saved = append_message(vid, role="assistant", body_md="Shall we Continue clarifying?", actions=[
        {"label": "Continue clarifying", "action": "idea.review", "payload": {"note": idea}, "style": "primary"}])
    original = deepcopy(saved["actions"])
    updated = _continue_clarifying(vid, saved)
    assert updated["actions"] == original
    assert updated["actions"][0]["payload"] == {"note": idea}
    assert updated["actions"][0]["style"] == "primary"
    assert sum(move["label"] == "Continue clarifying" for move in updated["actions"]) == 1


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("handler", ["general", "assistant"])
def test_model_questionnaire_followup_uses_saved_question_and_accepts_typed_answer(
        intake_client, monkeypatch, idea, handler):
    from app.chat import assistant, next_moves
    from app.chat.context import ChatContext
    from app.chat.handlers import general
    from app.chat.router import _continue_clarifying
    from app.chat.store import append_message, list_messages
    from app.db.supymem_models import IdeaRevision
    from app.idea.service import get_session
    from app.state import db
    from tests.unit.chat.test_founder_intake_progress import _pending_questions, _send

    venture_id, session_id, question_id = _pending_questions(intake_client, idea, idea, idea, field="offer")
    with db()() as session:
        revision = session.get(IdeaRevision, get_session(session_id)["current_revision"]["id"])
        append_idea_revision(session, revision.id,
            structured=json.dumps({**json.loads(revision.structured), "problem": idea}))
        session.commit()
    state = {**_saved_state(idea), "idea_state": "clarifying"}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    question = get_session(session_id)["turns"][-1]["text"]
    label = next_moves.compose(venture_id)["actions"][0]["label"]
    async def complete(messages, **kwargs):
        return generated

    monkeypatch.setattr(assistant, "_complete", complete)

    def answer(request):
        append_message(venture_id, role="user", body_md=request)
        if handler == "general":
            return asyncio.run(general.handle(ChatContext(venture={"id": venture_id}), request))
        return asyncio.run(assistant.answer_question(venture_id, request))

    for request, body in (("hey", f"Your idea is {idea}. Shall we {label}?"),
                          ("hey", 'A customer asked, "Tell me about it. What would help you make it useful?" Tell me your reaction.'),
                          ("Write three interview questions", "What would help you make it useful?")):
        generated = body
        reply = answer(request)
        assert reply["kind"] == "text" and body in reply["body_md"]
    for footer in ("", f"Tap {label} to save that and move forward.", f"Shall we {label}?"):
        generated = (f"Your idea is {idea}.\n\nOne question for you\n\n- **What would help you make it useful?**\n\n{footer}"
            if footer else f"Your idea is {idea}. What would help you make it useful?")
        reply = answer("hey")
        saved = _continue_clarifying(venture_id, append_message(venture_id, role="assistant", **reply))
        assert saved["kind"] == "idea_clarification_ready"
        assert question in saved["body_md"] and label not in saved["body_md"]
        assert not saved.get("next_moves")
        assert not any(move.get("action") == "idea.review" for move in saved["actions"])
        assert any(move.get("payload", {}).get("question_id") == question_id for move in saved["actions"])
        persisted = next(message for message in list_messages(venture_id) if message["id"] == saved["id"])
        assert persisted["body_md"] == saved["body_md"] and persisted["actions"] == saved["actions"]
    typed = f"A tool that helps with the manual steps in {idea.lower()}."
    reply = _send(intake_client, venture_id, typed)
    assert reply["kind"] == "idea_clarification_pending"
    session = get_session(session_id)
    assert session["current_revision"]["structured"]["offer"] == typed
    assert session["turns"][-1]["role"] == "founder" and session["turns"][-1]["text"] == typed


@pytest.mark.parametrize("idea", DOMAINS)
def test_hey_model_reply_persists_clarification_and_open_questions_before_next_research_chip(env, monkeypatch, idea):
    from app.chat import assistant, next_moves
    from app.chat.store import append_message
    from app.core.local_mode import is_local
    from app.db.models import ChatMessage, Job
    from app.state import db

    client, _, _ = env
    name = "ProtocolNest" if idea == DOMAINS[0] else "WalkTogether"
    made = client.post("/ventures", json={"name": name, "raw_idea": idea, "start": False})
    assert made.status_code == 201, made.text
    vid = made.json()["id"]
    with db()() as session:
        session.execute(delete(Job).where(Job.venture_id == vid))
        session.commit()
    state = {**_saved_state(idea), "questions_pending": True}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    research_label = "Start a market and competitor research run"

    async def complete(messages, **kwargs):
        prompt = "\n".join(message["content"] for message in messages)
        assert "Attached next steps" in prompt
        assert '"idea.review"' in prompt
        assert idea.casefold() in messages[1]["content"].casefold()
        return CLARIFICATION_REPLY + "\nNEXT: " + research_label

    monkeypatch.setattr(assistant, "_complete", complete)
    append_message(vid, role="user", body_md="hey")
    reply = asyncio.run(assistant.answer_question(vid, "hey"))
    assert reply["actions"] == [{"label": research_label, "action": "chat.reply",
                                 "payload": {"text": research_label}, "style": "secondary"}]
    reply["actions"][0]["style"] = "primary"
    saved = append_message(vid, role="assistant", kind=reply["kind"], body_md=reply["body_md"], actions=reply["actions"])
    aligned = saved["actions"][:2] if is_local() else saved["next_moves"]["actions"]
    assert [move["action"] for move in aligned[:2]] == ["idea.review", "chat.reply"]
    assert aligned[0]["label"] == "Continue clarifying" and aligned[0]["payload"] == {}
    assert aligned[1]["label"] == "Share my open questions"
    assert "open questions" in aligned[1]["payload"]["text"].lower()
    assert saved["actions"] == [*aligned[:2], {**reply["actions"][0], "style": "secondary"}]
    assert saved["actions"][-1] == {**reply["actions"][0], "style": "secondary"}
    assert sum(move.get("style") == "primary" for move in saved["actions"]) == 1
    with db()() as session:
        row = session.get(ChatMessage, saved["id"])
        assert json.loads(row.actions_json) == saved["actions"]
        persisted = next((item["next_moves"] for item in json.loads(row.citations_json or "[]") if "next_moves" in item), None)
        if is_local():
            assert persisted is None and "next_moves" not in saved
        else:
            assert persisted["actions"] == aligned
        assert not session.scalars(select(Job.id).where(Job.venture_id == vid)).all()


@pytest.mark.parametrize("idea", DOMAINS)
def test_general_greeting_model_and_buttons_share_the_planned_primary(env, monkeypatch, idea):
    from app.chat import assistant, next_moves, rephrase
    from app.chat.context import ChatContext
    from app.chat.handlers import general
    from app.chat.store import append_message
    from app.core.local_mode import is_local

    client, _, _ = env
    name = "ProtocolNest" if idea == DOMAINS[0] else "WalkTogether"
    made = client.post("/ventures", json={"name": name, "raw_idea": idea, "start": False})
    assert made.status_code == 201, made.text
    vid = made.json()["id"]
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {**_saved_state(idea), "questions_pending": True})
    monkeypatch.setattr(general, "_next_step_line", lambda *_: "Start unrelated research")

    async def prepare(venture_id, question, **kwargs):
        return question, question

    async def complete(messages, **kwargs):
        prompt = "\n".join(message["content"] for message in messages)
        assert "Attached next steps" in prompt
        assert '"action": "idea.review"' in prompt
        assert "Start unrelated research" not in messages[1]["content"]
        assert idea.casefold() in messages[1]["content"].casefold()
        return CLARIFICATION_REPLY

    monkeypatch.setattr(rephrase, "prepare", prepare)
    monkeypatch.setattr(assistant, "_complete", complete)
    reply = asyncio.run(general.handle(ChatContext(venture={"id": vid, "name": name}), "hey"))
    assert reply["actions"][0]["action"] == "idea.review"
    saved = append_message(vid, role="assistant", **reply)
    assert saved["actions"][0]["action"] == "idea.review"
    if is_local():
        assert "next_moves" not in saved
    else:
        assert saved["next_moves"]["actions"][0]["action"] == "idea.review"
    assert saved["actions"][1]["action"] == "chat.reply"
    assert saved["actions"][1]["label"] == "Share my open questions"


@pytest.mark.parametrize("idea", DOMAINS)
def test_research_invitation_overrides_real_composed_card_without_research(env, monkeypatch, idea):
    from app.chat import next_moves

    _, vid, _ = env
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: _saved_state(idea))
    monkeypatch.setattr(next_moves, "_blocked_actions", lambda _: set())  # the saved state above is confirmed; the row is not
    card = next_moves.compose(vid)
    assert "research.prepare" not in [move["action"] for move in card["actions"]]
    assert next_moves.align_reply({**card, "blocked_actions": ["research.prepare"]},
                                  "I recommend research. Shall we get started?", []) is False
    assert next_moves.align_reply(card, "I recommend research. Shall we get started?", []) is True
    assert card["actions"][0]["action"] == "research.prepare"
    assert card["actions"][0]["payload"] == {}
    assert all(move["style"] == "secondary" for move in card["actions"][1:])


@pytest.mark.parametrize("idea", DOMAINS)
def test_question_messages_trusts_exact_primary_label_before_model_call(env, monkeypatch, idea):
    from app.chat import next_moves, rephrase
    from app.chat.assistant import question_messages
    from app.credits import metered

    _, vid, _ = env
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {**_saved_state(idea), "questions_pending": True})
    card = next_moves.compose(vid)
    compose = next_moves.compose
    seen = []

    def observed_compose(venture_id):
        seen.append(venture_id)
        return compose(venture_id)

    async def prepare(venture_id, question, **kwargs):
        return question, question

    async def forbidden_model(*args, **kwargs):
        pytest.fail("Constructing prompt messages must not call the model")

    monkeypatch.setattr(next_moves, "compose", observed_compose)
    monkeypatch.setattr(rephrase, "prepare", prepare)
    monkeypatch.setattr(metered, "llm", forbidden_model)
    messages = asyncio.run(question_messages(vid, "hey", brief={"_rendered": "Saved idea: " + idea}, workspace_id=None))
    assert seen == [vid]
    assert messages[0]["role"] == "system"
    assert len(messages) == 2 and messages[1]["role"] == "user"
    system = messages[0]["content"]
    context = messages[1]["content"]
    assert "Attached next steps" in context
    assert card["actions"][0]["action"] == "idea.review"
    assert card["actions"][0]["label"] == "Continue clarifying"
    assert json.dumps([{"label": move["label"], "action": move["action"]} for move in card["actions"]], ensure_ascii=False) in context
    assert card["state_hash"] not in system + context
    assert card["actions"][0]["label"] in context
    assert all(phrase in system.lower() for phrase in ("last sentence", "exact label", "one question", "either/or", "both choices"))


@pytest.mark.parametrize("name,idea", [
    ("AINest", "I want to build a startup for product teams. It turns customer feedback into a prioritized weekly plan"),
    ("TrailNest", "I want to build a startup for neighborhood families. It helps them plan and join weekend walking groups"),
])
def test_onboarding_saved_reading_acknowledgement_has_no_work_buttons(entry_client, name, idea):
    from app.chat import next_moves
    from app.ventures.next_step import _facts

    preview = _preview(entry_client, idea)
    assert "proposal" in preview, preview
    proposal = preview["proposal"]
    response = entry_client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"], "name": name})
    assert response.status_code == 200, response.text
    assert response.json()["route"] == "startup"
    venture_id = response.json()["venture_id"]
    assert entry_client.get(f"/ventures/{venture_id}").json()["name"] == name
    reply = entry_client.get(f"/ventures/{venture_id}/chat").json()["items"][-1]
    assert reply["kind"] == "entry_pending"
    assert "Your startup idea is saved. I'm reading it now and will ask the one thing I most need to understand." in reply["body_md"]
    assert reply["actions"] == []
    assert not reply.get("next_moves")
    assert not any("next_moves" in item for item in reply["citations"])
    assert _facts(venture_id)["first_question_pending"] is True
    card = next_moves.compose(venture_id)
    assert card["intake_pending"] is True
    assert [action["action"] for action in card["actions"]] == ["idea.review", "chat.reply"]
    assert card["actions"][0]["label"] == "Check review"
    assert card["actions"][1]["payload"] == {"text": "Skip the questions"}


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("kind", ["entry_pending", "idea_clarification_pending", "status", "browse_progress",
                                  "gtm_search_progress", "gtm_diagnosis_pending", "deep_answer_pending"])
def test_status_rows_never_compose_or_regain_buttons(reply_moves, monkeypatch, idea, kind):
    from types import SimpleNamespace

    def forbidden(*args, **kwargs):
        pytest.fail("A status row must not compose next moves")

    monkeypatch.setattr(reply_moves, "compose", forbidden)
    metadata = {"saved_status": {"idea": idea}}
    row = SimpleNamespace(kind=kind, body_md="Your work is waiting for a result.",
                          actions_json=json.dumps(_card(idea)["actions"]), citations_json=json.dumps([metadata]))
    assert reply_moves.attach("venture", row) is False
    assert json.loads(row.actions_json) == []
    assert json.loads(row.citations_json) == [metadata]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("first_pending", [False, True])
def test_active_intake_greeting_never_offers_generic_actions(env, monkeypatch, idea, first_pending):
    from app.chat import assistant, next_moves, rephrase
    from app.chat.context import ChatContext
    from app.chat.handlers import general
    from app.chat.store import append_message
    from app.core.local_mode import is_local

    _, vid, _ = env
    state = {**_saved_state(idea), "idea_state": "clarifying", "first_question_pending": first_pending}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(next_moves, "experiments_autopilot_enabled", lambda: True)
    monkeypatch.setattr(general, "_next_step_actions", lambda _: _card(idea)["actions"])

    async def prepare(venture_id, text, **kwargs):
        return text, text

    async def complete(messages, **kwargs):
        context = messages[1]["content"]
        assert "The idea is still being understood." in context
        assert '"research.prepare"' not in context and '"experiment.start"' not in context
        if first_pending:
            assert "Do not invent another question or propose an action." in context
            return "I'm reading your saved idea and preparing the first question."
        assert '"idea.review"' in context
        return "Shall we Continue clarifying?"

    monkeypatch.setattr(rephrase, "prepare", prepare)
    monkeypatch.setattr(assistant, "_complete", complete)
    ctx = ChatContext(venture={"id": vid, "name": "AINest", "workspace_id": None})
    reply = asyncio.run(general.handle(ctx, "hey"))
    expected = [] if first_pending else ["idea.review"]
    assert [action["action"] for action in reply["actions"]] == expected
    saved = append_message(vid, role="assistant", **reply)
    assert [action["action"] for action in saved["actions"]] == expected
    if first_pending or is_local():
        assert not saved.get("next_moves")
    else:
        assert [action["action"] for action in saved["next_moves"]["actions"]] == expected
        assert saved["actions"][0]["style"] == "primary"


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("idea_state", ["open", "recon", "clarifying", "reviewing"])
def test_intake_card_only_offers_question_and_keeps_either_or_choices(env, monkeypatch, idea, idea_state):
    from app.chat import next_moves

    _, vid, _ = env
    state = {**_saved_state(idea), "idea_state": idea_state, "questions_pending": True}
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: state)
    card = next_moves.compose(vid)
    assert [action["action"] for action in card["actions"]] == ["idea.review"]
    assert next_moves.align_reply(card, "Shall we start research?", []) is False
    assert [action["action"] for action in card["actions"]] == ["idea.review"]
    actions = _card(idea)["actions"]
    assert next_moves.align_reply(card, CLARIFICATION_REPLY, actions) is True
    assert [action["action"] for action in card["actions"]] == ["idea.review", "chat.reply"]
    assert [action["style"] for action in card["actions"]] == ["primary", "secondary"]
    assert actions[1]["payload"] == {"text": "I'd like to share my open questions."}
    assert all(action["action"] in {"idea.review", "chat.reply"} for action in actions)


@pytest.mark.parametrize("idea", DOMAINS)
def test_status_questionnaire_reminder_does_not_add_buttons(env, idea):
    from app.chat.router import _continue_clarifying
    from app.chat.store import append_message

    _, vid, _ = env
    saved = append_message(vid, role="assistant", kind="entry_pending", body_md=f"I'm reading {idea} now.")
    assert saved["actions"] == [] and not saved.get("next_moves")
    assert _continue_clarifying(vid, saved) == saved


@pytest.mark.parametrize("idea", DOMAINS)
def test_intake_preserves_question_answers_approvals_and_recovery(reply_moves, idea):
    card = {**_card(idea), "intake_pending": True}
    question = {"label": idea, "action": "chat.reply", "payload": {"text": idea, "question_id": "next-question"}}
    approval = {"label": "Approve", "confirmation": {"id": "saved-approval"}}
    recovery = {"label": "Try again", "action": "job.retry", "payload": {"job_id": "saved-job"}}
    assert reply_moves.intake_actions(card, [*_card(idea)["actions"], question, approval, recovery]) == [
        question, approval, recovery]
    actions = [approval]
    original = deepcopy(card)
    assert reply_moves.align_reply(card, CLARIFICATION_REPLY, actions) is False
    assert actions == [approval] and card == original


@pytest.mark.parametrize("idea", DOMAINS)
def test_intake_keeps_read_only_failed_work_and_nonexecuting_corrections(reply_moves, idea):
    card = {**_card(idea), "intake_pending": True, "first_question_pending": True}
    review = {"label": "Review saved work", "action": "jobs.status", "payload": {"state": "failed"}}
    correction = {"label": "Prepare a fresh request", "action": "chat.prefill", "payload": {"text": idea}}
    send = {"label": "Send", "action": "gtm.send", "payload": {"text": idea}}
    assert reply_moves.intake_actions(card, [review, correction, send]) == [review, correction]


@pytest.mark.parametrize("idea", DOMAINS)
def test_requested_pending_idea_names_saved_context_without_unprompted_actions(reply_moves, monkeypatch, idea):
    from app.chat import customers, execute
    from app.ventures import stage

    state = {**_saved_state(idea), "reading_idea": True, "first_question_pending": True}
    monkeypatch.setattr(reply_moves, "_snapshot", lambda _: state)
    monkeypatch.setattr(stage, "get_stage", lambda _: {"stage": "idea", "label": "Shaping your idea"})
    monkeypatch.setattr(customers, "saved_search_request", lambda _: None)
    monkeypatch.setattr(execute, "_review_idea", lambda _: {"kind": "idea_clarification_pending",
        "body_md": "Your answers are saved. Your review is queued.", "actions": [], "citations": []})

    unprompted = reply_moves.compose("saved-venture")
    assert unprompted["kind"] == "status" and not unprompted["actions"]
    requested = reply_moves.compose("saved-venture", requested=True)
    assert idea in requested["body_md"] and "Check your idea review" in requested["body_md"]
    assert [action["action"] for action in requested["actions"]] == ["status"]


@pytest.mark.parametrize("idea", DOMAINS)
def test_pending_question_only_keeps_its_answer_control(env, monkeypatch, idea):
    from app.chat import next_moves
    from app.chat.store import append_message

    _, vid, _ = env
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {**_saved_state(idea), "idea_state": "reviewing"})
    question = {"label": "Product teams" if idea == DOMAINS[0] else "Local families", "action": "chat.reply",
                "payload": {"text": idea, "question_id": "saved-question"}, "style": "primary"}
    saved = append_message(vid, role="assistant", kind="idea_clarification_ready",
                           body_md="Who will use it?", actions=[question, *_card(idea)["actions"]])
    assert saved["actions"] == [question]
    assert not saved.get("next_moves")
    assert saved["body_md"].count("?") == 1


@pytest.mark.parametrize("idea", DOMAINS)
def test_intake_drops_legacy_research_reply_chip(reply_moves, idea):
    card = {**_card(idea), "intake_pending": True, "actions": [_card(idea)["actions"][1]]}
    chip = {"label": "Start a market and competitor research run", "action": "chat.reply", "reply_choice": True,
            "payload": {"text": "Start a market and competitor research run"}}
    assert reply_moves.intake_actions(card, [chip]) == []
    actions = [chip]
    assert reply_moves.align_reply(card, "Shall we Start a market and competitor research run?", actions) is False
    assert actions == []
    assert [move["action"] for move in card["actions"]] == ["idea.review"]


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("action,marker", [("chat.reply", "action"), ("chat.model_retry", None),
    ("job.retry", None), ("status", "action"), ("gtm.status", "action"), ("gtm.find", "payload")])
def test_intake_keeps_retry_without_restoring_routine_buttons(reply_moves, action, marker, idea):
    card = {**_card(idea), "intake_pending": True, "first_question_pending": True}
    retry = {"label": "Try again", "action": action, "payload": {"retry": True} if marker == "payload" else {},
             **({"retry": True} if marker == "action" else {})}
    assert reply_moves.intake_actions(card, [*card["actions"], retry]) == ([] if action == "gtm.find" else [retry])


@pytest.mark.parametrize("idea", DOMAINS)
def test_safe_retry_builders_survive_intake_without_replaying_work(reply_moves, monkeypatch, idea):
    from app.builder import execution as managed_builds
    from app.chat import assistant, build_chain, customers, recovery, site_actions
    from app.experiments import autonomous

    from app.core.local_mode import is_local
    if is_local():
        from app.chat import build_path
        monkeypatch.setattr(build_path, "local_build_reply", lambda *args, **kwargs:
            recovery.founder_failure(RuntimeError("status unavailable"), "venture", "your build",
                {"action": "build.prepare", "payload": {}}))

    def unavailable(*args, **kwargs):
        raise RuntimeError("status unavailable")

    monkeypatch.setattr(site_actions, "publish_preflight", unavailable)
    monkeypatch.setattr(customers, "_search_setup_reply", lambda: None)
    monkeypatch.setattr(managed_builds, "estimate", lambda *args, **kwargs: {"ready": False})
    card = {**_card(idea), "intake_pending": True, "first_question_pending": True}
    view = {"icp": {"who": idea}, "state": "failed"}
    plan = {"milestones": [{"id": "milestone", "title": idea, "included": True,
        "tasks": [{"id": "task", "title": idea, "capability_id": "build.run",
                   "implementation": "new_code", "state": "ready"}]}]}
    replies = [
        recovery.founder_failure(RuntimeError("status unavailable"), "venture", "your request",
                                 {"action": "status", "payload": {}}),
        customers._search_failure(view, retry_status=True),
        customers._search_error("venture", RuntimeError("status unavailable"), view=view),
        {"actions": site_actions.after_waitlist("venture")[1]},
        asyncio.run(assistant.narrate_action_result("venture", question=idea, action_result={})),
        build_chain.reply("venture", "", "plan", plan, [{"id": "app"}], idea, "founder"),
        autonomous.failure_reply({"id": "experiment", "error": "status unavailable", "retryable": True}),
    ]
    for reply in replies:
        retry = reply["actions"][0]
        assert retry["label"] == "Retry" and retry["retry"] is True
        assert retry in reply_moves.intake_actions(card, [*_card(idea)["actions"], *reply["actions"]])
        assert retry["action"] in {"status", "gtm.status", "site.preview", "build.prepare", "experiment.prepare"}
    for confirmation in (customers._search_card(view, retry=True), site_actions.rewrite_card(idea)):
        assert "retry" not in confirmation
        assert reply_moves._retry_action(confirmation) is False
        if confirmation.get("confirmation"):
            assert confirmation["action"] == confirmation["confirmation"]["action"]
        assert confirmation["action"] in {"gtm.find", "site.rewrite"}
    paid = autonomous.failure_reply({"id": "experiment", "error": "status unavailable", "runner": "outreach_split",
        "variants": [{"label": idea}], "hypothesis": idea})["actions"][0]
    assert paid["action"] == "outreach.draft" and "retry" not in paid
    assert reply_moves.intake_actions(card, [paid]) == []


@pytest.mark.parametrize("idea", DOMAINS)
@pytest.mark.parametrize("action", ["research.start", "gtm.send"])
@pytest.mark.parametrize("retry_marker", [None, False, 1, "true"])
@pytest.mark.parametrize("first_question_pending", [False, True])
def test_retry_label_cannot_bypass_intake_or_status_filter(
        reply_moves, monkeypatch, idea, action, retry_marker, first_question_pending):
    from types import SimpleNamespace

    card = {**_card(idea), "intake_pending": True, "first_question_pending": first_question_pending}
    question = card["actions"][1]
    monkeypatch.setattr(reply_moves, "compose", lambda _: pytest.fail("A status row must not compose next moves"))
    malformed = [None, "invalid-action", {}, {"action": []}, {"action": {}},
                 {"action": "chat.reply", "payload": "invalid"}]
    for payload in ({"idea": idea, "retry": retry_marker, "question_id": "not-a-question"}, "invalid", ["invalid"], 1, True):
        unsafe = {"label": "Retry", "action": action, "retry": retry_marker, "payload": payload}
        assert reply_moves.intake_actions(card, [unsafe, *malformed, question]) == (
            [] if first_question_pending else [question])
        row = SimpleNamespace(kind="status", actions_json=json.dumps([unsafe, *malformed]), citations_json="[]")
        assert reply_moves.attach("ven_retry", row) is False
        assert json.loads(row.actions_json) == []


@pytest.mark.parametrize("kind", ["entry_pending", "idea_clarification_pending", "status"])
def test_status_keeps_retry_without_composing_next_moves(reply_moves, monkeypatch, kind):
    from types import SimpleNamespace

    monkeypatch.setattr(reply_moves, "compose", lambda _: pytest.fail("A status row must not compose next moves"))
    retry = {"label": "Try again", "action": "status", "payload": {}, "retry": True}
    row = SimpleNamespace(kind=kind, actions_json=json.dumps([*_card(DOMAINS[0])["actions"], retry]),
                          citations_json="[]")
    assert reply_moves.attach("ven_retry", row) is False
    assert json.loads(row.actions_json) == [retry]


def test_pending_intake_keeps_saved_founder_blocker_without_routine_buttons(env, reply_moves, monkeypatch):
    _, venture_id, _ = env
    need = "Solve the saved account check."
    state = {**_saved_state(DOMAINS[0]), "idea_state": "clarifying", "first_question_pending": True,
             "founder": {"what_we_need": need}}
    monkeypatch.setattr(reply_moves, "_snapshot", lambda _: state)
    card = reply_moves.compose(venture_id)
    assert card["kind"] == "status" and card["actions"] == []
    assert "Here's the one thing only you can do: " + need in card["body_md"]


def test_saved_paid_quote_keeps_its_exact_input_and_ceiling(reply_moves, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(reply_moves, "compose", lambda _: pytest.fail("A paid quote must keep its saved approval"))
    approval = {"action": "status", "gtm_quote": {"input": {"problem": "Do not send outreach."},
                "quote": {"input_digest": "saved-input", "resource_digest": "saved-resources", "ceiling_micro": 25000}}}
    row = SimpleNamespace(kind="gtm_diagnosis_quote", actions_json=json.dumps([approval]), citations_json="[]")
    assert reply_moves.attach("ven_quote", row) is False
    assert json.loads(row.actions_json) == [approval] and row.citations_json == "[]"


def test_pending_intake_keeps_sending_revocation(reply_moves):
    card = {**_card(DOMAINS[0]), "intake_pending": True, "first_question_pending": True}
    stop = {"label": "Stop sending", "action": "autopilot.send_off", "style": "ghost"}
    assert reply_moves.intake_actions(card, [*card["actions"], stop]) == [stop]


def test_saved_proposal_keeps_review_controls_without_routine_buttons(reply_moves, monkeypatch):
    from types import SimpleNamespace

    card = {**_card(DOMAINS[0]), "intake_pending": True, "actions": []}
    review = [{"label": label, "action": action, "payload": {"session_id": "saved-idea", "revision_id": "saved-revision"}}
              for label, action in [("Looks right", "idea.proposal.approve"), ("Edit", "idea.proposal.edit")]]
    assert reply_moves.intake_actions(card, [review[0], _card(DOMAINS[0])["actions"][2], review[1]]) == review
    monkeypatch.setattr(reply_moves, "compose", lambda _: card)
    row = SimpleNamespace(kind="text", actions_json=json.dumps(review), citations_json="[]")
    assert reply_moves.attach("ven_proposal", row) is False
    assert json.loads(row.actions_json) == review and row.citations_json == "[]"
