"""Every journey milestone posts exactly one plain chat message with one next step."""

from __future__ import annotations

import json
import re

from sqlalchemy import select

from tests.integration.test_chat_first_idea_bridge import _chat_started_venture, client  # noqa: F401

_LEAK = re.compile(r"\b[a-z]{2,6}_[0-9a-z]{12,}\b|/Users/|/tmp/|localhost|127\.0\.0\.1")


def _rows(venture_id, kind):
    from app.db.models import ChatMessage
    from app.state import db

    with db()() as session:
        return session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.kind == kind)).all()


def _once(venture_id, kind, post):
    post()
    post()  # a retry must not duplicate
    (msg,) = _rows(venture_id, kind)
    assert not _LEAK.search(msg.body_md), msg.body_md
    return msg, json.loads(msg.actions_json)


def test_research_done(client, monkeypatch):  # noqa: F811
    from app.chat import milestone_hooks as h
    from app.research import service
    from app.research.verdict import research_verdict

    vid = _chat_started_venture(client)
    findings = {"source": [{"id": str(i), "category": "source", "evidence_kind": "search_excerpt"} for i in range(8)]}
    monkeypatch.setattr(service, "get_dossier", lambda did: {"summary": {
        "verdict": research_verdict("partial", findings, {}, None)}})
    msg, actions = _once(vid, "research_ready", lambda: h.research_done({"_job_id": "j1", "venture_id": vid}))
    assert "Research is ready." in msg.body_md and msg.body_md.count("\n- ") <= 3
    assert actions[0]["label"] == "Read full report" and actions[1]["action"] == "plan.generate"


def test_plan_done(client, monkeypatch):  # noqa: F811
    from app.chat import milestone_hooks as h
    from app.planning import compiler

    vid = _chat_started_venture(client)
    monkeypatch.setattr(compiler, "get_plan", lambda pid: {
        "proposal": {"milestones": [{}, {}]}, "estimate": {"total_upfront_credits": 40}})
    msg, actions = _once(vid, "plan_ready", lambda: h.plan_done({"_job_id": "j2", "venture_id": vid}))
    assert "2 milestones" in msg.body_md and "about 40.00 credits" in msg.body_md
    assert "review the plan before approving its execution" in msg.body_md
    assert actions[0] == {"label": "Review plan", "href": f"/v/{vid}/plan", "style": "primary"}


def test_plan_done_posts_even_when_details_fail(client, monkeypatch):  # noqa: F811
    from app.chat import milestone_hooks as h
    from app.planning import compiler

    vid = _chat_started_venture(client)
    monkeypatch.setattr(compiler, "get_plan", lambda pid: (_ for _ in ()).throw(RuntimeError("boom")))
    msg, actions = _once(vid, "plan_ready", lambda: h.plan_done({"_job_id": "j3", "venture_id": vid}))
    assert "Your plan is ready" in msg.body_md and actions[0]["label"] == "Review plan"


def test_first_look_ready(client):  # noqa: F811
    from app.chat import milestone_hooks as h

    vid = _chat_started_venture(client)
    msg, actions = _once(vid, "first_look_ready", lambda: h.first_look_ready(vid, "sv1"))
    assert [a["label"] for a in actions] == ["Open waitlist", "Rewrite", "Open preview"]
    assert actions[0]["confirmation"]["action"] == "waitlist.enable"
    assert actions[2] == {"label": "Open preview", "href": f"/v/{vid}/site", "style": "secondary"}


def test_research_and_plan_failure_post_retry(client):  # noqa: F811
    from app.chat import milestone_hooks as h
    from app.state import db

    vid = _chat_started_venture(client)
    cases = (("research.deep", "research", "Retry research", "research_invalid_output"),
             ("plan.compile", "plan", "Retry planning", "plan_inputs_changed"))
    for kind, path, label, code in cases:
        def post():
            with db()() as session:
                h.failed(session, vid, kind, "job_x", code)
                session.commit()
        msg, actions = _once(vid, f"{path}_failed", post)
        assert code not in msg.body_md and msg.body_md.startswith("<!--employee:")  # plain copy, spoken by Charlie
        assert actions[0] == {"label": label, "action": "job.retry", "payload": {"kind": kind}, "style": "primary"}
        (explain,) = [a for a in actions if a.get("action") == "blocker.explain"]
        assert explain["payload"] == {"code": "job_failed", "job_kind": kind, "job_code": code}
        (alternative,) = [a for a in actions if a.get("action") == "recovery.alternative"]
        assert alternative["payload"] == {"step": kind, "kind": f"{path}_failed", "job_id": "job_x", "error_code": code}


def test_task_completion_posts_once_and_scrubs(client):  # noqa: F811
    from app.planning.execute import _chat

    vid = _chat_started_venture(client)
    msg, actions = _once(vid, "task_run", lambda: _chat(
        vid, "tsk_1", "Finished **X**. Saved at /Users/me/repo/note.md via http://localhost:8000/x",
        None, actions=[{"label": "Open work", "href": f"/v/{vid}/tasks", "style": "secondary"}], once=True))
    assert actions[0]["label"] == "Open work"


def test_fatal_plan_compile_job_failure_fires_the_retry_milestone(client):  # noqa: F811
    from app.core.jobs import FatalError, enqueue, fail_job, lease
    from app.db.models import Venture
    from app.state import db

    vid = _chat_started_venture(client)
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
        enqueue(session, "plan.compile", {"workspace_id": wid, "venture_id": vid}, workspace_id=wid, venture_id=vid)
        session.commit()
    with db()() as session:
        (job,) = lease(session, "w1", kinds=["plan.compile"], n=1)
        session.commit()
        fail_job(session, job.id, FatalError("plan_invalid_output"), worker_id="w1", attempt=job.attempts)
        session.commit()
    (msg,) = _rows(vid, "plan_failed")
    assert "Planning did not finish" in msg.body_md
