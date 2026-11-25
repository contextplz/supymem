import json
import re

import pytest
from tests.company_os_harness import append_idea_revision

from tests.integration.test_chat_first_idea_bridge import client as bridge_client


def private_plan_completion(messages):
    from tests.idea_fixtures import completion

    saved = json.loads(messages[-1]["content"].split("Saved context:\n", 1)[1])
    audience = saved.get("customer") or "your saved audience"
    response = completion()
    response.choices[0].message.content = json.dumps({
        "hypothesis": f"You can reduce a recurring problem for {audience}.",
        "action": f"You interview five consenting adults from {audience} about recent workarounds. Do not send anything.",
        "success_measure": "Pass if three describe costly workarounds; fail with fewer; missing interviews are inconclusive."})
    return response


@pytest.fixture
def client(bridge_client, monkeypatch):
    from app.core.local_mode import is_local
    from tests.idea_fixtures import completion, install_provider

    if is_local():
        from app.intake import application
        from app.intake.smart import classify_fallback
        from app.ventures import founder_facts

        async def interpret(text, **kwargs):
            return classify_fallback(text).to_dict()

        async def extract(*args, **kwargs):
            return {}

        monkeypatch.setattr(application, "interpret", interpret)
        monkeypatch.setattr(founder_facts, "extract", extract)
        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")

        def provider(messages, kwargs):
            if "Prepare only the private market-test plan" in messages[0]["content"]:
                return private_plan_completion(messages)
            return completion()

        install_provider(monkeypatch, provider)
    return bridge_client


DOMAINS = [
    ("A marketplace where technicians publish inspection checklists. Fleet managers buy checklists and run vehicle inspections. Managers choose a checklist, then record each inspection. The platform charges a subscription and pays technicians a commission.",
     "For people who want to run inspections and also people who want to publish checklists",
     "Vehicle inspection checklist marketplace"),
    ("A marketplace where chefs publish cooking lessons. Families buy lessons and cook dinners together. Families choose a lesson, then follow the recipe. The platform charges a subscription and pays chefs a commission.",
     "For people who want to cook dinners and also people who want to publish lessons",
     "Family cooking lesson marketplace"),
]


@pytest.mark.parametrize("audience,person,company,idea", [
    ("regional freight operations directors", "Morgan Hale", "Harbor Dispatch", "reconcile delivery records for human reviewers"),
    ("parents planning accessible holidays", "Riley Stone", "Family Circle", "plan step-free trips without making bookings"),
])
@pytest.mark.usefixtures("client")
def test_customer_inside_an_answer_keeps_the_audience_not_product_prose(audience, person, company, idea):
    from app.clarify.dilemmas import venture_context
    from app.entry.chat_start import _onboarding_progress
    from app.idea.agent import answer_prefill
    from app.idea.runner import _known_fields
    from app.intake.smart import _prefill

    answer = (f"You would help {audience} like {person} at {company}. "
        f"{person} described a recurring delay. That is one anecdote, not validated willingness to pay.")
    assert _prefill(answer)["customer"] == audience
    assert answer_prefill(answer, "customer")["customer"] == audience
    assert _prefill(f"My customer is {audience} like {person} at {company}.")["customer"] == audience
    progress = _onboarding_progress({"path": "startup", "extracted": {"idea": idea, "language": "en"}},
        f"I want to {idea}.\nAudience: {answer}")
    assert "customer" in progress["completed"] and progress.get("field") != "customer"
    invalid = "You can combine records, but I cannot access private customer data."
    assert _prefill("Audience: " + invalid).get("customer") is None
    assert not answer_prefill(invalid, "customer").get("customer")
    assert venture_context({"customer": invalid, "audience": audience}, clip_audience=False)[0] == audience
    corrected = f"My customer is {audience} like {person} at {company}."
    assert _known_fields({"original_idea": f"I want to {idea}.", "project_intent": "startup",
        "understanding": {"customer": "human reviewers"}, "answered_fields": {"customer": "human reviewers"},
        "conversation": [{"role": "founder", "kind": "answer", "field": "customer", "text": corrected}]})["customer"] == audience
    assert _known_fields({"original_idea": f"I want to {idea}.", "project_intent": "startup",
        "understanding": {"customer": audience}, "answered_fields": {"customer": audience},
        "conversation": [{"role": "founder", "kind": "answer", "field": "customer", "text": audience},
            {"role": "founder", "kind": "answer", "field": "customer", "text": invalid}]})["customer"] == audience


@pytest.mark.parametrize("audience,idea,limit", [
    ("regional transport operations managers", "reconcile depot delivery records", "dispatch staff would retain every routing decision"),
    ("families planning accessible holidays", "build a planner that finds step-free holiday routes", "parents would retain every booking decision"),
])
@pytest.mark.usefixtures("client")
def test_release_customer_sentences_preserve_only_the_audience(audience, idea, limit):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import PageContext, _prefill, _validated_result, audience_phrase, classify_fallback

    assert audience_phrase(f"{audience}, and {limit}") == audience
    assert _prefill(f"I worked with {audience} for twelve years.").get("customer") is None
    assert _prefill("A tool that helps people manage their work.").get("customer") is None
    for text in (f"I want to {idea}. The first customer would be {audience}.",
                 f"I want to {idea} for {audience}, and {limit}."):
        assert _prefill(text)["customer"] == audience
        fallback = classify_fallback(text)
        model = fallback.to_dict()
        model["prefill_answers"]["customer"] = f"{audience}, and {limit}"
        validated = _validated_result(model, text, PageContext(), fallback)
        assert validated.prefill_answers["customer"] == audience and validated.extracted.audience == audience
        progress = _onboarding_progress({"path": "startup", "extracted": {"idea": idea, "language": "en"}}, text)
        assert "customer" in progress["completed"] and progress.get("field") != "customer"


def _send(client, venture_id, text, **extra):
    from core.ids import new_id
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": text, "idempotency_key": new_id("chatreq"), **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _click(client, venture_id, reply, action, *, text=None):
    choice = next(item for item in reply["actions"] if item.get("action") == action
                  and (text is None or item.get("payload", {}).get("text") == text))
    if action == "chat.reply":
        return _send(client, venture_id, choice["payload"]["text"],
                     **({"question_id": choice["payload"]["question_id"], "reply_to_question": True}
                        if choice["payload"].get("question_id") else {}))
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={
        **({"message_id": reply["id"]} if choice.get("confirmation") else {}), "action": action,
        "payload": choice.get("payload") or choice.get("confirmation", {}).get("resulting_state") or {}})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
@pytest.mark.parametrize("field", ["offer", "customer"])
@pytest.mark.parametrize("forcing_id", ["Q2", "Q4"])
def test_selected_direction_is_the_latest_saved_answer_in_every_stage(client, monkeypatch, idea, audience, summary, field, forcing_id):
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.gstack import office_hours
    from app.idea.service import _next_turn_number, get_session
    from app.state import db
    from tests.idea_fixtures import completion, install_provider, validation_completion
    from tests.security.test_durable_journey_boundary import run_queued

    venture_id, session_id, question_id = _pending_questions(client, idea, audience, summary, field=field)
    chosen = summary if field == "offer" else audience.removeprefix("For ")
    rejected_context = {"offer": "Automated stock audits for warehouse supervisors", "customer": "Warehouse supervisors"}
    rejected = rejected_context[field]
    with db()() as session:
        question = session.get(ClarifierTurn, question_id)
        question.options = json.dumps({"field": field, "options": [chosen, rejected]})
        session.commit()
    shown = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"})
    assert shown.status_code == 200, shown.text
    _send(client, venture_id, "Assumed: " + chosen, question_id=question_id, reply_to_question=True)
    structured = get_session(session_id)["current_revision"]["structured"]
    assert structured[field] == structured["_answered_fields"][field] == chosen
    stale = {"summary": summary, **rejected_context, "problem": "delayed warehouse stock audits"}
    def provider(messages, kwargs):
        return (validation_completion(messages) if kwargs.get("purpose") == "idea.validate"
                else completion(proposal=True, understanding=stale))

    calls = install_provider(monkeypatch, provider)
    run_queued("idea.clarify_turn")
    assert calls
    assert get_session(session_id)["current_revision"]["structured"][field] == chosen
    with db()() as session:
        row = session.get(IdeaSession, session_id)
        revision = session.get(IdeaRevision, row.current_revision_id)
        legacy = json.loads(revision.structured)
        legacy[field] = rejected
        legacy["_answered_fields"].pop(field)
        revision = append_idea_revision(session, revision.id, structured=json.dumps(legacy))
        evidence = "I spoke with two people about their workflow yesterday."
        for role, kind, text, metadata in [
            ("agent", "question", "Who have you interviewed about this problem?", {"field": field, "options": [evidence]}),
            ("founder", "answer", "Assumed: " + evidence, {"field": field, "provenance": "suggested_choice", "founder_text": "", "suggested_text": evidence}),
        ]:
            session.add(ClarifierTurn(id=f"legacy-evidence-{role}", session_id=row.id, workspace_id=row.workspace_id,
                venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role=role, kind=kind,
                text=text, options=json.dumps(metadata), revision_id=revision.id, created_at=row.updated_at))
            session.flush()
        snapshot = office_hours.saved_snapshot(session, venture_id, revision.id)
        assert snapshot[field] == chosen
        pick = {"gstack_id": forcing_id, "gstack_name": "Current workarounds", "contextual_options": True,
            "text": f"How do {chosen if field == 'customer' else rejected} handle stock audits today?", "options": [rejected, "No tools yet"],
            "why": "Clarify your next decision."}
        question = office_hours.add_question_turn(session, row, pick, [], 1, 1, row.updated_at)
        row.state = "validating"
        next_question_id = question.id
        session.commit()
    reply = _send(client, venture_id, "Continue clarifying")
    assert all(value not in reply["body_md"] for value in rejected_context.values())
    assert "stock audits" not in reply["body_md"]
    from app.core.local_mode import is_local
    if is_local() and forcing_id == "Q4" and field == "offer":
        assert not any(action.get("payload", {}).get("question_id") == next_question_id for action in reply["actions"])
    else:
        assert any(action.get("payload", {}).get("question_id") == next_question_id for action in reply["actions"])
    resumed = _send(client, venture_id, "Continue clarifying")
    if is_local() and forcing_id == "Q4" and field == "offer":
        assert resumed["kind"] == "idea_clarification_pending"
        run_queued("idea.validate")
        resumed = _send(client, venture_id, "Continue clarifying")
        assert resumed["kind"] == "text" and "**What we found**" in resumed["body_md"]
        assert any(action.get("action") == "idea.confirm" for action in resumed["actions"])
    else:
        assert resumed["kind"] == "idea_clarification_ready"
    assert _send(client, venture_id, "Skip the questions")["kind"] == "confirmation"
    with db()() as session:
        row = session.get(IdeaSession, session_id)
        assert json.loads(session.get(IdeaRevision, row.current_revision_id).structured)[field] == chosen
    corrected = client.patch(f"/ventures/{venture_id}/idea/sessions/{session_id}/revision", json={"patch": {field: rejected}})
    assert corrected.status_code == 200, corrected.text
    assert get_session(session_id)["current_revision"]["structured"][field] == rejected


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
@pytest.mark.parametrize("roles_only", [False, True])
def test_direct_audience_answer_is_not_reasked(client, roles_only, idea, audience, summary):
    from app.entry.chat_start import _onboarding_progress
    from app.intake.smart import classify_fallback
    from app.idea.agent import core_followup, core_answer_status
    from app.idea.service import get_session
    from tests.security.test_durable_journey_boundary import run_queued

    if roles_only:
        audience = "For people who want to use it and people who want to make it"
    text = idea + "\nAudience: " + audience
    progress = _onboarding_progress(classify_fallback(text).to_dict(), text)
    assert core_answer_status("Use it", 0, field="customer") == (False, False)
    assert core_answer_status("Make it", 0, field="customer") == (False, False)
    assert progress["completed"] == ["offer", "customer"]
    assert not progress.get("question")
    venture_id, sid, question_id = _pending_questions(client, idea, audience, summary, field="customer")
    question_text = core_followup("customer", 0)
    shown = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"})
    assert shown.status_code == 200, shown.text
    _send(client, venture_id, audience, question_id=question_id, reply_to_question=True)
    run_queued("idea.clarify_turn")
    saved = get_session(sid)
    assert (saved["turns"][-1].get("options") or {}).get("field") != "customer"
    assert saved["current_revision"]["structured"]["customer"].removeprefix("For ") == audience.removeprefix("For ")
    assert question_text not in _send(client, venture_id, "Continue clarifying")["body_md"]


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_initial_actor_description_prefills_without_a_model(client, monkeypatch, idea, audience, summary):
    from app.entry.chat_start import _onboarding_progress
    from app.intake import smart

    monkeypatch.setattr(smart, "_call_model", lambda *args, **kwargs: pytest.fail("Prefill must not call a model"))
    progress = _onboarding_progress(smart.classify_fallback(idea).to_dict(), idea)
    assert progress["completed"] == ["offer", "customer"]
    assert not progress.get("question")
    for description in ["The software records shipment statuses.", "The tools store favorite recipes."]:
        assert "customer" not in smart._prefill(description)
    for description in ["People publish maintenance playbooks.", "People publish cooking recipes."]:
        assert smart._prefill(description)["customer"].startswith("People who ")


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_summary_uses_inference_and_keeps_skip_controls_out(client, idea, audience, summary):
    from app.idea.agent import Clarification, Understanding, answer_metadata
    from app.idea.runner import _core_clarification, _understanding_summary
    from app.intake.smart import _prefill

    transcript = idea + "\nAudience: " + audience + "\nAudience: Skip this question"
    assert _prefill(transcript)["customer"] == audience.removeprefix("For ")
    assert "Skip" not in answer_metadata(transcript)["founder_text"]
    for actual_audience in ("Skip hire companies", "Skip-hire companies", "Skip rope learners", "Skip-rope learners"):
        assert actual_audience in answer_metadata("Audience: " + actual_audience)["founder_text"]
    inferred = Clarification(case="know", action="proposal", message="Check this summary.",
        understanding=Understanding(summary=summary, offer=summary, customer=audience.removeprefix("For ")))
    result = _core_clarification({"original_idea": transcript, "project_intent": "startup"}, inferred)
    rendered = _understanding_summary(result.understanding)
    assert result.understanding.offer == summary
    continued = _core_clarification({"original_idea": idea, "project_intent": "startup",
        "understanding": inferred.understanding.model_dump(), "answered_fields": {"offer": idea, "customer": audience},
        "conversation": [{"role": "founder", "field": "customer", "text": audience}]}, inferred.model_copy(deep=True))
    assert continued.understanding.offer == summary
    assert summary in rendered and "Skip this question" not in rendered
    assert rendered.count("**Customer:") == 1 and rendered.count(summary) == 1  # the offer no longer repeats the summary
    assert idea not in rendered
    earlier = _prefill(idea)["customer"]
    corrected = _core_clarification({"original_idea": idea, "project_intent": "startup",
        "understanding": {"customer": earlier}, "answered_fields": {"customer": earlier},
        "conversation": [{"role": "founder", "kind": "answer", "field": "customer", "text": audience}]},
        inferred.model_copy(update={"understanding": inferred.understanding.model_copy(update={"customer": earlier})}))
    assert corrected.understanding.customer.removeprefix("For ") == audience.removeprefix("For ")
    repaired = _core_clarification({"original_idea": transcript, "project_intent": "startup",
        "understanding": {"customer": "Skip this question"}, "answered_fields": {"customer": "Skip this question"}},
        inferred.model_copy(update={"understanding": inferred.understanding.model_copy(update={"customer": "Skip this question"})}))
    assert repaired.understanding.customer == audience.removeprefix("For ")
    labelled = _understanding_summary(Understanding(offer="Offer: " + summary,
        customer="Audience: " + audience.removeprefix("For ")))
    assert summary in labelled and labelled.count("**Customer:") == 1 and labelled.count("**What it does:") == 1


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_continuing_a_question_does_not_repeat_a_stale_summary(client, monkeypatch, idea, audience, summary):
    from app.chat.execute import _review_idea
    from app.gstack import office_hours

    venture_id, _, question_id = _pending_questions(client, idea, audience, summary)
    body = f"What have you learned from {audience.removeprefix('For ')}?"
    actions = [{"action": "chat.reply", "payload": {"question_id": question_id}}]
    monkeypatch.setattr(office_hours, "question_reply", lambda turn_id: (body, actions))
    reply = _review_idea(venture_id)
    assert reply["body_md"] == body
    assert reply["actions"] == actions
    assert reply["kind"] == "idea_clarification_ready"


def _pending_questions(client, idea, audience, summary, *, field=None):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession, IdeaRevision, ClarifierTurn
    from app.idea.agent import core_followup
    from app.gstack import office_hours
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    response = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert response.status_code == 201, response.text
    venture_id, now = response.json()["id"], utcnow()
    with db()() as session:
        venture = session.get(Venture, venture_id)
        row = IdeaSession(id=new_id("ids"), workspace_id=venture.workspace_id, venture_id=venture_id,
            case="know", state="clarifying" if field else "validating", question_budget=6, questions_asked=0, created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        fields = {"summary": summary, "offer": idea, "customer": audience.removeprefix("For "),
                  "_answered_fields": {"offer": idea, "customer": audience.removeprefix("For ")}}
        if field:
            fields.pop(field)
            fields["_answered_fields"].pop(field)
        revision = IdeaRevision(id=new_id("irv"), session_id=row.id, workspace_id=row.workspace_id,
            venture_id=venture_id, rev=1, source="founder", summary=summary,
            structured=json.dumps(fields), created_at=now)
        session.add(revision)
        session.flush()
        row.current_revision_id = revision.id
        if field:
            turn = ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id,
                venture_id=venture_id, turn=1, role="agent", kind="question", text=core_followup(field, 0),
                options=json.dumps({"field": field, "options": []}), revision_id=revision.id, created_at=now)
            session.add(turn)
        else:
            picks = office_hours.default_picks(fields)
            turn = office_hours.add_question_turn(session, row, picks[0], picks[1:], 1, len(picks), now)
        sid, question_id = row.id, turn.id
        session.commit()
    return venture_id, sid, question_id


@pytest.mark.parametrize("idea,audience,directive", [
    ("Schedule equipment calibration", "Laboratory supervisors",
     "Turn the equipment checks we discussed into a fifteen-minute agenda for laboratory supervisors."),
    ("Record amateur astronomy sessions", "Backyard stargazers",
     "Turn the observation prompts we discussed into a half-hour agenda for backyard stargazers."),
], ids=["b2b", "consumer"])
def test_document_transformation_does_not_answer_the_open_question(client, monkeypatch, idea, audience, directive):
    from app.chat import assistant
    from app.chat.idea_bridge import written_request
    from app.idea.service import get_session

    assert written_request(directive)
    assert not written_request(f'"{directive}"')
    assert not written_request(directive.rstrip(".") + " and publish it.")
    assert not written_request(f"A tool that turns observations into an agenda for {audience.lower()}.")
    venture_id, session_id, _question_id = _pending_questions(client, idea, audience, idea, field="offer")
    _send(client, venture_id, "Continue clarifying")
    before = get_session(session_id)
    calls = []

    async def write(venture_id, question, **kwargs):
        calls.append(question)
        return {"kind": "text", "body_md": "Your agenda is ready. Choose a time to try it.", "actions": [], "citations": []}

    monkeypatch.setattr(assistant, "answer_question", write)
    reply = _send(client, venture_id, directive)
    assert calls == [directive], reply
    after = get_session(session_id)
    assert after["turns"] == before["turns"]
    assert after["state"] == before["state"]
    assert not any(turn["role"] == "founder" and turn["text"] == directive for turn in after["turns"])


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
@pytest.mark.parametrize("auto_questions", [False, True])
def test_continue_clarifying_reads_the_next_unanswered_question(client, monkeypatch, auto_questions, idea, audience, summary):
    from app.idea.service import get_session

    if auto_questions:
        from app.config import settings
        from tests.idea_fixtures import completion, install_provider, questions_completion
        from tests.security.test_durable_journey_boundary import run_queued

        monkeypatch.setattr(settings, "new_user_flow", "on")
        inflight = []

        def provider(messages, kwargs):
            if "short GStack office-hours session" in messages[0]["content"]:
                inflight.append(_send(client, venture_id, "Continue clarifying"))
                return questions_completion()
            return completion(proposal=True, understanding={"summary": summary, "offer": summary,
                "customer": audience.removeprefix("For ")})

        calls = install_provider(monkeypatch, provider)
        preview = client.post("/entry/chat/preview", json={"text": idea})
        committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
        assert committed.status_code == 200, committed.text
        venture_id = committed.json()["venture_id"]
        run_queued("idea.clarify_turn")
        sid = client.get(f"/ventures/{venture_id}/idea/sessions").json()["items"][0]["id"]
        question_id = get_session(sid)["turns"][-1]["id"]
        assert inflight and inflight[0]["kind"] == "idea_clarification_pending", (calls, get_session(sid))
        assert any(item.get("payload", {}).get("text") == "Skip the questions" for item in inflight[0]["actions"]), inflight
    else:
        venture_id, sid, question_id = _pending_questions(client, idea, audience, summary)
    from app.chat.store import list_messages
    first = next((message for message in list_messages(venture_id)
        if any(action.get("payload", {}).get("question_id") == question_id
               for action in message.get("actions") or [])), None)
    if first is None:
        first = _send(client, venture_id, "Continue clarifying")
    answer = _send(client, venture_id, "I spoke with two people who described this problem last week.",
                   question_id=question_id, reply_to_question=True)
    resumed = _send(client, venture_id, "Continue clarifying")
    assert resumed["body_md"] != first["body_md"]
    assert get_session(sid)["turns"][-1]["id"] != question_id
    assert "Question 2" in answer["body_md"] and "Question 2" in resumed["body_md"]


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_experiment_waits_for_understanding_and_uses_saved_audience(client, idea, audience, summary, monkeypatch):
    from app.core.local_mode import is_local
    from app.chat.experiments import _experiment_snapshot
    from app.idea.service import get_session

    venture_id, sid, question_id = _pending_questions(client, idea, audience, summary)
    snapshot = _experiment_snapshot(venture_id)
    assert snapshot["journey"]["icp"]["who"] == audience.removeprefix("For ")
    reply = _send(client, venture_id, "Design an experiment")
    if is_local():
        from desktop_runtime import agent_status
        monkeypatch.setattr(agent_status, "statuses", lambda: [])
        assert "Prepared your experiment" in reply["body_md"]
        assert get_session(sid)["turns"][-1]["id"] == question_id
        assert not any(item.get("confirmation") or item.get("action") == "idea.confirm" for item in reply["actions"])
        switch = client.put("/local/capabilities/run_for_me_experiments", json={"on": False})
        assert switch.status_code == 200, switch.text
    else:
        assert "confirm your idea" in reply["body_md"].lower()
        assert any(item.get("payload", {}).get("question_id") == question_id for item in reply["actions"])
    assert "First, who are you trying to reach?" not in reply["body_md"]
    blocked = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.page.prepare", "payload": {"kind": "headlines"}})
    assert blocked.status_code == 200, blocked.text
    if is_local():
        assert "Coding tools" in blocked.json()["body_md"]
        assert any(action.get("href") == "/settings/local#coding-tools" for action in blocked.json()["actions"])
        restored = client.put("/local/capabilities/run_for_me_experiments", json={"on": True})
        assert restored.status_code == 200, restored.text
    else:
        assert "confirm your idea" in blocked.json()["body_md"].lower()
    _send(client, venture_id, "Skip the questions")
    confirmed = _send(client, venture_id, "yes correct")
    assert get_session(sid)["state"] == "confirmed", confirmed
    assert "First, who are you trying to reach?" not in _send(client, venture_id, "Design an experiment")["body_md"]


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_summary_confirmation_is_not_a_hidden_question_answer(client, idea, audience, summary):
    from app.idea.service import get_session, post_message
    from app.idea.runner import _known_fields

    venture_id, sid, question_id = _pending_questions(client, idea, audience, summary)
    before = get_session(sid)
    reply = _send(client, venture_id, "yes correct")
    assert "summary is saved, but not yet confirmed" in reply["body_md"]  # the idea itself is not confirmed here
    assert "I assumed" not in reply["body_md"]
    assert get_session(sid)["turns"] == before["turns"]
    post_message(sid, "yes correct", expected_question_id=question_id)
    assert get_session(sid)["turns"] == before["turns"]
    assert get_session(sid)["gstack_answers"] == []
    known = _known_fields({"original_idea": idea, "understanding": before["current_revision"]["structured"],
        "conversation": [{"role": "founder", "field": "customer", "text": "yes correct"}]})
    assert known["customer"] == audience.removeprefix("For ")
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.chat.store import append_message

    card = ConfirmationCard.for_action(get_action("build.start"), interpretation="Review a build request.",
        consequences=["Starts the requested build only after you choose its confirmation."], resulting_state={})
    append_message(venture_id, role="assistant", kind="confirmation", body_md="Review this separate request.", actions=[card.as_action()])
    reply = _send(client, venture_id, "yes correct")
    assert reply["kind"] == "confirmation" and "Nothing was started" in reply["body_md"]
    assert reply["actions"][0]["confirmation"]["action"] == "build.start"
    assert get_session(sid)["turns"] == before["turns"]


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_skip_validates_the_correction_question_before_mutation(client, idea, audience, summary):
    from app.idea.service import get_session, post_message

    _, session_id, question_id = _pending_questions(client, idea, audience, summary)
    before = get_session(session_id)
    for correction_id, error in (("missing_question", "unknown saved question"), (question_id, "question field mismatch")):
        with pytest.raises(ValueError, match=error):
            post_message(session_id, "Skip the questions", correction_question_id=correction_id)
        after = get_session(session_id)
        assert after["current_revision"] == before["current_revision"]
        assert after["state"] == before["state"] and after["turns"] == before["turns"]


@pytest.mark.parametrize("buttons", [False, True])
@pytest.mark.parametrize("before_worker", [False, True])
@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_skip_questions_finishes_with_buttons_or_plain_replies(client, buttons, before_worker, idea, audience, summary):
    from app.idea.service import get_session, post_message

    if before_worker:
        preview = client.post("/entry/chat/preview", json={"text": idea})
        committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
        assert committed.status_code == 200, committed.text
        venture_id = committed.json()["venture_id"]
        sid = client.get(f"/ventures/{venture_id}/idea/sessions").json()["items"][0]["id"]
    else:
        venture_id, sid, question_id = _pending_questions(client, idea, audience, summary)
    before = get_session(sid)["current_revision"]["id"]
    with pytest.raises(ValueError, match="unknown saved question"):
        post_message(sid, "Skip the questions", correction_question_id="missing_question")
    assert get_session(sid)["current_revision"]["id"] == before
    with pytest.raises(ValueError, match="message exceeds"):
        post_message(sid, "Skip " + "all " * 4000 + "questions")
    assert get_session(sid)["current_revision"]["id"] == before
    question = _send(client, venture_id, "Continue clarifying")
    if buttons:
        skipped = _click(client, venture_id, question, "chat.reply", text="Skip the questions")
        skipped = _send(client, venture_id, "What next?")
        confirmed = _click(client, venture_id, skipped, "idea.confirm")
    else:
        skipped = _send(client, venture_id, "Skip the questions")
        if not before_worker:
            original = get_session(sid)["current_revision"]
            changed = post_message(sid, "Actually, " + idea)
            assert not changed["session"]["current_revision"]["structured"].get("_questions_skipped")
            from app.db.supymem_models import IdeaRevision
            from app.state import db
            with db()() as session:
                assert json.loads(session.get(IdeaRevision, original["id"]).structured) == original["structured"]
            skipped = _send(client, venture_id, "Skip the questions")
        confirmed = _send(client, venture_id, "yes correct")
    assert "Nothing changed" not in skipped["body_md"]
    assert "I assumed" not in skipped["body_md"]
    assert get_session(sid)["state"] == "confirmed", confirmed
    assert get_session(sid)["current_revision"]["structured"]["offer"]
    assert get_session(sid)["current_revision"]["structured"]["customer"]
    assert all(not answer["answer"] for answer in get_session(sid)["gstack_answers"])
    assert not any(item.get("label") == "Continue clarifying" for item in skipped["actions"])
    next_step = _send(client, venture_id, "What next?")
    assert next_step["actions"], next_step
    if before_worker and buttons:
        from app.idea.service import start_direction_correction

        original = get_session(sid)["current_revision"]
        reopened = start_direction_correction(sid, "know", venture_id=venture_id, revision_id=original["id"])
        assert not reopened["current_revision"]["structured"].get("_questions_skipped")
        assert get_session(sid)["current_revision"] == original
        _send(client, venture_id, "Skip the questions")
        _send(client, venture_id, "yes correct")
        assert get_session(reopened["id"])["state"] == "confirmed"


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_founder_conversation_reaches_confirmation(client, monkeypatch, available, idea, audience, summary):
    from sqlalchemy import select
    from app.db.models import Job
    from app.state import db
    from app.core.local_mode import is_local
    from app.config import settings
    from app.idea.service import get_session
    from tests.idea_fixtures import completion, install_provider, questions_completion, validation_completion
    from tests.security.test_durable_journey_boundary import run_queued

    monkeypatch.setattr(settings, "new_user_flow", "off")
    calls = []
    if not available and is_local():
        from app.core.providers_local import clear_settings
        clear_settings()
    def provider(messages, kwargs):
        if "Prepare only the private market-test plan" in messages[0]["content"]:
            return private_plan_completion(messages)
        if kwargs.get("purpose") == "intake.classify":
            return completion()
        if "short GStack office-hours session" in messages[0]["content"]:
            return questions_completion()
        if kwargs.get("purpose") == "idea.validate":
            return validation_completion(messages)
        return completion(proposal=True, message="Check my understanding of your idea.",
                          understanding={"summary": summary, "offer": summary,
                                         "customer": audience.removeprefix("For ")})

    if available:
        calls = install_provider(monkeypatch, provider)
    preview = client.post("/entry/chat/preview", json={"text": idea})
    assert preview.status_code == 200, preview.text
    proposal = preview.json()["proposal"]
    assert not proposal.get("onboarding_question"), proposal
    committed = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert committed.status_code == 200, committed.text
    venture_id = committed.json()["venture_id"]
    run_queued("idea.clarify_turn")
    sid = client.get(f"/ventures/{venture_id}/idea/sessions").json()["items"][0]["id"]
    with db()() as session:
        jobs = session.scalars(select(Job).where(Job.venture_id == venture_id, Job.kind == "idea.clarify_turn")).all()
        assert all(job.status == "succeeded" for job in jobs), [(job.status, job.last_error) for job in jobs]
        if is_local() and not available:
            from app.db.supymem_models import IdeaClarificationRun
            run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.session_id == sid))
            assert run.state == "completed" and json.loads(run.usage) == {}
    if is_local() and not available:
        install_provider(monkeypatch, provider)
    answered_questions = []
    for _ in range(10):
        saved = get_session(sid)
        turn = saved["turns"][-1]
        from app.idea.validation import METHODS
        completed_methods = {run["method"] for run in saved["validation_runs"]
            if run["state"] == "complete" and run["revision_id"] == saved["current_revision"]["id"]}
        if turn["kind"] == "proposal" or is_local() and (saved["state"] == "reviewing" or set(METHODS) <= completed_methods):
            break
        assert turn["role"] == "agent" and turn["kind"] == "question"
        assert turn["text"] not in answered_questions
        assert (turn.get("options") or {}).get("field") != "customer"
        answered_questions.append(turn["text"])
        _send(client, venture_id, "Skip this question", question_id=turn["id"], reply_to_question=True)
        with db()() as session:
            pending_job = session.scalar(select(Job).where(Job.venture_id == venture_id,
                Job.workspace_id == saved["workspace_id"], Job.kind.in_(("idea.clarify_turn", "idea.validate")), Job.status == "scheduled"))
            pending = (pending_job.kind, pending_job.id) if pending_job else None
        if pending:
            run_queued(pending[0], through_job_id=pending[1])
    else:
        pytest.fail("Clarification never reached a proposal")
    review = _send(client, venture_id, "Skip the questions" if is_local() and turn["kind"] != "proposal" else "Continue clarifying")
    body = review["body_md"]
    assert "Skip this question" not in body
    labels = re.findall(r"\*\*([^:*]+):\*\*", body)
    assert len(labels) == len(set(labels))
    assert body.count(idea) == 1
    if available:
        assert summary in body
        assert any(kwargs.get("purpose") == "idea.clarify_turn" for _, kwargs in calls)
        assert any("concise paraphrases" in messages[0]["content"] for messages, kwargs in calls
                   if kwargs.get("purpose") == "idea.clarify_turn")
    assert not any(item.get("label") == "Continue clarifying" and item.get("style") == "primary"
                   for item in review["actions"])
    approval = _send(client, venture_id, "yes correct")
    assert "I assumed" not in approval["body_md"]
    with db()() as session:
        pending_job_id = session.scalar(select(Job.id).where(Job.venture_id == venture_id,
            Job.workspace_id == saved["workspace_id"], Job.kind == "idea.validate", Job.status == "scheduled"))
    if pending_job_id:
        run_queued("idea.validate", through_job_id=pending_job_id)
    resumed = _send(client, venture_id, "Continue clarifying")
    assert resumed["body_md"] != body
    if is_local() and get_session(sid)["state"] == "confirmed":
        assert "Your idea is saved" in resumed["body_md"]
    elif resumed["kind"] == "confirmation":
        assert any(item.get("action") == "idea.confirm" for item in resumed["actions"])
    else:
        assert resumed["kind"] == "idea_clarification_ready" and "Question" in resumed["body_md"]
    experiment = _send(client, venture_id, "Design an experiment")
    assert "First, who are you trying to reach?" not in experiment["body_md"]
    if is_local():
        assert "Prepared your experiment" in experiment["body_md"] and "Nothing is sent" in experiment["body_md"]
        assert get_session(sid)["state"] == "confirmed"
        assert not any(item.get("action") == "idea.confirm" for item in experiment["actions"])
    else:
        assert "confirm" in experiment["body_md"].lower() or "question" in experiment["body_md"].lower()
        skipped = _send(client, venture_id, "Skip the questions")
        assert "Nothing changed" not in skipped["body_md"]
        assert "I assumed" not in skipped["body_md"]
        assert any(item.get("action") == "idea.confirm" for item in skipped["actions"])
        confirmed = _send(client, venture_id, "yes correct")
        assert get_session(sid)["state"] == "confirmed", confirmed
        assert confirmed["actions"]
        assert all(item.get("label") != "Continue clarifying" for item in confirmed["actions"])
    finished = _send(client, venture_id, "Continue clarifying")
    assert "Your idea is saved" in finished["body_md"] and finished["body_md"].count(idea) == 1
    assert any(item.get("action") == "whats_next" for item in finished["actions"])
