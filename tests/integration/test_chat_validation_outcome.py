"""A chat-started venture hears in chat how its paid idea.validate job ended."""

from __future__ import annotations

import json

from sqlalchemy import select

from tests.integration.test_chat_first_idea_bridge import _chat_started_venture, client  # noqa: F401


def _validate_job(venture_id):
    from app.core.jobs import enqueue
    from app.db.supymem_models import IdeaSession
    from app.state import db

    with db()() as session:
        idea = session.scalars(select(IdeaSession).where(IdeaSession.venture_id == venture_id)).first()
        jid = enqueue(session, "idea.validate", {"session_id": idea.id, "revision_id": idea.current_revision_id},
                      workspace_id=idea.workspace_id, venture_id=venture_id)
        session.commit()
        return jid, idea.id, idea.current_revision_id, idea.workspace_id


def _outcomes(venture_id):
    from app.db.models import ChatMessage
    from app.state import db

    with db()() as session:
        return session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.kind.like("idea_validation_%"))).all()


def _fail_terminally(jid, code="boom"):
    from app.core.jobs import FatalError, fail_job
    from app.db.models import Job
    from app.state import db

    with db()() as session:
        session.get(Job, jid).attempts = 1
        fail_job(session, jid, FatalError(code))
        session.commit()


def test_validation_success_posts_one_review_message(client):  # noqa: F811
    from app.db.supymem_models import ValidationRun
    from app.idea.validation import post_outcome_to_chat
    from app.state import db
    from core.clock import utcnow

    venture_id = _chat_started_venture(client)
    jid, sid, rid, wid = _validate_job(venture_id)
    with db()() as session:
        session.add(ValidationRun(id="vlr_t", session_id=sid, revision_id=rid, workspace_id=wid,
            venture_id=venture_id, method="gstack_ceo_review", state="complete", verdict="needs_evidence",
            report=json.dumps({"proposed_tests": [{"hypothesis": "Five people will try it on a real task"}],
                               "gaps": ["No one outside the founder has used it", "Cost is unknown"]}),
            created_at=utcnow(), completed_at=utcnow()))
        session.commit()
    for _ in range(2):  # a retry must not duplicate
        with db()() as session:
            post_outcome_to_chat(session, jid, sid, rid, ok=True)
            session.commit()
    (msg,) = _outcomes(venture_id)
    actions = json.loads(msg.actions_json)
    assert actions[0]["label"] == "Continue to research" and actions[0]["confirmation"]["action"] == "research.start"
    assert actions[1] == {"label": "Build my landing page now", "action": "idea.confirm", "style": "secondary",
                          "payload": {"session_id": sid, "revision_id": rid}}
    assert actions[2] == {"label": "Full report", "href": f"/v/{venture_id}/idea", "style": "secondary"}
    assert "Five people will try it" in msg.body_md and "Cost is unknown" in msg.body_md
    assert "not proof of demand, users or revenue" in msg.body_md


def test_validation_terminal_failure_posts_retry_message(client):  # noqa: F811
    venture_id = _chat_started_venture(client)
    jid, *_ = _validate_job(venture_id)
    _fail_terminally(jid, "validation_invalid_output")
    (msg,) = _outcomes(venture_id)
    assert msg.body_md.startswith("<!--employee:")  # Charlie speaks, not "Team"
    assert "Validation did not finish." in msg.body_md and "returned something I could not use" in msg.body_md
    assert "Retrying uses your saved answers." in msg.body_md and "validation_invalid_output" not in msg.body_md
    actions = json.loads(msg.actions_json)
    assert actions[0] == {"label": "Retry validation", "action": "job.retry",
                          "payload": {"kind": "idea.validate"}, "style": "primary"}
    (explain,) = [a for a in actions if a.get("action") == "blocker.explain"]
    assert explain["payload"] == {"code": "job_failed", "job_kind": "idea.validate",
                                  "job_code": "validation_invalid_output"}
    (alternative,) = [a for a in actions if a.get("action") == "recovery.alternative"]
    assert alternative["payload"] == {"step": "idea.validate", "kind": "idea_validation_failed",
                                      "job_id": jid, "error_code": "validation_invalid_output"}


def test_validation_outcome_posts_for_every_venture(client):  # noqa: F811
    from app.db.models import Venture, Workspace
    from app.state import db

    venture_id = _chat_started_venture(client)
    jid, *_ = _validate_job(venture_id)
    with db()() as session:
        workspace = session.get(Workspace, session.get(Venture, venture_id).workspace_id)
        workspace.settings_json = json.dumps(
            {**json.loads(workspace.settings_json), "chat_entry_defer_clarification": False})
        session.commit()
    _fail_terminally(jid)
    assert len(_outcomes(venture_id)) == 1
