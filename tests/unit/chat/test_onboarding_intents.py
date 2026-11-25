"""Explicit requests take precedence over a pending onboarding answer."""

import asyncio
import json

import pytest

from tests.company_os_harness import append_idea_revision
from tests.unit.chat.test_customers_in_chat import customer_chat as customer_chat, journey_db as journey_db  # noqa: F401

CONTINUE_NOTE = "You can choose Continue clarifying whenever you're ready."


@pytest.fixture(autouse=True)
def _local_owner_session(request):
    if "customer_chat" not in request.fixturenames:
        return
    client, _ = request.getfixturevalue("customer_chat")
    from app.core.local_mode import is_local

    if is_local():
        from core.local_security import _proxy_secret

        client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")


@pytest.mark.parametrize("action", ["search", "experiment"])
def test_explicit_search_and_experiment_requests_run_instead_of_next_step_guidance(customer_chat, monkeypatch, action):
    from app.chat import router
    from app.credits.policy import set_policy
    from app.db.supymem_models import EntryChatProposal, IdeaSession
    from app.gtm.discovery import request_audience
    from app.tenancy import bind

    client, data = customer_chat
    with data["factory"]() as session:
        session.get(IdeaSession, "customer_idea").state = "confirmed"
        session.get(EntryChatProposal, "customer_entry").route = "gtm"
        session.commit()
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    routed = []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append(intent)
        return {"kind": "text", "body_md": "Your requested work is ready. Review it next.", "actions": [], "citations": []}

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    text, expected = {
        "search": (f"Charlie, find {data['audience']} who deal with {data['problem']}.", "gtm_review"),
        "experiment": (f"Design an experiment to test if {data['audience']} will try a new way to address {data['problem']}.", "experiment_start"),
    }[action]
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": text})
    assert response.status_code == 200, response.text
    assert routed[-1:] == [expected], response.json()
    assert not any(action.get("confirmation") for action in response.json().get("actions", []))
    assert "I recommend:" not in response.json()["body_md"]
    if action == "search":
        with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
            assert data["audience"].casefold() in request_audience(text).casefold()


def test_mixed_setup_answer_preserves_one_complete_founder_message(customer_chat, monkeypatch):
    from app.chat import router
    from app.db.models import ChatMessage

    client, data = customer_chat
    _pending_question(data, "workflow")
    answer = f"We prepare a weekly checklist for {data['audience']}."
    request = "Show my saved summary."
    message = f"{answer} {request}"
    routed = []

    async def save_answer(venture_id, text, pending, actor_subject, **kwargs):
        assert text == answer and kwargs["persist"] is False
        return {"kind": "idea_clarification_ready"}

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append(text)
        return {"kind": "text", "body_md": "Your summary is ready. Review it next.", "actions": [], "citations": []}

    monkeypatch.setattr(router, "_answer_idea", save_answer)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    with data["factory"]() as session:
        before = {row.id for row in session.query(ChatMessage).filter_by(role="user")}
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": message})
    assert response.status_code == 200, response.text
    assert routed == [request]
    with data["factory"]() as session:
        messages = session.query(ChatMessage).filter_by(venture_id=data["vid"], role="user").all()
        assert [row.body_md for row in messages if row.id not in before] == [message]


def test_saved_guide_edits_bypass_setup_without_saving_answers(customer_chat, monkeypatch):
    from app.chat import assistant, router
    from app.chat.idea_bridge import (
        pending_question,
        split_question_reply,
        written_request,
    )
    from app.chat.store import append_message
    from app.db.supymem_models import ClarifierTurn

    client, data = customer_chat
    question, chips = _pending_question(data, "offer")
    saved = f"Your six-question interview guide for {data['audience']}: {data['problem']}."
    original = client.post(f"/ventures/{data['vid']}/chat/conversations",
        json={"title": "Your interview guide", "idempotency_key": "saved-guide"})
    assert original.status_code == 200, original.text
    append_message(data["vid"], role="assistant", body_md=saved, conversation_id=original.json()["id"])
    conversation = client.post(f"/ventures/{data['vid']}/chat/conversations",
        json={"title": "Your guide edits", "idempotency_key": "guide-edits"})
    assert conversation.status_code == 200, conversation.text
    append_message(data["vid"], role="assistant", kind="idea_clarification_ready", body_md=question,
        actions=chips, conversation_id=conversation.json()["id"])
    requests = (
        f"Make the saved interview guide shorter for {data['audience']}, cutting its length in half.",
        f"Shorten the saved interview guide for {data['audience']}, keeping only three questions.",
        f"Could you rewrite the saved interview guide for {data['audience']} as three concise questions?",
    )
    routed = []
    edited = "Your shorter guide is ready. You can review its three questions next."

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        assert intent == "assistant"
        messages = await assistant.question_messages(venture_id, text,
            brief={"venture_id": venture_id, "workspace_id": data["wid"], "name": data["problem"]},
            workspace_id=data["wid"])
        assert saved in json.dumps(messages)
        routed.append(text)
        return {"kind": "text", "body_md": edited, "actions": [], "citations": []}

    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    for request in requests:
        response = client.post(f"/ventures/{data['vid']}/chat",
            json={"message": request, "conversation_id": conversation.json()["id"]})
        assert response.status_code == 200, response.text
        assert edited in response.json()["body_md"]
        assert question not in response.json()["body_md"]
        assert written_request(request)
        assert not written_request(request + " Then send it to everyone.")
    assert routed == list(requests)
    assert pending_question(data["vid"], "", force=True) == ("customer_idea", "intent_question")
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).filter_by(role="founder").count() == 0
        saved_question = session.get(ClarifierTurn, "intent_question")
        description = f"My product gives {data['audience']} a shorter guide to {data['problem']}."
        assert not written_request(description)
        assert split_question_reply(description, question=saved_question) == (description, "")


def test_returning_requests_recall_only_this_ventures_saved_work(customer_chat, monkeypatch):
    from app.chat import assistant, router
    from app.chat.idea_bridge import (
        pending_question,
        split_question_reply,
        written_request,
    )
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn
    from core.clock import utcnow

    client, data = customer_chat
    _pending_question(data, "customer")
    saved = f"Your discovery worksheet for {data['audience']}: {data['problem']}."
    other = "Private inventory assumptions for an unrelated flower wholesaler."
    with data["factory"]() as session:
        session.add(Venture(id="ven_unrelated", workspace_id=data["wid"], name="Unrelated venture",
            raw_idea=other, stage="idea", autonomy_level="L1", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    old = client.post(f"/ventures/{data['vid']}/chat/conversations", json={"title": "Your worksheet", "idempotency_key": "saved-worksheet"})
    assert old.status_code == 200, old.text
    append_message(data["vid"], role="assistant", body_md=saved, conversation_id=old.json()["id"])
    from app.entry import service
    with service.venture_scope("ven_unrelated", workspace_id=data["wid"], actor_id=data["actor"]):
        append_message("ven_unrelated", role="assistant", body_md=other)
    new = client.post(f"/ventures/{data['vid']}/chat/conversations", json={"title": "Your next visit", "idempotency_key": "return-worksheet"})
    assert new.status_code == 200, new.text
    requests = ("I've returned. Where did we stop with the worksheet?",
                "Continue the saved discovery worksheet into a seven-day pilot plan. Keep it as text.")
    routed = []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        messages = await assistant.question_messages(venture_id, text,
            brief={"venture_id": venture_id, "workspace_id": data["wid"], "name": data["problem"]},
            workspace_id=data["wid"])
        content = json.dumps(messages)
        assert saved in content and other not in content
        routed.append(text)
        return {"kind": "text", "body_md": saved + " You can review your pilot plan next.", "actions": [], "citations": []}

    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    with data["factory"]() as session:
        question = session.get(ClarifierTurn, "intent_question")
        for request in requests:
            assert split_question_reply(request, question=question) == ("", request)
    assert written_request(requests[1])
    for request in requests:
        response = client.post(f"/ventures/{data['vid']}/chat", json={"message": request, "conversation_id": new.json()["id"]})
        assert response.status_code == 200, response.text
        assert saved in response.json()["body_md"]
    assert routed == list(requests)
    assert pending_question(data["vid"], "", force=True) == ("customer_idea", "intent_question")
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).filter_by(role="founder").count() == 0


def test_escaped_search_dispatch_starts_work_not_another_card(customer_chat, monkeypatch):
    from app.chat import execute, router
    from app.chat.context import assemble
    from app.chat.rephrase import INPUT

    _client, data = customer_chat
    requests = [f"Just find {data['audience']} please", f"Also, find 4 {data['audience']} to talk to",
                f"{data['audience']} ke leads dhoondh do"]
    starts = []

    async def prepare(*args, **kwargs):
        return {"kind": "confirmation", "body_md": "Review your search.", "actions": [], "citations": []}

    async def start(venture_id, reply, actor_subject, *, actions):
        starts.append(actions)
        return {**reply, "kind": "next_up", "body_md": "Your search is queued. Review the results next."}

    monkeypatch.setattr(router, "_prepare_intent", prepare)
    monkeypatch.setattr(execute, "start_requested_work", start)
    token = INPUT.set({"questionnaire_escaped": True})
    try:
        for text in requests:
            reply = asyncio.run(router._dispatch_intent(data["vid"], assemble(data["vid"]), "gtm_review",
                text, None, actor_subject=data["actor"]))
            assert reply["kind"] == "next_up" and starts[-1] == {"gtm.find"}
        for text in ("How many leads do we have?", "Who is my ideal customer?"):
            reply = asyncio.run(router._dispatch_intent(data["vid"], assemble(data["vid"]), "gtm_review",
                text, None, actor_subject=data["actor"]))
            assert reply["kind"] == "confirmation"
        assert len(starts) == len(requests)
    finally:
        INPUT.reset(token)


@pytest.mark.parametrize("card_reply", [False, True])
def test_typed_action_escapes_question_card(customer_chat, monkeypatch, card_reply):
    from app.chat import router
    from app.chat.idea_bridge import pending_question
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn

    client, data = customer_chat
    question, _ = _pending_question(data, "workflow")
    role = data["audience"].split(" in ")[0]
    place = "Calgary" if data["business_model"] == "b2b" else "Brisbane"
    requests = [f"Could you please find me 4 {role} in {place} I could talk to?",
                f"Find 4 {role} near {place}.", f"Just find me the {role} in {place} please",
                f"Will you look up {role} in {place}?", f"Get me 4 contacts for {role} in {place}.",
                f"{place} mein {role} ke leads dhoondh do please"]
    routed = []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append((intent, text))
        return {"kind": "next_up", "body_md": "Your requested work is queued. Review the results next.",
                "actions": [], "citations": []}

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    for request in requests:
        reply = asyncio.run(router.handle_message(data["vid"], request, actor_subject=data["actor"],
            reply_to_question=card_reply, question_id="intent_question" if card_reply else None))
        assert routed and routed[-1][1] == request
        assert question not in reply["body_md"]
        assert not reply["actions"]
        assert not reply["body_md"].endswith(CONTINUE_NOTE)
        assert pending_question(data["vid"], "", force=True) == ("customer_idea", "intent_question")
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == 1
        assert not session.query(Job).count()
    stale = asyncio.run(router.handle_message(data["vid"], request, actor_subject=data["actor"],
        reply_to_question=True, question_id="older_question"))
    assert stale["id"].startswith("stale-")
    assert len(routed) == len(requests)


@pytest.mark.parametrize("separator", [". Also, ", "; ", ", and ", " aur ", "\n"])
def test_mixed_question_answer_routes_remaining_request(customer_chat, monkeypatch, separator):
    from app.chat import customers, router
    from app.chat.idea_bridge import split_question_reply
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession

    client, data = customer_chat
    question, _ = _pending_question(data, "workflow")
    answer = f"We draft a daily checklist for {data['problem'].lower()}"
    request = (f"{data['audience']} ke 4 leads dhoondh do please" if separator == " aur "
               else f"could you please find me 4 {data['audience']} I could talk to?")
    message = answer + separator + request
    expected_answer, expected_request = split_question_reply(message)
    assert expected_answer == answer + ("." if separator.startswith(".") else "")
    assert expected_request.endswith(request) and "Also" not in expected_answer
    routed = []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append(text)
        return {"kind": "next_up", "body_md": "Your search is queued. Review the results next.",
                "actions": [], "citations": []}

    async def run_command(venture_id, name, match, text, actor):
        assert name == "find"
        return await dispatch(venture_id, None, "gtm_review", text, None)

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    monkeypatch.setattr(customers, "run_command", run_command)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    payload = {"message": message}
    if separator.startswith(".") or separator == "\n":
        payload.update(reply_to_question=True, question_id="intent_question")
    response = client.post(f"/ventures/{data['vid']}/chat", json=payload)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert routed == [expected_request], (reply["kind"], reply["body_md"], reply["actions"])
    assert question not in reply["body_md"]
    assert reply["actions"] == [{"label": "Continue clarifying", "action": "idea.review", "style": "secondary"}]
    with data["factory"]() as session:
        saved = session.query(ClarifierTurn).filter_by(role="founder").one()
        assert saved.text == expected_answer and json.loads(saved.options)["field"] == "workflow"
        revision = session.get(IdeaRevision, session.get(IdeaSession, "customer_idea").current_revision_id)
        assert json.loads(revision.structured)["_answered_fields"]["workflow"] == expected_answer
        job = session.query(Job).filter_by(kind="idea.clarify_turn").one()
        assert json.loads(job.payload_json)["text"] == expected_answer
    assert "what to ask" not in reply["body_md"] and reply["body_md"].count("?") == 0


@pytest.mark.parametrize("missing_location", [False, True])
def test_semantic_request_escapes_questionnaire_once(customer_chat, monkeypatch, missing_location):
    from app.chat import llm_router, router
    from app.db.supymem_models import ClarifierTurn

    client, data = customer_chat
    _pending_question(data, "workflow")
    request = f"Μπορείς να εντοπίσεις \"{data['audience']}\" για μια συνέντευξη;"
    translated = f"Find {data['audience']} for an interview"
    classified, routed = [], []

    async def classify(venture_id, text, acts, context, **kwargs):
        assert acts == [("general", text)]
        classified.append(text)
        return (llm_router.Decision([], clarify="Which location should I search?") if missing_location
                else llm_router.Decision([("gtm_review", translated)]))

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append((intent, text))
        return {"kind": "next_up", "body_md": "Your search is queued. Review the results next.",
                "actions": [], "citations": []}

    monkeypatch.setattr(llm_router, "route", classify)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": request,
        "reply_to_question": True, "question_id": "intent_question"})
    assert response.status_code == 200, response.text
    assert classified == [request]
    assert routed == ([("clarify", "Which location should I search?")] if missing_location else [("gtm_review", translated)])
    assert not response.json()["actions"]
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == 1


def test_semantic_quoted_request_stays_a_question_answer(customer_chat, monkeypatch):
    from app.chat import llm_router, router
    from app.db.supymem_models import ClarifierTurn

    client, data = customer_chat
    _pending_question(data, "workflow")
    request = f"find 4 {data['audience']} to interview"
    answer = f'We use a checklist containing "{request}" as sample text.'
    routed = []

    async def classify(*args, **kwargs):
        return llm_router.Decision([("gtm_review", request)])

    async def dispatch(*args, **kwargs):
        routed.append(args)
        return {"kind": "next_up", "body_md": "Your search is queued. Review the results next.",
                "actions": [], "citations": []}

    monkeypatch.setattr(llm_router, "route", classify)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": answer,
        "reply_to_question": True, "question_id": "intent_question"})
    assert response.status_code == 200, response.text
    assert routed == [] and response.json()["kind"] == "idea_clarification_pending"
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).filter_by(role="founder").one().text == answer


def test_splitter_preserves_request_qualifiers_and_descriptive_answer_constraints(customer_chat):
    from app.chat.idea_bridge import split_question_reply
    from app.chat.llm_router import _command_evidence

    _client, data = customer_chat
    request = f"Without starting any work, explain the saved assumptions for {data['audience']}."
    assert split_question_reply(request) == ("", request)
    answer = (f"Start with {data['audience']} handling {data['problem'].lower()} through a daily checklist. "
              "Recruit only people who agree to review a prototype. Do not send messages or collect private data.")
    assert split_question_reply(answer) == (answer, "")
    action = f"Also, find 4 {data['audience']} to talk to. Do not send invitations."
    assert split_question_reply(answer + " " + action) == (answer, action)
    quoted = f'Our checklist for {data["audience"]} says "daily review, find 4 people and send invitations".'
    assert len(_command_evidence(quoted, preserve_length=True)) == len(quoted)
    assert _command_evidence(quoted) == quoted.split('"')[0] + " " + quoted.rsplit('"', 1)[1]
    assert split_question_reply(quoted) == (quoted, "")
    assert split_question_reply(quoted + " " + action) == (quoted, action)


@pytest.mark.parametrize("answer_template", [
    "I helped {audience} with {problem}. I have not interviewed anyone, run an experiment or received an order.",
    "You have not interviewed anyone, run an experiment or received an order for {audience}.",
    "A daily overview for {audience}, draft summaries and create reminders.",
    "The first task is to track {problem}, then create a daily summary.",
    "It will help {audience} track progress and show what is missing.",
    "Will track {problem} and create a daily summary.",
    "Have not interviewed anyone, run an experiment or received an order for {audience}.",
    "Am planning a daily overview for {audience}, draft summaries and create reminders.",
])
def test_descriptive_and_negated_answers_only_split_explicit_followup_requests(customer_chat, answer_template):
    from app.chat.idea_bridge import direct_request, split_question_reply

    _, data = customer_chat
    answer = answer_template.format(audience=data["audience"], problem=data["problem"].lower())
    requests = [f"Find 4 {data['audience']} to talk to. Do not send invitations.",
                "Could you explain the saved assumptions?",
                "How should I validate this?",
                "Without starting any work, explain the next step.",
                "Can outside researchers validate this?"]
    assert not direct_request(answer), answer
    assert split_question_reply(answer) == (answer, ""), answer
    for request in requests:
        message = answer + " " + request
        assert split_question_reply(message) == (answer, request), message
    request = requests[1]
    assert split_question_reply(answer.rstrip(".") + ", please " + request) == (
        answer.rstrip("."), "please " + request)
    for request in requests:
        assert direct_request(request), request
        assert split_question_reply(request) == ("", request)
    quoted = f'Will support {data["audience"]} with a note saying "Can you create a reminder?"'
    assert not direct_request(quoted)
    assert split_question_reply(quoted) == (quoted, "")


def _pending_question(data, field="customer", *, pressure_test=False):
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from core.clock import utcnow

    question = f"What should you tell us about {field.replace('_', ' ')}?"
    options = {"field": field, "options": [data["audience"]]}
    if pressure_test:
        from app.gstack.office_hours import default_picks
        options["field"] = "gstack_Q1"
        queue = [default_picks({"audience": data["audience"], "problem": data["problem"]})[1]]
        options.update(phase="gstack", gstack_id="Q1", gstack_name="Demand Reality", index=1, total=2, queue=queue)
    with data["factory"]() as session:
        if pressure_test:
            session.get(IdeaSession, "customer_idea").state = "validating"
        session.get(Venture, data["vid"]).name = data["problem"]
        session.add(ClarifierTurn(id="intent_question", session_id="customer_idea",
            workspace_id=data["wid"], venture_id=data["vid"], turn=1, role="agent",
            kind="question", text=question, options=json.dumps(options), created_at=utcnow()))
        session.commit()
    chips = [{"label": data["audience"], "action": "chat.reply", "text": data["audience"],
              "payload": {"question_id": "intent_question"}, "style": "secondary"}]
    append_message(data["vid"], role="assistant", kind="idea_clarification_ready",
                   body_md=question, actions=chips)
    return question, chips


@pytest.mark.parametrize("field", ["offer", "workflow", "requirements", "constraints",
                                  "desired_outcome", "first_user_path", "current_solution"])
@pytest.mark.parametrize("card_reply", [False, True])
def test_product_imperatives_answer_the_showing_question(customer_chat, monkeypatch, field, card_reply):
    from app.chat import llm_router, router
    from app.chat.idea_bridge import pending_question, split_question_reply
    from app.chat.store import append_message
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession

    client, data = customer_chat
    question, chips = _pending_question(data, field)
    answer = ("Check vaccination records and flag missed reminders for clinic staff"
              if data["business_model"] == "b2b" else
              "Generate bedtime routines and track overnight sleep for parents")
    if field == "current_solution":
        answer = "Review dosage conflicts" if data["business_model"] == "b2b" else "Find soothing lullabies"
    elif field == "desired_outcome":
        answer = ("Update treatment reminders for staff" if data["business_model"] == "b2b" else
                  "Summarize overnight changes for parents")
    elif field == "constraints":
        answer = ("Do not expose patient records without permission" if data["business_model"] == "b2b" else
                  "Do not share infant data without permission")
    message = f"Assumed: {data['audience']}\n{answer}" if field == "requirements" else answer
    routed, classified = [], []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append(text)
        return {"kind": "next_up", "body_md": "Your requested work is queued. Review the results next.",
                "actions": [], "citations": []}

    async def classify(venture_id, text, acts, context, **kwargs):
        classified.append(text)
        return llm_router.Decision([("general", text)])

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    monkeypatch.setattr(llm_router, "route", classify)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    for request in ("Write a launch plan", "Research my competitors", "Find me 10 customers",
                    "Research competitors", "Find 3 customers", "Check my launch checklist",
                    "Build a landing page", "Make a plan", "Check our launch checklist", "Draft us an outline",
                    f"Could you {answer.lower()}?", f"Draft one interview email for {data['audience']}",
                    "I need you to check the saved files", "Open waitlist"):
        assert pending_question(data["vid"], request) is None
    with data["factory"]() as session:
        current = session.get(ClarifierTurn, "intent_question")
        assert split_question_reply(answer + "; Research my competitors", question=current) == (answer, "Research my competitors")
    append_message(data["vid"], role="assistant", kind="text",
                   body_md="Your previous question is saved. Ask me what you need next.")
    assert pending_question(data["vid"], answer) is None
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": answer})
    assert response.status_code == 200, response.text
    assert routed == [answer]
    append_message(data["vid"], role="assistant", kind="idea_clarification_ready", body_md=question, actions=chips)
    routed.clear()
    classified.clear()
    assert pending_question(data["vid"], answer) == ("customer_idea", "intent_question")
    payload = {"message": message}
    if card_reply:
        payload.update(reply_to_question=True, question_id="intent_question")
    response = client.post(f"/ventures/{data['vid']}/chat", json=payload)
    assert response.status_code == 200, response.text
    assert routed == [] and classified == []
    with data["factory"]() as session:
        saved = session.query(ClarifierTurn).filter_by(role="founder").one()
        assert saved.text == message and json.loads(saved.options)["field"] == field
        if field == "requirements":
            assert json.loads(saved.options)["suggested_text"] == data["audience"]
        revision = session.get(IdeaRevision, session.get(IdeaSession, "customer_idea").current_revision_id)
        assert json.loads(revision.structured)["_answered_fields"][field] == answer
        job = session.query(Job).filter_by(kind="idea.clarify_turn").one()
        assert json.loads(job.payload_json)["text"] == message
        assert not session.query(CreditReservation).count()


def test_pressure_test_next_step_is_one_recommendation_not_another_question(customer_chat, monkeypatch):
    from app.chat import next_moves, router
    from app.chat.idea_bridge import pending_question
    from tests.unit.chat.test_next_moves import _saved_state

    _, data = customer_chat
    question, _ = _pending_question(data, pressure_test=True)
    monkeypatch.setattr(next_moves, "_snapshot", lambda _: {**_saved_state(data["problem"]), "questions_pending": False})
    monkeypatch.setattr(next_moves, "_fresh_recommendation", lambda _: None)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    for request in ("what should I do next?", "What can I do next?", "What's next?",
                    "Could you please tell me what should I do next?", "Just tell me, what next?"):
        reply = asyncio.run(router.handle_message(data["vid"], request, actor_subject=data["actor"]))
        assert reply["kind"] == "next_moves" and question not in reply["body_md"]
        assert reply["body_md"].count("I recommend:") == reply["body_md"].count("Why:") == 1
        assert sum(action["style"] == "primary" for action in reply["actions"]) == 1
        assert len([action for action in reply["actions"] if action["action"] != "idea.review"]) <= 3
        assert not any(action["label"] == "Continue clarifying" for action in reply["actions"])
        assert pending_question(data["vid"], "", force=True) == ("customer_idea", "intent_question")


def test_pressure_test_single_email_requests_escape_and_show_review_draft(customer_chat, monkeypatch):
    from app.chat import intent, router
    from app.chat.idea_bridge import pending_question, split_question_reply
    from app.db.models import PlannedTask
    from app.db.supymem_models import ClarifierTurn
    from app.planning import execute

    _, data = customer_chat
    question, _ = _pending_question(data, pressure_test=True)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    drafts = []

    async def compose(*args, **kwargs):
        drafts.append((args, kwargs))
        return {"status": "done", "draft_text": f"Draft 1\nSubject: Your experience\n\nHi {data['audience']}, can we talk?"}

    monkeypatch.setattr(execute, "execute_planned_task", compose)
    audience = data["audience"]
    for request in (f"Could you please write me a short cold email I can send to {audience}?",
                    f"Draft one outreach email for {audience}.",
                    f"Just compose a brief email to {audience}.",
                    f"{audience} ke liye ek short cold email bana do please"):
        assert intent.classify_intent(request) == "outreach_draft"
        assert split_question_reply(request) == ("", request)
        mixed = request.endswith("bana do please")
        message = f"One person asked us for help with {data['problem']}. Also, {request}" if mixed else request
        reply = asyncio.run(router.handle_message(data["vid"], message, actor_subject=data["actor"]))
        assert "Subject: Your experience" in reply["body_md"] and "Nothing was sent" in reply["body_md"]
        assert question not in reply["body_md"]
        assert any(action.get("action") == "idea.review" for action in reply["actions"]) == mixed
        pending = pending_question(data["vid"], "", force=True)
        assert pending and pending[0] == "customer_idea"
        assert (pending[1] != "intent_question") == mixed
    assert len(drafts) == 4
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == 3
        assert any("asked us for help" in turn.text for turn in session.query(ClarifierTurn).filter_by(role="founder"))
        tasks = session.query(PlannedTask).all()
        assert len(tasks) == 4 and all("review only" in task.detail for task in tasks)
    for suffix in (" Also send it now.", " And search for more people.", " And find more people.", " Also build a dashboard.",
                   ". Do not prepare a new task yet."):
        assert not intent.single_outreach_email_request(request + suffix)


def test_pressure_test_competitor_questions_route_to_research_not_testimony(customer_chat, monkeypatch):
    from app.chat import router
    from app.chat.idea_bridge import pending_question
    from app.db.supymem_models import ClarifierTurn

    _, data = customer_chat
    question, _ = _pending_question(data, pressure_test=True)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    routed = []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append((intent, text))
        return {"kind": "text", "body_md": "Your competitor research is ready. Review the comparison next.", "actions": [], "citations": []}

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    for request in ("who are my main competitors?", "Which are our direct competitors?",
                    "Please, who are my competitors?", "Just tell me, what are my top competitors?"):
        reply = asyncio.run(router.handle_message(data["vid"], request, actor_subject=data["actor"]))
        assert routed[-1][0] == "research.competitors"
        assert question not in reply["body_md"]
        assert not reply["actions"]
        assert pending_question(data["vid"], "", force=True) == ("customer_idea", "intent_question")
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == 1


@pytest.mark.parametrize("affirmative", ["yes", "ok", "sure", "go ahead", "do it"])
@pytest.mark.parametrize("question_target", [None, "intent_question"])
def test_legacy_typed_confirmation_uses_latest_card_without_answering_onboarding(customer_chat, monkeypatch, affirmative, question_target):
    from app.authz.workspace import subject_person_id
    from app.chat.store import append_message, list_messages
    from app.credits import metered
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import ClarifierTurn, EntryChatProposal
    from app.entry import chat_start

    client, data = customer_chat
    from app.core.local_mode import is_local
    from tests.idea_fixtures import install_provider
    if is_local():
        with monkeypatch.context() as provider_patch:
            install_provider(provider_patch)
    _pending_question(data)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A routing confirmation must not call a provider")

    monkeypatch.setattr(metered, "llm", forbidden)
    append_message(data["vid"], role="assistant", kind="gtm_skip_proposal", body_md="An older choice.",
        actions=[{"label": "Continue", "confirmation": {"action": "gtm.route.confirm",
            "resulting_state": {"proposal_id": "older_proposal"}}}])
    path = f"/ventures/{data['vid']}/chat"
    preview = chat_start.preview(text="find my first customers", actor_id=subject_person_id(data["actor"]),
        target_venture_id=data["vid"], request_id="legacy-customer-request")
    proposal_id = preview["proposal"]["id"]
    card = {"label": "Continue", "confirmation": {"action": "gtm.route.confirm",
        "resulting_state": {"proposal_id": proposal_id}}}
    saved = append_message(data["vid"], role="assistant", kind="gtm_skip_proposal",
        body_md=preview["proposal"]["message"], actions=[card])
    card = saved["actions"][0]

    stale = client.post(path, json={"message": affirmative, "reply_to_question": True, "question_id": "old_question"})
    assert stale.status_code == 200 and "already answered" in stale.json()["body_md"]
    for text in ("maybe", "yes?", "yes, but not now"):
        ambiguous = client.post(path, json={"message": text})
        assert ambiguous.status_code == 200, ambiguous.text
        assert ambiguous.json()["kind"] == "text"
        assert not any(action.get("confirmation") for action in ambiguous.json()["actions"])
        assert "card above" in ambiguous.json()["body_md"]
    body = {"message": affirmative}
    if question_target:
        body.update(reply_to_question=True, question_id=question_target)
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "gtm_route_confirmed"
    assert data["audience"] in reply["body_md"]
    assert reply["actions"][0]["action"] == "gtm.status" and not reply["actions"][0].get("confirmation")
    button = client.post(f"{path}/gtm/route/confirm", json={"proposal_id": proposal_id})
    assert button.status_code == 200 and button.json()["id"] == reply["id"]
    assert len([message for message in list_messages(data["vid"])
                if message["role"] == "user" and message["body_md"] == affirmative]) == 1

    for action, source in [("gtm.send", "typed"), ("social_post", "typed"),
                           ("approve", "typed"), ("gtm.route.confirm", "voice")]:
        append_message(data["vid"], role="assistant", kind="confirmation", body_md="Review this choice.",
            actions=[{"label": "Continue", "confirmation": {"action": action,
                "resulting_state": {"proposal_id": "must_not_run"}}}])
        refused = client.post(path, json={"message": affirmative, "source": source})
        assert refused.status_code == 200, refused.text
        if is_local():
            assert "Tap" in refused.json()["body_md"] or "Tell me which suggested action you want to run" in refused.json()["body_md"]
            assert not refused.json().get("action_started")
        else:
            assert "Tap" in refused.json()["body_md"]
    with data["factory"]() as session:
        assert session.get(EntryChatProposal, proposal_id).state == "committed"
        assert not session.query(ClarifierTurn).filter_by(role="founder").count()
        assert session.query(Job).count() == 1 and not session.query(CreditReservation).count()


@pytest.mark.parametrize("field", ["offer", "customer", "workflow", "current_solution", "problem",
                                    "first_user_path", "pricing_hypothesis"])
def test_explicit_lookup_reads_sources_then_resumes_same_onboarding_question(customer_chat, monkeypatch, field):
    from app.chat import execute
    from app.db.supymem_models import ClarifierTurn
    from app.research import explore

    client, data = customer_chat
    question, chips = _pending_question(data, field)
    url = f"https://{data['business_model']}.example/about"
    summary = f"According to {url}, this product helps {data['audience']} with {data['problem'].lower()}."
    calls = []

    async def search(request, *, max_results, extract):
        calls.append((request, max_results, extract))
        return {"mode": "ok", "hits": [{"title": data["problem"], "url": url, "snippet": summary}],
                "pages": [{"url": url, "markdown": summary}]}

    async def summarize(request, look, *, venture_id=None):
        assert request == query and look["pages"][0]["url"] == url and venture_id == data["vid"]
        return summary

    monkeypatch.setattr(explore, "explore_web", search)
    monkeypatch.setattr(execute, "_answer_from_search", summarize)
    queries = [f"Do you understand what {data['problem']} is? Can you check online?",
               f"Could you please look up {data['problem']} on the web.", f"Can you read {url}?"]
    path = f"/ventures/{data['vid']}/chat"
    for query in queries:
        response = client.post(path, json={"message": query})
        assert response.status_code == 200, response.text
        reply = response.json()
        assert reply["body_md"].count(summary) == 1 and question not in reply["body_md"]
        assert not reply["body_md"].endswith(CONTINUE_NOTE)
        assert not reply["actions"]
        assert reply["citations"] == [{"label": data["problem"], "href": url}]
    assert calls == [(query, 5, 3) for query in queries]
    if field == "offer":
        proposed = client.post(path, json={"message": "find my first customers"})
        assert proposed.status_code == 200, proposed.text
        assert proposed.json()["kind"] == "gtm_route_confirmed"
        assert not any(action.get("confirmation") for action in proposed.json()["actions"])
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == 1
        assert session.get(ClarifierTurn, "intent_question").role == "agent"


def _numbered_question(data):
    from app.chat.store import append_message
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack.office_hours import PHASE, question_reply

    question, chips = _pending_question(data, "current_solution")
    with data["factory"]() as session:
        turn = session.get(ClarifierTurn, "intent_question")
        turn.options = json.dumps({"phase": PHASE, "gstack_id": "Q2", "gstack_name": "Current workarounds",
            "index": 2, "total": 4, "field": "current_solution", "options": ["Paper lists"], "queue": []})
        session.get(IdeaSession, "customer_idea").state = "validating"
        session.commit()
    question, chips = question_reply("intent_question")
    append_message(data["vid"], role="assistant", kind="idea_clarification_ready", body_md=question, actions=chips)
    return question, chips


@pytest.mark.parametrize("answer_type", ["terse", "descriptive", "request"])
def test_direct_requests_escape_numbered_question_and_real_answers_still_save(customer_chat, monkeypatch, answer_type):
    from app.chat import router
    from app.chat.idea_bridge import pending_question
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import ClarifierTurn

    client, data = customer_chat
    question, _ = _numbered_question(data)
    routed = []

    async def dispatch(venture_id, context, intent, text, speaker, **kwargs):
        routed.append(text)
        return {"kind": "text", "body_md": "You can review your next step.", "actions": [], "citations": []}

    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    requests = [f"Show the next step for {data['audience']}", "Can you suggest a smaller version?",
                "Plan my next experiments for this idea", "What experiments should I run next?",
                "test my idea", "book 3 customer calls this week", f"rename this venture to {data['problem']}"]
    for text in requests:
        response = client.post(f"/ventures/{data['vid']}/chat", json={"message": text})
        assert response.status_code == 200, response.text
        reply = response.json()
        assert not reply["body_md"].endswith(CONTINUE_NOTE)
        assert question not in reply["body_md"] and "Question 3 of 4" not in reply["body_md"]
        assert not reply["parts"][-1]["text"].endswith(CONTINUE_NOTE)
        assert pending_question(data["vid"], "Paper lists", force=True) == ("customer_idea", "intent_question")
    assert len(routed) == len(requests) and routed[:4] == requests[:4]
    secret = "sk-test-" + "syntheticcredential" * 4
    monkeypatch.setattr("app.secrets.service.request_secret", lambda *args, **kwargs: {
        "spec_id": "openai.api_key", "form": {"label": "Model key"}, "status": "needs_you", "env_refs": []})
    protected = client.post(f"/ventures/{data['vid']}/chat", json={"message":
        f"Assumed: {secret}\nPlan my next experiments for this idea"})
    assert protected.status_code == 200, protected.text
    assert protected.json()["kind"] == "needs_you" and secret not in protected.text
    from app.core.local_mode import is_local

    if is_local():
        assert "secret_request" not in protected.json()
        assert any(action.get("href") == "/settings/local" for action in protected.json()["actions"])
    else:
        assert protected.json()["secret_request"]["status"] == "needs_you"
    assert len(routed) == len(requests)
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == 1
        assert not session.query(Job).count() and not session.query(CreditReservation).count()
    for answer in ("Paper lists", f"They use paper lists to handle {data['problem'].lower()} for {data['audience']}."):
        assert pending_question(data["vid"], answer) == ("customer_idea", "intent_question")
    answer = {"terse": "Paper lists",
              "descriptive": f"They use paper lists to handle {data['problem'].lower()} for {data['audience']}.",
              "request": requests[0]}[answer_type]
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": answer,
        "reply_to_question": True, "question_id": "intent_question"})
    assert response.status_code == 200, response.text
    assert len(routed) == len(requests) + int(answer_type == "request")
    with data["factory"]() as session:
        saved = session.query(ClarifierTurn).filter_by(role="founder").all()
        assert [turn.text for turn in saved] == ([] if answer_type == "request" else [answer])


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("idea_confirmed", [False, True])
@pytest.mark.hosted_only
def test_experiment_planning_uses_existing_action_only_when_flagged(customer_chat, monkeypatch, enabled, idea_confirmed):
    from app.chat import router, market_beliefs
    from app.credits import policy
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession

    client, data = customer_chat
    if idea_confirmed:
        with data["factory"]() as session:
            idea = session.get(IdeaSession, "customer_idea")
            idea.state, idea.confirmed_revision_id = "confirmed", idea.current_revision_id
            session.commit()
    else:
        _numbered_question(data)
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": enabled}, "test")
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    planned, ordinary = [], []

    async def prepare(venture_id, payload=None):
        planned.append(venture_id)
        return {"kind": "market_beliefs", "body_md": f"Your tests for {data['audience']} need your approval.",
                "actions": [], "citations": []}

    async def general(venture_id, context, intent, text, speaker, **kwargs):
        ordinary.append(intent)
        return {"kind": "text", "body_md": "You can review your saved work.", "actions": [], "citations": []}

    original_dispatch = router._dispatch_intent

    async def dispatch(*args, **kwargs):
        return await original_dispatch(*args, **kwargs) if args[2] == "experiment.beliefs.prepare" else await general(*args, **kwargs)

    monkeypatch.setattr(market_beliefs, "prepare", prepare)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    stale = client.post(f"/ventures/{data['vid']}/chat", json={"message": "plan my experiments",
        "reply_to_question": True, "question_id": "old_question"})
    assert stale.status_code == 200 and "already answered" in stale.json()["body_md"]
    assert not planned and not ordinary
    requests = ["plan my experiments", "Plan my next experiments for this idea", "what should I test next",
                "what experiments should I run", "run experiments for me", "test my idea",
                "Assumed: Paper lists\nPlan my next experiments for this idea",
                "I want to plan my experiments", "I would like to plan my experiments"]
    for text in requests:
        response = client.post(f"/ventures/{data['vid']}/chat", json={"message": text})
        assert response.status_code == 200, response.text
        if not idea_confirmed:
            assert not response.json()["body_md"].endswith(CONTINUE_NOTE)
        if enabled and not idea_confirmed:
            assert not any(action.get("action", "").startswith("experiment.") for action in response.json()["actions"])
    assert len(planned) == (len(requests) if enabled and idea_confirmed else 0)
    assert len(ordinary) == (0 if enabled else len(requests))
    async def fallback(context, text, speaker=None, **kwargs):
        return {"kind": "text", "body_md": "You can review your saved work.", "actions": [], "citations": []}

    monkeypatch.setattr(router.general, "handle", fallback)
    reply = asyncio.run(router._prepare_intent(data["vid"], router.assemble(data["vid"]),
        "experiment.beliefs.prepare", "plan my experiments", None, data["actor"]))
    if enabled and not idea_confirmed:
        assert not any(action.get("action", "").startswith("experiment.") for action in reply["actions"])
    else:
        assert reply["kind"] == ("market_beliefs" if enabled else "text")
    assert len(planned) == (len(requests) + 1 if enabled and idea_confirmed else 0)
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == (0 if idea_confirmed else 1)
        assert not session.query(Job).count() and not session.query(CreditReservation).count()


def test_new_experiment_requests_prepare_the_stated_hypothesis_without_a_saved_test(customer_chat, monkeypatch):
    import json
    from app.autopilot import outreach
    from app.chat import experiments
    from app.credits import metered, policy
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession

    client, data = customer_chat
    audience, problem = (("warehouse operators", "inventory alerts") if data["business_model"] == "b2b"
                         else ("home gardeners", "watering reminders"))
    hypothesis = f"{audience} would pay for {problem}"
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True, "goals_v1": True}, "test")
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")

    def forbidden(*args, **kwargs):
        pytest.fail("Designing a private interview test must not call a provider or create a generic goal")

    planned = []

    async def design(system, request, venture_id, **kwargs):
        assert hypothesis in request
        planned.append(request)
        return {"hypothesis": hypothesis,
                "action": "Prepare recruiting drafts and an interview script for five matched people.",
                "success_measure": "From five matched people, record recent behavior, spending and concrete commitments. Treat opinions alone as inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", design)
    monkeypatch.setattr(metered, "llm", forbidden)
    monkeypatch.setattr("app.tasks.chat.classify", forbidden)
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = f"{problem}\nAudience: {audience}"
        revision = append_idea_revision(session, "customer_revision", structured=json.dumps({"customer": audience, "problem": problem,
                                         "business_model": data["business_model"]}))
        idea = session.get(IdeaSession, "customer_idea")
        idea.state, idea.confirmed_revision_id = "confirmed", revision.id
        session.commit()
    path = f"/ventures/{data['vid']}/chat"
    created = client.post(path + "/conversations", json={"idempotency_key": "hypothesis-test"})
    assert created.status_code == 200, created.text
    conversation_id = created.json()["id"]
    assert not experiments.ledger.list_for_venture(data["vid"])
    started = False
    for run, request in ((False, f"Please design an experiment about {hypothesis}."),
                         (True, f"Run a small experiment to test whether {hypothesis}."),
                         (False, f"Try a quick test to see if {hypothesis}."),
                         (False, f"Could you test whether {hypothesis}?"),
                         (False, f"Try an experiment for {hypothesis}."),
                         (True, f"Run an experiment to check that {hypothesis}.")):
        response = client.post(path, json={"message": request, "conversation_id": conversation_id})
        assert response.status_code == 200, response.text
        reply = response.json()
        if run:
            assert reply["kind"] == "gtm_search_progress" and audience in reply["body_md"], reply
        else:
            assert hypothesis in reply["body_md"], reply
            assert "Prepared your experiment" in reply["body_md"] and "Interview script:" in reply["body_md"]
            assert "five matched people" in reply["body_md"] and "Treat opinions alone as inconclusive" in reply["body_md"]
        assert "Nothing" in reply["body_md"] and "—" not in reply["body_md"]
        assert not any(action.get("confirmation") for action in reply["actions"])
        assert any(hypothesis in row["hypothesis"] for row in experiments.ledger.list_for_venture(data["vid"]))
        started = started or run
        with data["factory"]() as session:
            jobs = session.query(Job).all()
            assert len(jobs) == int(started)
            if jobs:
                assert jobs[0].kind == "gtm.journey.prepare" and hypothesis in json.loads(jobs[0].payload_json)["discovery_note"]
        assert not outreach.drafts(data["vid"])
    assert len(planned) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_customer_call_goal_escapes_question_and_keeps_existing_reviews(customer_chat, monkeypatch, enabled):
    from types import SimpleNamespace
    from app.chat import conversations, router
    from app.core.local_mode import is_local
    from app.credits import metered, policy
    from app.db.supymem_models import ClarifierTurn
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import chat as task_chat
    from tests.task_billing_fixtures import checkpoint, install_recovery, synthetic_receipt

    client, data = customer_chat
    _numbered_question(data)
    conversation = conversations.create(data["vid"], idempotency_key="goal-routing")
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"background_work": enabled}, {})
    else:
        policy.set_policy("feature_flags", {"goals_v1": enabled}, "test")
    monkeypatch.setenv("CHAT_ROUTER_MODE", "off")
    recovery = install_recovery(monkeypatch)
    planner_calls = []

    async def llm(messages, **kwargs):
        planner_calls.append(kwargs)
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content="{}"))])
        receipt = synthetic_receipt(kwargs)
        checkpoint(response, kwargs, receipt)
        recovery.confirm(receipt)
        return response

    monkeypatch.setattr(metered, "llm", llm)
    calls = []
    original_handle = task_chat.handle_message
    original_dispatch = router._dispatch_intent

    async def handle(*args, **kwargs):
        calls.append((args, kwargs))
        return await original_handle(*args, **kwargs)

    async def dispatch(*args, **kwargs):
        if enabled:
            return await original_dispatch(*args, **kwargs)
        return {"kind": "text", "body_md": "You can review your saved work.", "actions": [], "citations": []}

    monkeypatch.setattr(task_chat, "handle_message", handle)
    monkeypatch.setattr(router, "_dispatch_intent", dispatch)
    text = "book 3 customer calls this week"
    payload = {"message": text, "conversation_id": conversation["id"], "idempotency_key": "goal-request"}
    response = client.post(f"/ventures/{data['vid']}/chat", json=payload)
    assert response.status_code == 200, response.text
    assert not response.json()["body_md"].endswith(CONTINUE_NOTE)
    replay = client.post(f"/ventures/{data['vid']}/chat", json=payload)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert len(calls) == int(enabled)
    assert len(planner_calls) == int(enabled)
    if enabled:
        for effect in ("send invitations", "email prospects", "then dm participants"):
            refused = client.post(f"/ventures/{data['vid']}/chat", json={**payload,
                "message": text + " and " + effect, "idempotency_key": "unapproved-" + effect})
            assert refused.status_code == 200, refused.text
            assert refused.json()["kind"] == "refusal", refused.json()
            assert len(calls) == len(planner_calls) == 1
    with data["factory"]() as session:
        assert session.query(ClarifierTurn).count() == 1
        assert session.query(Task).count() == int(enabled)
        if enabled:
            task = session.query(Task).one()
            assert task.goal == text and json.loads(task.scope_json)["_goals_v1"] is True
            run = session.query(TaskRun).filter_by(task_id=task.id).one()
            steps = session.query(TaskStep).filter_by(run_id=run.id).order_by(TaskStep.position).all()
            assert [step.skill for step in steps] == ["tasks.booking_link", "gtm.find", "outreach.draft", "tasks.customer_calls"]
            assert all(step.state != "completed" for step in steps[1:])


def _text_only_question(data):
    from app.chat.store import append_message
    from app.db.supymem_models import ClarifierTurn

    question, _ = _pending_question(data, "workflow")
    with data["factory"]() as session:
        session.get(ClarifierTurn, "intent_question").options = json.dumps({"field": "workflow", "options": []})
        session.commit()
    append_message(data["vid"], role="assistant", kind="idea_clarification_ready", body_md=question, actions=[
        # As the live app shows a text-only question: no options, only the skip chip naming the question.
        {"label": "Skip the questions", "action": "chat.reply", "text": "Skip the questions",
         "payload": {"text": "Skip the questions", "question_id": "intent_question"}}])


@pytest.mark.parametrize("answer", [
    "Two asset managers showed me their spreadsheets and recommend dropping trades that wear the battery too fast",
    "Three ward sisters keep a paper rota and copy every overdue action into an email on Fridays",
])
@pytest.mark.parametrize("decision", ["clarify", "work"])
def test_long_typed_answer_to_a_text_only_question_is_saved(customer_chat, monkeypatch, answer, decision):
    from app.chat import llm_router
    from app.db.supymem_models import ClarifierTurn
    from sqlalchemy import select

    client, data = customer_chat
    _text_only_question(data)

    async def route(*args, **kwargs):
        return (llm_router.Decision([], clarify="Which one do you mean?") if decision == "clarify"
                else llm_router.Decision([("research", answer)]))

    monkeypatch.setattr(llm_router, "route", route)
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": answer})
    assert response.status_code == 200, response.text
    with data["factory"]() as session:
        turns = session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == "customer_idea",
                                                            ClarifierTurn.role == "founder")).all()
        assert any(answer in turn.text for turn in turns)


@pytest.mark.parametrize("request_text", [
    "I would like you to look up my competitors for this idea",
    "Find me the public pricing pages of the three biggest alternatives today",
    "Please find the public pricing pages of the three biggest alternatives",
])
def test_long_message_addressing_charlie_still_reaches_the_router(customer_chat, monkeypatch, request_text):
    from app.chat import llm_router

    client, data = customer_chat
    _text_only_question(data)
    routed = []

    async def route(venture_id, text, *args, **kwargs):
        routed.append(text)
        return llm_router.Decision([], clarify="Which competitors do you mean?")

    monkeypatch.setattr(llm_router, "route", route)
    response = client.post(f"/ventures/{data['vid']}/chat", json={"message": request_text})
    assert response.status_code == 200, response.text
    assert request_text in routed
