import json

from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, run_queued, venture


def test_confirm_enqueues_then_worker_posts_result_once(client, monkeypatch):
    from app.chat import recovery
    from app.db.models import ChatMessage, Job
    from app.state import db

    vid, wid, sid = venture(client)
    calls = []

    async def guidance(*args):
        calls.append(args)
        return json.dumps({"choice": "diy", "intro": "Use your saved answers.", "steps": ["Write the problem."], "bring_back": "Your answer"})

    monkeypatch.setattr(recovery, "_llm", guidance)
    path = f"/ventures/{vid}/chat/confirm"
    body = {"action": "recovery.alternative", "payload": {"step": "idea.clarify_turn", "error_code": "clarification_outcome_uncertain"}, "idempotency_key": "alternative-click"}
    response = request(client, "POST", path, json=body)
    assert response.status_code == 200 and response.json()["kind"] == "recovery_pending"
    assert not calls
    replay = request(client, "POST", path, json=body)
    assert replay.status_code == 200 and replay.json()["id"] == response.json()["id"]
    with db()() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "chat.recovery", Job.venture_id == vid)).all()
        assert len(jobs) == 1 and jobs[0].max_attempts == 1
        job_id = jobs[0].id
    run_queued("chat.recovery", through_job_id=job_id)
    assert len(calls) == 1
    history = request(client, "GET", f"/ventures/{vid}/chat")
    assert history.status_code == 200
    with db()() as session:
        assert session.get(Job, job_id).status == "succeeded"
        result = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == vid, ChatMessage.body_md.contains("Use your saved answers.")))
        assert result is not None
    request(client, "POST", path, json=body)
    assert len(calls) == 1


def test_worker_loss_never_replays_uncertain_guidance(client, monkeypatch):
    from app.core.jobs import lease, reclaim_stale
    from app.db.models import Job
    from app.state import db
    from core.clock import utcnow
    from datetime import timedelta

    vid, wid, sid = venture(client)
    request(client, "POST", f"/ventures/{vid}/chat/confirm", json={"action": "recovery.alternative", "payload": {"step": "idea.clarify_turn"}, "idempotency_key": "lost-worker"})
    with db()() as session:
        claimed = lease(session, "lost-worker", kinds=["chat.recovery"], n=1)[0]
        job = session.get(Job, claimed.id)
        payload = json.loads(job.payload_json)
        payload["_runtime"] = {"operations": {"alternative": {"state": "calling"}}}
        job.payload_json = json.dumps(payload)
        job.locked_at = utcnow() - timedelta(minutes=3)
        session.commit()
        reclaim_stale(session)
        session.commit()
        assert job.status == "failed_dead"
        assert lease(session, "replacement-worker", kinds=["chat.recovery"], n=1) == []
