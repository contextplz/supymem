"""Migrated PG task locking and recovery; provider evidence is synthetic.

Run only on the disposable loopback TEST_POSTGRES_URL under the shared PG lease.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from threading import Barrier

from sqlalchemy import func, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def _contract(*, kind="one_time"):
    from app.tasks.contracts import Contract
    return Contract(title="Synthetic PG research", goal="Research the synthetic PG idea", kind=kind,
        trigger={"event_type": "waitlist.signup", "filter": {"source": "website"}} if kind == "event" else {},
        steps=[{"title": "Research", "skill": "research.deep"}])


def _create(d, *, kind="one_time", key=None):
    from app.tasks import service
    with d["factory"]() as session:
        task = service.create(session, workspace_id=d["wid"], venture_id=d["vid"], owner_person_id=d["actor"],
            contract=_contract(kind=kind), create_key=key)
        session.commit()
        return task.id


def test_concurrent_create_key_has_one_task_run_and_start_job(journey_db):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    d, barrier = journey_db, Barrier(2)
    def request():
        barrier.wait(timeout=10)
        return _create(d, key="pg-task-create:" + d["rid"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: request(), range(2)))
    assert ids[0] == ids[1]
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Task).where(Task.venture_id == d["vid"])) == 1
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == ids[0]))
        assert run and session.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.task_id == ids[0])) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(
            Job.venture_id == d["vid"], Job.kind == "tasks.execute")) == 1


def test_concurrent_workflow_admission_has_one_durable_projection(journey_db):
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    d, barrier = journey_db, Barrier(2)
    def admit():
        barrier.wait(timeout=10)
        with d["factory"]() as session:
            jid = enqueue(session, "research.deep", {"session_id": d["sid"], "revision_id": d["rid"],
                "_actor_subject": d["actor"]}, workspace_id=d["wid"], venture_id=d["vid"],
                dedupe_key="pg-task-research:" + d["rid"], effect_key="pg-task-effect:" + d["rid"])
            session.commit()
            return jid
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: admit(), range(2)))
    assert ids[0] == ids[1]
    with d["factory"]() as session:
        steps = session.scalars(select(TaskStep).where(TaskStep.job_id == ids[0])).all()
        assert len(steps) == 1
        run = session.get(TaskRun, steps[0].run_id)
        task, job = session.get(Task, run.task_id), session.get(Job, ids[0])
        assert (task.workspace_id, task.venture_id, task.owner_person_id) == (d["wid"], d["vid"], d["actor"])
        assert json.loads(steps[0].inputs_json) == json.loads(job.payload_json)
        assert job.effect_key == "pg-task-effect:" + d["rid"]


def test_concurrent_domain_event_delivery_creates_one_scoped_run(journey_db):
    from app.db.task_models import TaskRun
    from app.tasks import service
    d, barrier = journey_db, Barrier(2)
    task_id = _create(d, kind="event")
    def deliver():
        barrier.wait(timeout=10)
        with d["factory"]() as session:
            ids = service.emit_domain_event(session, workspace_id=d["wid"], venture_id=d["vid"],
                event_type="waitlist.signup", event_id="synthetic-signup", data={"source": "website"})
            session.commit()
            return ids
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: deliver(), range(2)))
    with d["factory"]() as session:
        assert service.emit_domain_event(session, workspace_id="foreign", venture_id=d["vid"],
            event_type="waitlist.signup", event_id="synthetic-foreign", data={"source": "website"}) == []
        runs = session.scalars(select(TaskRun).where(TaskRun.task_id == task_id)).all()
        assert len(runs) == 1 and runs[0].run_key == "event:waitlist.signup:synthetic-signup"


def test_scheduler_leader_does_not_duplicate_maintenance_windows(journey_db):
    from app.db.models import Job
    from app.tasks.scheduler import run_due
    d, barrier = journey_db, Barrier(2)
    moment = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    def tick():
        barrier.wait(timeout=10)
        with d["factory"]() as session:
            result = run_due(session, now=moment)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: tick(), range(2)))
    emitted = [tick["tick"] for result in outcomes for tick in result["ticks"]]
    assert len(emitted) == len(set(emitted))
    assert {"5m", "15m", "hourly", "6h", "daily", "weekly"} <= set(emitted)
    with d["factory"]() as session:
        keys = session.scalars(select(Job.dedupe_key).where(Job.dedupe_key.like("%:2026%"))).all()
        assert len(keys) == len(set(keys))


def test_saved_research_receipt_recovers_task_checkpoint_without_paid_replay(journey_db):
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.research.execution import identifier
    from app.tasks import service
    from core.clock import utcnow
    d, now = journey_db, utcnow()
    with d["factory"]() as session:
        jid = enqueue(session, "research.deep", {"session_id": d["sid"], "revision_id": d["rid"],
            "_actor_subject": d["actor"]}, workspace_id=d["wid"], venture_id=d["vid"],
            dedupe_key="pg-task-crash:" + d["rid"])
        session.commit()
    # Simulate a saved domain result and durable job completion whose task
    # projection was lost. No provider is invoked to manufacture the receipt.
    with d["factory"]() as session:
        job = session.get(Job, jid)
        job.status, job.finished_at = "succeeded", now
        dossier = ResearchDossier(id=identifier(jid), workspace_id=d["wid"], venture_id=d["vid"],
            revision_id=d["rid"], kind="deep", state="partial", created_at=now, completed_at=now,
            summary=json.dumps({"_runtime": {"job_id": jid, "session_id": d["sid"], "operations": {}},
                "coverage_gaps": ["Synthetic receipt fixture, not live research evidence"]}))
        session.add(dossier)
        session.flush()
        session.add(ResearchFinding(id="pg-task-finding:" + d["rid"], dossier_id=dossier.id,
            workspace_id=d["wid"], venture_id=d["vid"], category="source", title="Synthetic source",
            url="https://example.test/synthetic", source="synthetic", summary="Not live research evidence",
            evidence=json.dumps({"observed_at": now.isoformat(), "evidence_kind": "search_excerpt"}), created_at=now))
        session.commit()
    with d["factory"]() as session:
        for _ in range(2):
            service.fire_due(session, now=now)
            session.flush()
        session.commit()
    with d["factory"]() as session:
        step = session.scalar(select(TaskStep).where(TaskStep.job_id == jid))
        run = session.get(TaskRun, step.run_id)
        assert step.state == run.state == session.get(Task, run.task_id).state == "completed"
        assert session.scalar(select(func.count()).select_from(TaskEvent).where(
            TaskEvent.run_id == run.id, TaskEvent.kind == "completed")) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(
            Job.venture_id == d["vid"], Job.kind == "research.deep")) == 1
