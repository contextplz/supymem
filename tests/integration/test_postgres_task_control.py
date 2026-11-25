"""Two-session task controls on disposable PG; source receipts are synthetic.

Run only under the coordinator's PostgreSQL lease. No providers or workers run.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import json
from threading import Barrier, Event

import pytest
from sqlalchemy import func, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def saved_workflow(d):
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.research.execution import identifier
    from app.tasks.locks import lock_for_job
    from app.tenancy import bind
    from core.clock import utcnow

    now = utcnow()
    with d["factory"]() as session, bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        jid = enqueue(session, "research.deep", {"session_id": d["sid"], "revision_id": d["rid"],
            "_actor_subject": d["actor"]}, workspace_id=d["wid"], venture_id=d["vid"],
            dedupe_key="pg-task-control:" + d["rid"])
        session.commit()
    with d["factory"]() as session:
        task, run, step, job = lock_for_job(session, jid)
        task.state = run.state = step.state = "working"
        job.status, job.finished_at = "succeeded", now
        session.add(ResearchDossier(id=identifier(jid), workspace_id=d["wid"], venture_id=d["vid"],
            revision_id=d["rid"], kind="deep", state="partial", created_at=now, completed_at=now,
            summary=json.dumps({"_runtime": {"job_id": jid, "session_id": d["sid"], "operations": {}},
                "coverage_gaps": ["Synthetic control fixture, not live research evidence"]})))
        session.flush()
        session.add(ResearchFinding(id="pg-control-source:" + d["rid"], dossier_id=identifier(jid),
            workspace_id=d["wid"], venture_id=d["vid"], category="source", title="Synthetic control source",
            url="https://example.test/task-control", source="synthetic", summary="Not live research evidence",
            evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "search_excerpt"}), created_at=now))
        session.commit()
        ids = task.id, run.id, step.id, job.id
    with d["factory"]() as session:
        assert session.get(Task, ids[0]).state == session.get(TaskRun, ids[1]).state == session.get(TaskStep, ids[2]).state == "working"
        count = session.scalar(select(func.count()).select_from(Job).where(Job.venture_id == d["vid"]))
    return (*ids, count)


def test_committed_pause_survives_completion_from_a_stale_session(journey_db):
    from app.api import handoffs_api
    from app.db.models import Job
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.tasks import service
    from app.tenancy import bind

    d = journey_db
    task_id, run_id, step_id, job_id, initial_jobs = saved_workflow(d)
    loaded, paused = Barrier(2), Event()

    def finish_from_old_identity():
        with d["factory"]() as session:
            cached = session.get(Task, task_id)
            assert cached.state == "working"
            loaded.wait(timeout=10)
            assert paused.wait(timeout=10)
            service.after_job(session, job_id)
            session.commit()

    def pause():
        loaded.wait(timeout=10)
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            assert handoffs_api.pause(d["vid"], task_id)["state"] == "paused"
        paused.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        finish, control = pool.submit(finish_from_old_identity), pool.submit(pause)
        control.result(timeout=15)
        finish.result(timeout=15)
    with d["factory"]() as session:
        assert session.get(Task, task_id).state == "paused"
        assert session.get(TaskRun, run_id).state != "completed"
        step = session.get(TaskStep, step_id)
        assert step.state == "completed" and json.loads(step.evidence_json)[0]["source_count"] == 1
        assert not session.scalar(select(TaskEvent.id).where(TaskEvent.task_id == task_id, TaskEvent.kind == "completed"))
        assert session.scalar(select(func.count()).select_from(Job).where(Job.venture_id == d["vid"])) == initial_jobs


def test_parent_lock_serializes_controls_and_keeps_owned_dirty_values(journey_db):
    from app.api import handoffs_api
    from app.db.task_models import Task
    from app.tasks import service
    from app.tasks.locks import TaskLockRequired, lock_for_job, lock_task
    from app.tenancy import bind

    d = journey_db
    task_id, _, _, job_id, _ = saved_workflow(d)
    entered = Event()

    def pause():
        entered.set()
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return handoffs_api.pause(d["vid"], task_id)

    with ThreadPoolExecutor(max_workers=1) as pool, d["factory"]() as owner:
        task, run, step, job = lock_for_job(owner, job_id)
        task.title, run.summary, job.last_error = "Owned pending title", "Owned pending summary", "synthetic_pending"
        again = lock_for_job(owner, job_id)
        assert again == (task, run, step, job)
        assert (task.title, run.summary, job.last_error) == ("Owned pending title", "Owned pending summary", "synthetic_pending")
        control = pool.submit(pause)
        assert entered.wait(timeout=10)
        with pytest.raises(TimeoutError):
            control.result(timeout=0.2)
        service.after_job(owner, job_id)
        owner.commit()
        assert control.result(timeout=15)["state"] == "completed"
        with d["factory"]() as other:
            fresh = lock_task(other, task_id)
            assert fresh.state == "completed" and fresh.title == "Owned pending title"
            fresh.title = "Changed after commit"
            other.commit()
        assert lock_task(owner, task_id).title == "Changed after commit"  # Prior transaction cache is gone.
        owner.rollback()
    with d["factory"]() as dirty:
        row = dirty.get(Task, task_id)
        row.title = "Unprotected dirty identity"
        with pytest.raises(TaskLockRequired, match="task_lock_before_mutation_required"):
            lock_task(dirty, task_id)
        assert row.title == "Unprotected dirty identity"  # No refresh or implicit stale flush.
        dirty.rollback()
