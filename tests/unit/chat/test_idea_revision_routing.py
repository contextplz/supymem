import asyncio
import json
from unittest.mock import Mock

import pytest

from tests.company_os_harness import append_idea_revision, load_company_os_app
from tests.idea_fixtures import completion, install_provider
from tests.unit.chat.test_round3_matrix import venture  # noqa: F401

load_company_os_app()

CORRECTION = (
    "That's not right. I don't have any customers yet, it's just an idea. "
    "I want a marketplace app where home cooks in Bangalore apartment complexes "
    "sell weekend meal boxes to their neighbours, not a payment tool."
)


@pytest.mark.parametrize("venture", ["Fleet inspection scheduling", "Amateur astronomy outing plans"], indirect=True)
def test_local_saved_chat_reply_does_not_queue_an_unrequested_idea_refresh(venture, monkeypatch):
    from app.chat.store import append_message, list_messages
    from app.db.models import Job
    from app.state import db
    from app.ventures import idea_page

    _, venture_id, _ = venture
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    refresh = Mock(side_effect=AssertionError("A saved reply must not start an unrelated idea refresh"))
    monkeypatch.setattr(idea_page, "request_refresh", refresh)
    with db()() as session:
        before = {job.id for job in session.query(Job).filter_by(venture_id=venture_id)}
    reply = append_message(venture_id, role="assistant", body_md="Your saved work is ready. Ask to review it.", preserve_exact=True)
    refresh.assert_not_called()
    assert reply in list_messages(venture_id)
    with db()() as session:
        assert {job.id for job in session.query(Job).filter_by(venture_id=venture_id)} == before


@pytest.mark.parametrize("audience,offer", [
    ("regional transport operations managers", "Reconcile depot delivery records and retain every receipt."),
    ("families planning accessible holidays", "Plan step-free walking routes and retain each accessibility restriction."),
])
def test_release_compound_skip_updates_the_saved_question_before_summary(venture, audience, offer):
    from app.idea.service import get_session
    from app.state import db

    client, venture_id, _ = venture
    session_id, revision_id = awaiting_idea(venture, state="clarifying")
    with db()() as session:
        append_idea_revision(session, revision_id, structured=json.dumps({"customer": audience, "offer": offer}))
        session.commit()
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Skip the remaining questions and show my summary."})
    assert response.status_code == 200
    saved = get_session(session_id)
    assert saved["state"] == "reviewing" and saved["current_revision"]["structured"]["_questions_skipped"]
    assert saved["turns"][-1]["kind"] == "proposal"
    assert audience in response.json()["body_md"] and offer in response.json()["body_md"]
    assert "Nothing changed" not in response.json()["body_md"]


@pytest.mark.parametrize("audience,offer", [
    ("regional transport operations managers", "Reconcile delivery records, preserve each source receipt and leave every dispatch decision with you."),
    ("families planning accessible holidays", "Plan step-free walking routes, preserve each accessibility restriction and leave every booking decision with you."),
])
def test_release_review_reads_full_saved_idea_instead_of_generic_questions(venture, audience, offer):
    from app.db.models import Job
    from app.state import db

    client, venture_id, _ = venture
    _, revision_id = awaiting_idea(venture, state="confirmed")
    with db()() as session:
        append_idea_revision(session, revision_id, summary=f"You help {audience}. {offer}",
            structured=json.dumps({"customer": audience, "offer": offer}))
        session.commit()
        jobs_before = session.query(Job).count()
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Review my idea."})
    assert response.status_code == 200
    assert audience in response.json()["body_md"] and offer in response.json()["body_md"]
    assert "Six questions" not in response.json()["body_md"] and response.json()["actions"]
    with db()() as session:
        assert session.query(Job).count() == jobs_before


def awaiting_idea(venture, *, state="reviewing", chat_entry=False):
    from app.db.models import Workspace
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import append_revision
    from app.state import db
    from core.clock import utcnow

    _, venture_id, workspace_id = venture
    with db()() as session:
        if chat_entry:
            workspace = session.get(Workspace, workspace_id)
            workspace.settings_json = json.dumps({"chat_entry_defer_clarification": True,
                                                  "entry_goal": "startup"})
        idea = IdeaSession(id="ids_review", workspace_id=workspace_id, venture_id=venture_id,
                           case="know", state=state, created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = append_revision(session, idea, {"summary": "A payment tool with customers",
                                                   "customer": "Existing paying customers"}, source="agent")
        session.add(ClarifierTurn(id="ict_review", session_id=idea.id, workspace_id=workspace_id,
                                 venture_id=venture_id, turn=1, role="agent",
                                 kind="proposal" if state == "reviewing" else "question",
                                 text="Here is what I took from your answers. Tell me if I got anything wrong."
                                      if state == "reviewing" else "Who is this idea for?",
                                 revision_id=revision.id, created_at=utcnow()))
        session.commit()
        return idea.id, revision.id


@pytest.mark.parametrize("audience,offer", [
    ("warehouse managers", "Track incoming deliveries and retain every receipt."),
    ("parents managing food allergies", "Plan safe dinners and create a shopping list."),
])
def test_own_idea_summary_is_read_not_other_venture_navigation(venture, audience, offer):
    from app.state import db

    client, venture_id, _ = venture
    _, revision_id = awaiting_idea(venture)
    with db()() as session:
        append_idea_revision(session, revision_id, structured=json.dumps({"customer": audience, "offer": offer}))
        session.commit()
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Please show me the summary of my idea."})
    assert response.status_code == 200
    reply = response.json()
    assert audience in reply["body_md"] and offer in reply["body_md"], reply
    assert "Switch ventures" not in reply["body_md"]
    from app.chat.idea_proposal import requested_saved_review
    assert not requested_saved_review(venture_id, "Show me the summary of another venture's idea.")


@pytest.mark.parametrize("audience,offer", [
    ("warehouse managers", "Track incoming deliveries and retain every receipt."),
    ("parents managing food allergies", "Plan safe dinners and create a shopping list."),
])
def test_review_after_skip_reads_saved_proposal_without_audit(venture, monkeypatch, audience, offer):
    from app.chat import router
    from app.db.models import Job
    from app.state import db

    client, venture_id, _ = venture
    _, revision_id = awaiting_idea(venture)
    with db()() as session:
        append_idea_revision(session, revision_id,
            structured=json.dumps({"customer": audience, "offer": offer, "_questions_skipped": True}))
        session.commit()
        jobs_before = session.query(Job).count()
    monkeypatch.setattr(router, "split_speech_acts", lambda text: [("ceo_review", text)])
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": "Review my idea."})
    assert response.status_code == 200
    reply = response.json()
    assert audience in reply["body_md"] and offer in reply["body_md"], reply
    assert "Demand read:" not in reply["body_md"] and "Stage:" not in reply["body_md"]
    from app.chat.idea_proposal import requested_saved_review
    assert not requested_saved_review(venture_id, "Critique my idea.")
    assert not requested_saved_review(venture_id, "Review my idea and search for customers.")
    with db()() as session:
        assert session.query(Job).count() == jobs_before


@pytest.mark.parametrize("audience,offer", [
    ("independent pharmacy inventory planners", "Compare delivery manifests and flag missing stock."),
    ("amateur birdwatchers", "Plan accessible birdwatching routes and save sightings."),
])
def test_preconfirmation_review_reads_current_saved_idea(venture, audience, offer):
    from app.chat.idea_proposal import requested_saved_review
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.state import db

    client, venture_id, _ = venture
    idea_id, revision_id = awaiting_idea(venture)
    with db()() as session:
        append_idea_revision(session, revision_id, structured=json.dumps({"customer": audience, "offer": offer}))
        session.commit()
        jobs_before = session.query(Job).count()
    for request in ("Please review the idea before I confirm it.",
                    "Please review the idea so I can confirm it.",
                    "Could you review my idea before we approve it?"):
        assert requested_saved_review(venture_id, request)
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": request})
        assert response.status_code == 200
        body = response.json()["body_md"]
        assert audience in body and offer in body
        assert "Demand read:" not in body and "Stage:" not in body
    for request in ("Review another venture's idea before I confirm it.",
                    "Review my idea and search for customers."):
        assert not requested_saved_review(venture_id, request)
    with db()() as session:
        assert session.query(Job).count() == jobs_before
        assert not session.get(IdeaSession, idea_id).confirmed_revision_id


@pytest.mark.parametrize("audience,offer", [
    ("commercial laundry shift supervisors", "Schedule linen pickups and save each handover record."),
    ("people growing herbs on balconies", "Plan watering reminders and save plant observations."),
])
def test_saved_idea_review_keeps_revision_bound_confirm_and_edit(request, audience, offer):
    from app.chat.idea_proposal import requested_saved_review
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.idea.service import append_revision
    from app.state import db

    venture_data = request.getfixturevalue("venture")
    client, venture_id, _ = venture_data
    idea_id, old_revision_id = awaiting_idea(venture_data)
    with db()() as session:
        idea = session.get(IdeaSession, idea_id)
        old_revision = session.get(IdeaRevision, old_revision_id)
        old_content = (old_revision.summary, old_revision.structured)
        revision = append_revision(session, idea, {"summary": f"You help {audience}.", "customer": audience,
            "offer": offer, "_questions_skipped": True}, source="founder")
        revision_id = revision.id
        session.get(ClarifierTurn, "ict_review").revision_id = revision_id
        session.commit()
        jobs_before = session.query(Job).count()
        revisions_before = session.query(IdeaRevision).count()
    for message in ("Could you review the saved idea before I confirm it?", "Please review our saved idea."):
        assert requested_saved_review(venture_id, message)
        response = client.post(f"/ventures/{venture_id}/chat", json={"message": message})
        assert response.status_code == 200, response.text
        reply = response.json()
        assert audience in reply["body_md"] and offer in reply["body_md"]
        assert "Six questions" not in reply["body_md"] and "Demand read:" not in reply["body_md"]
        actions = {action["action"]: action for action in reply["actions"]}
        assert {"idea.confirm", "idea.proposal.edit"} <= actions.keys()
        for action_name in ("idea.confirm", "idea.proposal.edit"):
            action = actions[action_name]
            payload = action.get("payload") or action["confirmation"]["resulting_state"]
            assert payload["session_id"] == idea_id
            assert payload["revision_id"] == revision_id
    for message in ("Do not review my saved idea.", "Review another venture's saved idea.",
                    "Review my saved idea and find customers."):
        assert not requested_saved_review(venture_id, message)
    assert not requested_saved_review("missing_venture", "Please review my saved idea.")
    with db()() as session:
        assert session.query(Job).count() == jobs_before
        assert session.query(IdeaRevision).count() == revisions_before
        assert not session.get(IdeaSession, idea_id).confirmed_revision_id
        old_revision = session.get(IdeaRevision, old_revision_id)
        assert (old_revision.summary, old_revision.structured) == old_content


@pytest.mark.parametrize("chat_entry,correction,buyer,quote", [
    (False, CORRECTION, "their neighbours", "sell weekend meal boxes to their neighbours"),
    (True, "That's not right. I don't have customers yet. I want a marketplace where equipment cooperatives sell used centrifuges to independent lab managers.",
     "independent lab managers", "sell used centrifuges to independent lab managers"),
])
def test_review_correction_uses_edit_and_posts_revised_summary(venture, monkeypatch, chat_entry, correction, buyer, quote):
    from app.chat import router
    from app.chat.idea_proposal import EDIT_QUESTION, approve
    from app.db.models import ChatMessage, Job
    from app.idea import service
    from app.state import db
    from sqlalchemy import select
    from tests.security.test_durable_journey_boundary import run_queued

    client, venture_id, _ = venture
    session_id, old_revision_id = awaiting_idea(venture, chat_entry=chat_entry)
    post = Mock(wraps=service.post_message)
    monkeypatch.setattr(service, "post_message", post)
    monkeypatch.setattr(router, "split_speech_acts", lambda text: [("plan_revision", text)])

    def revised(messages, kwargs):
        snapshot = json.loads(messages[-1]["content"])
        assert snapshot["conversation"][-1]["text"] == correction
        assert snapshot["force_propose"] is True
        return completion(proposal=True, understanding={
            "summary": f"A marketplace to {quote}. No customers yet.",
            "customer": buyer,
            "customer_extraction": {"text": buyer, "kind": "person", "quote": quote},
            "offer": f"A marketplace to {quote}.",
        })

    provider_calls = install_provider(monkeypatch, revised)
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": correction})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "idea_clarification_pending"
    assert "Change or regenerate this plan" not in response.json()["body_md"]
    assert post.call_args.args == (session_id, correction)
    state = service.get_session(session_id)
    assert state["turns"][-2]["text"] == EDIT_QUESTION
    assert state["turns"][-1]["text"] == correction
    assert state["current_revision"]["id"] != old_revision_id
    assert "replaced by a newer version" in approve(venture_id, {
        "session_id": session_id, "revision_id": old_revision_id})["body_md"]

    run_queued("idea.clarify_turn")
    calls_for_correction = [kwargs for messages, kwargs in provider_calls if kwargs.get("purpose") == "idea.clarify_turn"
                            and (json.loads(messages[-1]["content"]).get("conversation") or [{}])[-1].get("text") == correction]
    assert len(calls_for_correction) == 1
    state = service.get_session(session_id)
    assert state["state"] == "reviewing" and state["turns"][-1]["kind"] == "proposal"
    assert state["current_revision"]["structured"]["customer"] == buyer
    assert "No customers yet" in state["current_revision"]["structured"]["summary"]
    with db()() as session:
        summary = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                                  ChatMessage.kind == "idea_clarification_ready")
                                 .order_by(ChatMessage.created_at.desc()).limit(1))
        assert "marketplace" in summary.body_md and "No customers yet" in summary.body_md
        assert session.scalar(select(Job.id).where(Job.venture_id == venture_id,
                              Job.kind == "idea.validate")) is None


@pytest.mark.parametrize("intent", ["plan_revision", "plan_approve", "plan_generate", "plan_quote", "work_start"])
def test_plan_intent_without_plan_revises_awaiting_idea(venture, monkeypatch, intent):
    from app.chat import router
    from app.db.models import ChatMessage
    from app.idea.service import get_session
    from app.state import db
    from sqlalchemy import select

    client, venture_id, _ = venture
    session_id, _ = awaiting_idea(venture)
    text = "Change the plan to a weekend meal box marketplace"
    monkeypatch.setattr(router, "split_speech_acts", lambda text: [(intent, text)])
    response = client.post(f"/ventures/{venture_id}/chat", json={"message": text})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["kind"] == "idea_clarification_pending"
    assert "Change or regenerate this plan" not in reply["body_md"]
    assert get_session(session_id)["turns"][-1]["text"] == text
    with db()() as session:
        saved = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                                ChatMessage.role == "user", ChatMessage.body_md == text)).all()
        assert len(saved) == 1
        replies = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                                  ChatMessage.role == "assistant",
                                  ChatMessage.kind == "idea_clarification_pending")).all()
        assert len(replies) == 1


@pytest.mark.parametrize("text", [CORRECTION, "That's not right", "I want a marketplace",
                                 "It's just an idea", "I don't have customers", "Not a payment tool"])
def test_clarifying_page_idea_accepts_correction(venture, text):
    from app.chat.router import handle_message
    from app.idea.service import get_session

    _, venture_id, _ = venture
    session_id, _ = awaiting_idea(venture, state="clarifying")
    asyncio.run(handle_message(venture_id, "Continue clarifying"))  # the question is on screen before it can be corrected
    reply = asyncio.run(handle_message(venture_id, text))
    assert reply["kind"] == "idea_clarification_pending"
    assert get_session(session_id)["turns"][-1]["text"] == text


def test_plan_revision_without_plan_or_pending_idea_uses_general(venture, monkeypatch):
    from app.chat.context import assemble
    from app.chat.router import _dispatch_intent, general

    _, venture_id, _ = venture
    calls = []

    async def reply(ctx, text, speaker, **kwargs):
        calls.append(text)
        return {"kind": "text", "body_md": "Tell me more about your idea.", "actions": [], "citations": []}

    monkeypatch.setattr(general, "handle", reply)
    result = asyncio.run(_dispatch_intent(venture_id, assemble(venture_id), "plan_revision", CORRECTION, None))
    assert calls == [CORRECTION]
    assert "Change or regenerate this plan" not in result["body_md"]
