import json

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, run_queued, venture
from tests.idea_fixtures import install_provider


def test_uncertain_call_never_replays_but_founder_can_start_fresh(client, monkeypatch):
    from app.core.jobs import FatalError
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import runner, service
    from app.state import db

    def timeout(*args):
        raise TimeoutError("provider deadline")

    calls = install_provider(monkeypatch, callback=timeout)
    vid, wid, sid = venture(client)
    old_id = run_queued("idea.clarify_turn")
    with db()() as session:
        old = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == old_id))
        assert old.state == "uncertain"
        old_run_id, old_usage = old.id, old.usage
        old_hold = json.loads(old_usage)["reservation_id"]
        job = session.get(Job, old_id)
        payload = json.loads(job.payload_json) | {"_job_id": job.id, "_worker_id": "test-replay", "_job_attempt": 2}
        job.status, job.locked_by, job.attempts = "leased", "test-replay", 2
        session.commit()
    with pytest.raises(FatalError, match="clarification_outcome_uncertain"):
        runner._claim(payload)
    with db()() as session:
        session.get(Job, old_id).status = "failed_dead"
        session.commit()
    with pytest.raises(RuntimeError, match="clarification_outcome_uncertain"):
        service.retry_failed_clarification(sid, venture_id=vid)
    from app.chat.execute import execute_action
    import asyncio
    autonomous = asyncio.run(execute_action(vid, "job.retry", payload={"kind": "idea.clarify_turn", "session_id": sid, "run": True}, actor_subject="usr_alice"))
    assert "retry" in autonomous["body_md"].lower() and "saved idea" in autonomous["body_md"].lower()
    with db()() as session:
        assert len(session.scalars(select(Job).where(Job.workspace_id == wid, Job.venture_id == vid,
            Job.kind == "idea.clarify_turn")).all()) == 1
        assert session.get(CreditReservation, old_hold).status == "reconciling"
    assert len(calls) == 1
    fresh_calls = install_provider(monkeypatch)
    path = f"/ventures/{vid}/chat/confirm"
    prepared = request(client, "POST", path, json={"action": "job.retry", "payload": {"kind": "idea.clarify_turn"}})
    assert prepared.status_code == 200
    action = prepared.json()["actions"][0]
    assert action["label"] == "Try again"
    approved = request(client, "POST", path, json={"action": action["action"], "payload": action["confirmation"]["resulting_state"], "idempotency_key": "founder-retry"})
    assert approved.status_code == 200
    with db()() as session:
        fresh = session.scalar(select(Job).where(Job.venture_id == vid, Job.id != old_id, Job.kind == "idea.clarify_turn"))
        fresh_id = fresh.id
        assert fresh.idempotency_key != session.get(Job, old_id).idempotency_key
    run_queued("idea.clarify_turn", through_job_id=fresh_id)
    assert len(calls) == len(fresh_calls) == 1
    with db()() as session:
        old = session.get(IdeaClarificationRun, old_run_id)
        fresh = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == fresh_id))
        assert old.state == "uncertain" and old.usage == old_usage
        assert session.get(CreditReservation, old_hold).status == "reconciling"
        assert fresh.state == "completed" and json.loads(fresh.usage)["reservation_id"] != old_hold


def test_explicit_retry_cannot_bypass_live_call(client, monkeypatch):
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import service
    from app.state import db

    def timeout(*args):
        raise TimeoutError("provider deadline")

    install_provider(monkeypatch, callback=timeout)
    vid, wid, sid = venture(client)
    old_id = run_queued("idea.clarify_turn")
    with db()() as session:
        session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == old_id)).state = "calling"
        session.commit()
    with pytest.raises(RuntimeError, match="clarification_outcome_uncertain"):
        service.retry_failed_clarification(sid, venture_id=vid, allow_uncertain=True)
