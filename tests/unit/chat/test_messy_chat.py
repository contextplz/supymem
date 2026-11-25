"""Messy chat keeps navigation, unrelated discussion and old choices out of answers."""

import asyncio
import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.company_os_harness import append_idea_revision
from tests.unit.chat.test_founder_chat_controls import founder


@pytest.fixture(params=[
    ("Inspection scheduling for building managers", "Building managers"),
    ("Training journals for amateur runners", "Amateur runners"),
], ids=["b2b", "consumer"])
def chat_case(founder, request):
    from app.db.models import Venture
    from app.state import db

    problem, audience = request.param
    with db()() as session:
        venture = session.get(Venture, founder["vid"])
        venture.name = problem
        venture.raw_idea = problem
        revision = append_idea_revision(session, founder["original"], summary=problem,
            structured=json.dumps({"summary": problem, "problem": problem,
                                   "customer": audience, "offer": problem}))
        session_id = revision.session_id
        revision_id = revision.id
        session.commit()
    return {**founder, "original": revision_id, "problem": problem, "audience": audience, "session_id": session_id}


def routing_card(data):
    from app.chat.store import append_message
    from app.ventures.journey_overrides import prepare

    reply = prepare(data["vid"], "journey.skip_step",
                    {"step": "research", "why": "Skip research for " + data["problem"]}, data["actor"])
    return append_message(data["vid"], role="assistant", body_md=reply["body_md"],
                          kind=reply["kind"], actions=reply["actions"])


def question_card(data, *, with_options=True):
    from app.chat.store import append_message
    from app.db.models import VentureDocument
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db
    from core.clock import utcnow

    question_id = "ict_hunt_question"
    text = "Who are you helping first?"
    now = utcnow()
    with db()() as session:
        idea = session.get(IdeaSession, data["session_id"])
        idea.state = "clarifying"
        idea.confirmed_revision_id = None
        idea.confirmed_at = None
        session.add(VentureDocument(id="intake_decision:" + data["vid"], venture_id=data["vid"],
                                   kind="intake_decision", title="Your chosen starting point",
                                   body_md=json.dumps({"path": "startup"}), created_at=now, updated_at=now))
        session.add(ClarifierTurn(id=question_id, session_id=idea.id, workspace_id=data["wid"],
                                 venture_id=data["vid"], turn=1, role="agent", kind="question", text=text,
                                 options=json.dumps({"field": "customer", "options": [data["audience"]] if with_options else []}),
                                 created_at=now))
        session.commit()
    append_message(data["vid"], role="assistant", kind="idea_clarification_ready", body_md=text,
                   actions=[{"label": data["audience"], "action": "chat.reply", "text": data["audience"],
                             "payload": {"question_id": question_id}, "style": "secondary"}] if with_options else [])
    return question_id


def saved_answers(data):
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    with db()() as session:
        return [turn.text for turn in session.scalars(select(ClarifierTurn).where(
            ClarifierTurn.session_id == data["session_id"], ClarifierTurn.role == "founder"))]


@pytest.mark.parametrize("text", ["Hello, how are you?", "Thanks for your help!", "Good morning."])
def test_small_talk_does_not_become_a_customer(chat_case, text):
    from app.chat.router import handle_message
    from app.idea.agent import founder_audience
    from app.intake.smart import _prefill, classify_fallback

    assert not founder_audience(text)
    assert "customer" not in _prefill(text)
    result = classify_fallback(text)
    assert not result.extracted.audience
    assert "customer" not in result.prefill_answers
    question_card(chat_case)
    asyncio.run(handle_message(chat_case["vid"], text, actor_subject=chat_case["actor"]))
    assert not saved_answers(chat_case)


def test_short_side_question_is_not_swallowed_by_a_route_card(chat_case, monkeypatch):
    from app.chat import router

    routing_card(chat_case)
    text = "ok, why skip research?"
    reply = asyncio.run(router._answer_confirmation(chat_case["vid"], text, chat_case["actor"]))
    assert reply is None, f"Your question was intercepted instead of answered: {reply}"
    requests = []

    async def answer(venture_id, context, intent, request, speaker, **kwargs):
        requests.append(request)
        return {"kind": "text", "body_md": "Your research helps you check assumptions. Ask which check you need next.",
                "actions": [], "citations": []}

    monkeypatch.setattr(router, "_dispatch_intent", answer)
    reply = asyncio.run(router.handle_message(chat_case["vid"], text, actor_subject=chat_case["actor"]))
    assert requests and "why skip research" in requests[-1]
    assert "check assumptions" in reply["body_md"]


@pytest.mark.parametrize("text", ["no", "not now", "cancel"])
def test_declining_a_route_card_retires_the_pending_choice(chat_case, text):
    from app.chat.router import _answer_confirmation
    from app.chat.guard import pending_confirmation
    from app.db.models import ChatMessage
    from app.db.supymem_models import ChatProposal
    from app.state import db

    card = routing_card(chat_case)
    proposal_id = card["actions"][0]["confirmation"]["resulting_state"]["proposal_id"]
    reply = asyncio.run(_answer_confirmation(chat_case["vid"], text, chat_case["actor"]))
    with db()() as session:
        proposal_state = session.get(ChatProposal, proposal_id).state
        saved_action = json.loads(session.get(ChatMessage, card["id"]).actions_json)[0]
    assert proposal_state != "pending" and pending_confirmation(chat_case["vid"]) is None, (
        f"Your no leaves the choice live: state={proposal_state}, reply={reply}")
    assert saved_action.get("disabled_reason"), f"Your declined choice still has an enabled button: {saved_action}"
    from app.chat.confirmations import card as saved_card
    with pytest.raises(ValueError):
        saved_card(chat_case["vid"], card["id"], "journey.skip_step", {"proposal_id": proposal_id},
                   actor_subject=chat_case["actor"])


@pytest.mark.parametrize("text", ["back", "That's not right, back", "Lovely weather for a picnic today", "Bananas",
                                "The {audience} watched a match yesterday"])
@pytest.mark.parametrize("explicit", [False, True])
def test_back_does_not_become_a_saved_customer_answer(chat_case, text, explicit):
    from app.chat.router import handle_message

    text = text.format(audience=chat_case["audience"].lower())
    question_id = question_card(chat_case)
    reply = asyncio.run(handle_message(chat_case["vid"], text, actor_subject=chat_case["actor"],
                                      reply_to_question=explicit, question_id=question_id if explicit else None))
    answers = saved_answers(chat_case)
    assert not answers, f"Your navigation request was saved as customer testimony: {answers}; reply={reply}"


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("lead", ["We help ", "I want "], ids=["answer", "correction"])
def test_another_conversation_does_not_answer_an_unseen_question(chat_case, explicit, lead):
    from app.chat.conversations import bind_conversation, create
    from app.chat.router import handle_message
    from app.chat.store import list_messages

    question_id = question_card(chat_case)
    other = create(chat_case["vid"], idempotency_key="hunt-other-conversation")["id"]
    with bind_conversation(chat_case["vid"], other):
        assert not list_messages(chat_case["vid"])
        reply = asyncio.run(handle_message(chat_case["vid"], lead + chat_case["audience"].lower(),
                                           actor_subject=chat_case["actor"], reply_to_question=explicit,
                                           question_id=question_id if explicit else None))
    answers = saved_answers(chat_case)
    assert not answers, f"Your other chat consumed a question you cannot see there: {answers}; reply={reply}"


@pytest.mark.parametrize("with_options", [True, False], ids=["choices", "free-text"])
@pytest.mark.parametrize("answer_style", ["short", "description", "long_start"])
def test_a_visible_explicit_question_answer_is_saved_once(chat_case, with_options, answer_style):
    from app.chat.router import handle_message

    question_id = question_card(chat_case, with_options=with_options)
    topic = chat_case["problem"].partition(" for ")[0].lower()
    answer = {"short": "We help " + chat_case["audience"].lower(),
              "description": chat_case["audience"] + " use " + topic + " every day.",
              "long_start": "Start with " + chat_case["audience"].lower() + " who need " + topic + " in their current workflow."}[answer_style]
    asyncio.run(handle_message(chat_case["vid"], answer, actor_subject=chat_case["actor"],
                               reply_to_question=True, question_id=question_id))
    assert saved_answers(chat_case) == [answer]


def test_a_stale_question_chip_does_not_save_an_answer(chat_case):
    from app.chat.router import handle_message
    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    old_question_id = question_card(chat_case)
    reply = asyncio.run(handle_message(chat_case["vid"], chat_case["audience"], actor_subject=chat_case["actor"],
                                      reply_to_question=True, question_id="ict_old_question"))
    assert reply["id"].startswith("stale-")
    assert not saved_answers(chat_case)
    with db()() as session:
        old = session.get(ClarifierTurn, old_question_id)
        question_id = "ict_unshown_question"
        session.add(ClarifierTurn(id=question_id, session_id=old.session_id, workspace_id=old.workspace_id,
                                 venture_id=old.venture_id, turn=2, role="agent", kind="question", text=old.text,
                                 options=old.options, created_at=old.created_at - timedelta(seconds=1)))
        session.commit()
    asyncio.run(handle_message(chat_case["vid"], chat_case["audience"], actor_subject=chat_case["actor"],
                               reply_to_question=True, question_id=question_id))
    assert not saved_answers(chat_case), "Your old card answered a different unseen question with the same wording."


def test_stale_question_reply_gives_you_a_next_step(chat_case):
    from app.chat.router import handle_message
    from app.org.characters import parse_speaker

    question_card(chat_case)
    reply = asyncio.run(handle_message(chat_case["vid"], chat_case["audience"], actor_subject=chat_case["actor"],
                                      reply_to_question=True, question_id="ict_old_question"))
    body = parse_speaker(reply["body_md"])[1]
    assert not saved_answers(chat_case)
    assert any(action.get("action") or action.get("href") for action in reply["actions"]) or re.search(
        r"\b(?:review|choose|open|ask|continue|try|retry)\b", body, re.I), f"You get no next step: {reply}"


def test_requested_routing_change_runs_without_a_confirmation(chat_case):
    from app.chat.router import handle_message
    from app.ventures.journey_overrides import read

    reply = asyncio.run(handle_message(chat_case["vid"], "skip research", actor_subject=chat_case["actor"]))
    assert "research" in read(chat_case["vid"])["skipped"]
    assert not any(action.get("confirmation") for action in reply["actions"])
