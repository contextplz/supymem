"""A founder who skipped the questions and confirmed the idea reaches a real experiment."""
import pytest
from tests.company_os_harness import append_idea_revision

from tests.unit.chat.test_founder_intake_progress import client as client, bridge_client as bridge_client
from tests.unit.chat import test_idea_review
from tests.unit.chat.test_founder_intake_progress import DOMAINS, _click, _send

confirmation_saved = test_idea_review.confirmation_saved


@pytest.mark.parametrize("customer,problem", [
    ("commercial laundry operators", "Late linen collections leave your customers without stock"),
    ("people organizing weekend picnics", "You cannot coordinate who brings the shared supplies"),
])
@pytest.mark.parametrize("approved", [True, False])
def test_skipped_review_keeps_approval_and_continues_your_requested_test(confirmation_saved, monkeypatch, customer, problem, approved):
    import asyncio

    from app.chat import experiments, idea_proposal
    from app.chat.execute import execute_action
    from app.config import settings
    from app.credits import policy
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea import service
    from core.clock import utcnow
    from core.ids import new_id

    data = confirmation_saved
    from app.core.local_mode import is_local
    if is_local():
        from tests.idea_fixtures import install_provider
        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
        install_provider(monkeypatch)
    monkeypatch.setattr(settings, "new_user_flow", "on")
    policy.set_policy("feature_flags", {"experiments_autopilot_v1": True}, "test")
    with data["factory"]() as session:
        idea = IdeaSession(id=new_id("ids"), workspace_id=data["wid"], venture_id=data["vid"],
            case="know", state="reviewing", created_at=utcnow(), updated_at=utcnow())
        session.add(idea)
        session.flush()
        revision = service.append_revision(session, idea, {"customer": customer, "problem": problem}, source="founder")
        session.add(ClarifierTurn(id=new_id("ict"), session_id=idea.id, workspace_id=data["wid"],
            venture_id=data["vid"], turn=1, role="agent", kind="proposal", text="Review your saved summary.",
            revision_id=revision.id, created_at=utcnow()))
        session.commit()
        sid, rid = idea.id, revision.id
    if approved:
        idea_proposal.approve(data["vid"], {"session_id": sid, "revision_id": rid})
    else:
        from app.core.jobs import enqueue

        with data["factory"]() as session:
            enqueue(session, "idea.validate", {"session_id": sid, "revision_id": rid},
                workspace_id=data["wid"], venture_id=data["vid"], dedupe_key=idea_proposal.gstack_dedupe_key(sid, rid))
            session.commit()
    service.post_message(sid, "Skip the questions", actor_subject=data["actor"], reply_in_chat=True)
    planned = []

    async def plan(system, request, venture_id, **kwargs):
        planned.append(request)
        return {"hypothesis": f"You can reduce the problem for {customer}.",
            "action": "Interview five consenting adults about recent workarounds. Do not send anything.",
            "success_measure": "Pass if three describe costly workarounds; fail with fewer; missing interviews are inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", plan)
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda _: {
        "journey": {"icp": {"who": customer, "problem": problem}}, "confirmed_scope": {"customer": customer, "problem": problem}})
    request = {"text": f"Prepare an experiment for {customer}."}
    reply = asyncio.run(execute_action(data["vid"], "experiment.start", payload=request, actor_subject=data["actor"]))
    if not approved and not is_local():
        card = next(item["confirmation"] for item in reply["actions"] if item.get("confirmation"))
        assert card["action"] == "idea.confirm" and not planned
        reply = asyncio.run(execute_action(data["vid"], "idea.confirm", payload=card["resulting_state"], actor_subject=data["actor"]))
    assert len(planned) == 1 and request["text"] in planned[0]
    assert "Prepared your experiment" in reply["body_md"]
    assert not any(item.get("confirmation") for item in reply["actions"])
    saved = service.get_session(sid)
    assert saved["state"] == ("reviewing" if is_local() else "confirmed")
    if is_local():
        with data["factory"]() as session:
            assert session.get(IdeaSession, sid).confirmed_revision_id is None


def _flow_on(monkeypatch):
    from app.config import settings
    from app.credits.policy import set_policy

    monkeypatch.setattr(settings, "new_user_flow", "on")
    set_policy("feature_flags", {"experiments_autopilot_v1": True}, updated_by="test")


def _new_venture(client, idea):
    """A brand new venture from the chat entry, with its first question answered."""
    from app.core.local_mode import is_local
    from app.idea.service import get_session
    from tests.security.test_durable_journey_boundary import run_queued

    if is_local():
        from core.local_security import _proxy_secret
        client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
    preview = client.post("/entry/chat/preview", json={"text": idea})
    assert preview.status_code == 200, preview.text
    committed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]})
    assert committed.status_code == 200, committed.text
    venture_id = committed.json()["venture_id"]
    run_queued("idea.clarify_turn")
    sid = client.get(f"/ventures/{venture_id}/idea/sessions").json()["items"][0]["id"]
    turn = get_session(sid)["turns"][-1]
    assert turn["kind"] == "question", turn
    _send(client, venture_id, "I spoke with two people about this last week.",
          **({"question_id": turn["id"], "reply_to_question": True} if (turn.get("options") or {}).get("options") else {}))
    return venture_id, sid


def _skip_and_confirm(client, venture_id):
    question = _send(client, venture_id, "Continue clarifying")
    skipped = _click(client, venture_id, question, "chat.reply", text="Skip the questions")
    return _click(client, venture_id, skipped, "idea.confirm")


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_saved_idea_copy_gives_one_real_next_step_without_starting_work(client, monkeypatch, idea, audience, summary):
    from sqlalchemy import select

    from app.config import settings
    from app.db.models import Job
    from app.idea.service import get_session
    from app.org.characters import parse_speaker
    from app.state import db

    monkeypatch.setattr(settings, "new_user_flow", "on")
    venture_id, session_id = _new_venture(client, idea)
    with db()() as session:
        jobs_before = set(session.scalars(select(Job.id).where(Job.venture_id == venture_id)))
    skipped = _send(client, venture_id, "Skip the questions")
    saved = get_session(session_id)
    assert saved["current_revision"]["structured"]["_questions_skipped"] is True
    assert saved["state"] == "reviewing"
    assert saved["turns"][-1]["text"] == (
        "You've saved your answers, and the questions you skipped will stay blank. Review your idea when you're ready."
    )
    assert any(action.get("action") == "idea.confirm" for action in skipped["actions"])
    confirmed = _click(client, venture_id, skipped, "idea.confirm")
    assert get_session(session_id)["state"] == "confirmed"
    assert parse_speaker(confirmed["body_md"])[1] == "Your idea is saved. Ask me what to do next."
    assert [action["action"] for action in confirmed["actions"]] == ["whats_next"]
    reviewed = _send(client, venture_id, "Continue clarifying")
    assert parse_speaker(reviewed["body_md"])[1].startswith("Your idea is saved.")
    assert parse_speaker(reviewed["body_md"])[1].endswith("Ask me what to do next.")
    assert [action["action"] for action in reviewed["actions"]] == ["whats_next"]
    for reply in (confirmed, reviewed):
        assert not any(action.get("confirmation") for action in reply["actions"])
        assert not any(word in reply["body_md"].lower() for word in ("confirmed", "credits", "action", "—"))
    with db()() as session:
        assert set(session.scalars(select(Job.id).where(Job.venture_id == venture_id))) == jobs_before


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_skipped_questions_confirmation_moves_on_to_an_experiment(client, monkeypatch, idea, audience, summary):
    from app.core.local_mode import is_local
    from app.db.models import Venture
    from app.state import db
    from app.ventures.build_prerequisites import answers_pending
    from app.ventures.current_idea import intake_pending

    _flow_on(monkeypatch)
    venture_id, _ = _new_venture(client, idea)
    confirmed = _skip_and_confirm(client, venture_id)
    assert intake_pending(venture_id) is False
    with db()() as session:
        assert not answers_pending(session, session.get(Venture, venture_id))
    next_step = _click(client, venture_id, confirmed, "whats_next")
    offered = [item.get("action") for item in next_step["actions"]]
    assert "idea.review" not in offered
    if is_local():
        experiment = _send(client, venture_id, "Design an experiment")
    else:
        assert "experiment.start" in offered
        experiment = _click(client, venture_id, next_step, "experiment.start")
    assert "Prepared your experiment" in experiment["body_md"]
    assert "confirm your idea" not in experiment["body_md"].lower()
    assert "your idea is confirmed" not in experiment["body_md"].lower()
    assert experiment["actions"]
    assert not any(item.get("action") in {"whats_next", "idea.review", "experiment.start"} for item in experiment["actions"])


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_reopening_the_idea_after_skipping_needs_confirming_again(client, monkeypatch, idea, audience, summary):
    from app.idea.service import get_session, start_direction_correction
    from app.ventures.current_idea import intake_pending

    _flow_on(monkeypatch)
    venture_id, sid = _new_venture(client, idea)
    _skip_and_confirm(client, venture_id)
    start_direction_correction(sid, "know", venture_id=venture_id, revision_id=get_session(sid)["current_revision"]["id"])
    assert intake_pending(venture_id) is True
    blocked = _send(client, venture_id, "Design an experiment")
    assert "your idea is confirmed" not in blocked["body_md"].lower()


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_skipping_without_confirming_keeps_the_gate(client, monkeypatch, idea, audience, summary):
    from app.core.local_mode import is_local
    from app.ventures.current_idea import intake_pending

    _flow_on(monkeypatch)
    venture_id, _ = _new_venture(client, idea)
    _send(client, venture_id, "Skip the questions")
    assert intake_pending(venture_id) is True
    blocked = _send(client, venture_id, "Design an experiment")
    if is_local():
        assert "Prepared your experiment" in blocked["body_md"]
        assert not any(item.get("confirmation") or item.get("action") == "idea.confirm" for item in blocked["actions"])
        assert intake_pending(venture_id) is True
    else:
        assert "confirm your idea" in blocked["body_md"].lower()
        assert any(item.get("action") == "idea.confirm" for item in blocked["actions"])


@pytest.mark.parametrize("idea,audience,summary", DOMAINS)
def test_confirmed_with_unfinished_review_prepares_the_requested_experiment(client, monkeypatch, idea, audience, summary):
    """A confirmed idea lets you request an experiment without finishing the saved answers."""
    import json

    from app.chat import experiments
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db

    planned = []

    async def plan(system, request, venture_id, **kwargs):
        planned.append(request)
        return {"hypothesis": f"{audience} need {summary}.",
                "action": "Prepare your interview script for five consenting people. Nothing is sent.",
                "success_measure": "Pass with three recent costly workarounds; missing interviews are inconclusive."}

    monkeypatch.setattr(experiments, "_llm_json", plan)
    _flow_on(monkeypatch)
    venture_id, sid = _new_venture(client, idea)
    _skip_and_confirm(client, venture_id)
    with db()() as session:
        revision = session.get(IdeaRevision, session.get(IdeaSession, sid).current_revision_id)
        fields = json.loads(revision.structured)
        fields.pop("_questions_skipped")
        append_idea_revision(session, revision.id, structured=json.dumps(fields))
        session.commit()
    next_step = _send(client, venture_id, "What next?")
    assert not any(item.get("action") == "idea.review" for item in next_step["actions"])
    assert not planned
    experiment = _send(client, venture_id, "Design an experiment")
    assert "Prepared your experiment" in experiment["body_md"]
    assert not any(action.get("confirmation") for action in experiment["actions"])
    assert "confirm your idea" not in experiment["body_md"].lower()
    assert len(planned) == 1 and "Design an experiment" in planned[0]
