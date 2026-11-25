"""Stuck paid journey work terminates without replaying the provider action."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.core.jobs import complete_job, enqueue, expire_overdue_paid_journey, heartbeat, lease, reclaim_stale  # noqa: E402
from app.db.models import Base, Job  # noqa: E402
from app.db.session import make_engine  # noqa: E402
from core.clock import utcnow  # noqa: E402


def _factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session)


def _lose_worker(factory, job_id, operations):
    from app.db.supymem_models import ResearchDossier
    from app.research.execution import identifier
    engine = factory.kw["bind"]  # dossier parents are irrelevant here
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=OFF"))
    engine.dispose()
    with factory() as session:
        assert len(lease(session, "old-worker", kinds=["idea.validate"])) == 1
        session.get(Job, job_id).locked_at = utcnow() - timedelta(seconds=120)
        session.add(ResearchDossier(id=identifier(job_id), workspace_id="w", venture_id="v", revision_id="r",
            kind="validation", state="running", budget_calls=3, calls_used=1, created_at=utcnow(),
            summary=json.dumps({"_runtime": {"operations": operations}})))
        session.commit()


def test_lost_paid_worker_without_call_in_flight_is_retried(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        job_id = enqueue(session, "idea.validate", {"revision_id": "rev_1"})
        session.commit()
    _lose_worker(factory, job_id, {"review": {"state": "completed"}})  # receipt recorded: safe to resume
    with factory() as session:
        assert reclaim_stale(session) == 1
        session.commit()
        assert session.get(Job, job_id).status == "retry_wait"
        assert len(lease(session, "new-worker", kinds=["idea.validate"])) == 1


def test_lost_paid_worker_with_call_in_flight_is_terminal_without_second_lease(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        job_id = enqueue(session, "idea.validate", {"revision_id": "rev_1"})
        session.commit()
    _lose_worker(factory, job_id, {"review": {"state": "calling"}})
    with factory() as session:
        assert reclaim_stale(session) == 1
        assert lease(session, "new-worker", kinds=["idea.validate"]) == []
        session.commit()
        row = session.get(Job, job_id)
        assert (row.status, row.last_error, row.attempts) == ("failed_dead", "job_outcome_uncertain", 1)


@pytest.mark.parametrize("kind", ["idea.clarify_turn"])
def test_live_heartbeat_cannot_keep_overdue_paid_job_running_forever(tmp_path, kind):
    factory = _factory(tmp_path)
    with factory() as session:
        job_id = enqueue(session, kind, {})
        session.commit()
        claimed = lease(session, "worker", kinds=[kind])[0]
        row = session.get(Job, job_id)
        row.started_at = utcnow() - timedelta(seconds=row.timeout_s + 1)
        session.commit()
    with factory() as session:
        assert expire_overdue_paid_journey(session) == 1
        heartbeat(session, job_id, worker_id="worker")
        complete_job(session, job_id, worker_id="worker", attempt=claimed.attempts)
        session.commit()
        row = session.get(Job, job_id)
        assert (row.status, row.last_error, row.attempts) == ("failed_dead", "job_outcome_uncertain", 1)
        assert lease(session, "other-worker", kinds=[kind]) == []


def test_fresh_paid_job_and_other_kinds_keep_existing_lease_behavior(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        paid_id = enqueue(session, "idea.validate", {})
        other_id = enqueue(session, "ops.signal_sweep", {})
        session.commit()
        lease(session, "worker", kinds=["idea.validate", "ops.signal_sweep"], n=2)
        session.get(Job, other_id).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with factory() as session:
        assert expire_overdue_paid_journey(session) == 0
        assert reclaim_stale(session) == 1
        session.commit()
        assert session.get(Job, paid_id).status == "leased"
        assert session.get(Job, other_id).status == "retry_wait"


def test_clarification_at_180_seconds_keeps_live_lease_but_lost_call_never_releases(tmp_path):
    from app.core.jobs import heartbeat
    from app.db.models import Venture
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    factory = _factory(tmp_path)
    now = utcnow()
    with factory() as session:
        job_id = enqueue(session, "idea.clarify_turn", {})
        session.commit()
        now = utcnow()
        claimed = lease(session, "worker", kinds=["idea.clarify_turn"], now=now)[0]
        session.add(Venture(id="v", workspace_id="w", name="Test", raw_idea="Test", stage="idea", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaSession(id="s", workspace_id="w", venture_id="v", case="know", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="r", session_id="s", workspace_id="w", venture_id="v", rev=1, source="founder", created_at=now))
        session.flush()
        session.add(IdeaClarificationRun(id="clarify-long", job_id=job_id, session_id="s",
            workspace_id="w", venture_id="v", revision_id="r", state="calling",
            attempt=claimed.attempts, worker_id=claimed.worker_id, snapshot="{}", usage="{}",
            created_at=now, updated_at=now))
        session.commit()
        later = now + timedelta(seconds=180)
        heartbeat(session, job_id, worker_id=claimed.worker_id, attempt=claimed.attempts, now=later)
        session.commit()
        assert expire_overdue_paid_journey(session, now=later) == 0
        assert reclaim_stale(session, now=later) == 0
        assert lease(session, "other-worker", kinds=["idea.clarify_turn"], now=later) == []
        assert session.get(Job, job_id).timeout_s == 900
        session.commit()
    with factory() as session:
        assert reclaim_stale(session, now=later + timedelta(seconds=91)) == 1
        session.commit()
    with factory() as session:
        assert lease(session, "other-worker", kinds=["idea.clarify_turn"], now=later + timedelta(seconds=91)) == []
        row = session.get(Job, job_id)
        assert (row.status, row.last_error, row.attempts) == ("failed_dead", "job_outcome_uncertain", 1)



def _failure_rows(session, job_id):
    from sqlalchemy import select
    from app.db.models import AnalyticsEvent
    return [json.loads(event.props_json) for event in session.scalars(
        select(AnalyticsEvent).where(AnalyticsEvent.event_name == "job.failed"))
        if json.loads(event.props_json).get("job_id") == job_id]


@pytest.mark.hosted_only
def test_overdue_paid_job_records_one_terminal_error_without_replay(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        job_id = enqueue(session, "research.deep", {}, max_attempts=1)
        session.commit()
        claimed = lease(session, "worker", kinds=["research.deep"])[0]
        row = session.get(Job, job_id)
        row.started_at = utcnow() - timedelta(seconds=row.timeout_s + 1)
        session.commit()
    with factory() as session:
        assert expire_overdue_paid_journey(session) == 1
        assert expire_overdue_paid_journey(session) == 0
        complete_job(session, job_id, worker_id="worker", attempt=claimed.attempts)
        session.commit()
        errors = _failure_rows(session, job_id)
        assert len(errors) == 1
        assert errors[0]["error_code"] == "research_interrupted"
        assert errors[0]["final"] is True
        assert errors[0]["attempt"] == 1
        assert lease(session, "replacement", kinds=["research.deep"]) == []


@pytest.mark.hosted_only
def test_lost_paid_worker_with_uncertain_call_records_terminal_error(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        job_id = enqueue(session, "idea.validate", {"revision_id": "rev_1"})
        session.commit()
    _lose_worker(factory, job_id, {"review": {"state": "calling"}})
    with factory() as session:
        assert reclaim_stale(session) == 1
        assert reclaim_stale(session) == 0
        session.commit()
        errors = _failure_rows(session, job_id)
        assert len(errors) == 1
        assert errors[0]["error_code"] == "job_outcome_uncertain"
        assert errors[0]["final"] is True


@pytest.mark.hosted_only
def test_exhausted_content_worker_records_failure_and_an_alternative(tmp_path):
    from sqlalchemy import select
    from app.db.models import ChatMessage, Venture

    factory = _factory(tmp_path)
    with factory() as session:
        session.add(Venture(id="ven_watchdog", name="Watchdog QA", raw_idea="QA",
                            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        job_id = enqueue(session, "content.draft", {}, venture_id="ven_watchdog", max_attempts=1)
        session.commit()
        assert len(lease(session, "lost", kinds=["content.draft"])) == 1
        session.get(Job, job_id).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with factory() as session:
        assert reclaim_stale(session) == 1
        assert reclaim_stale(session) == 0
        session.commit()
        assert session.get(Job, job_id).last_error == "lease_attempts_exhausted"
        errors = _failure_rows(session, job_id)
        assert len(errors) == 1 and errors[0]["final"] is True
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == "ven_watchdog")).all()
        assert len(messages) == 1
        assert messages[0].kind == "content_failed"
        actions = json.loads(messages[0].actions_json)
        assert any(action.get("label") == "Do it another way" for action in actions)
        assert lease(session, "replacement", kinds=["content.draft"]) == []


def test_safe_paid_worker_resume_does_not_announce_terminal_failure(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        job_id = enqueue(session, "idea.validate", {"revision_id": "rev_1"})
        session.commit()
    _lose_worker(factory, job_id, {"review": {"state": "completed"}})
    with factory() as session:
        assert reclaim_stale(session) == 1
        session.commit()
        assert _failure_rows(session, job_id) == []
        assert session.get(Job, job_id).status == "retry_wait"
