"""Migrated PostgreSQL: restart-safe founder jobs and fenced worker ownership."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import (
    ROOT, concurrent, pg_database as pg_database, service_modules,
)


@pytest.fixture()
def journey_db(pg_database, tmp_path, monkeypatch, request):
    url, engine = pg_database
    for key, value in {"APP_ENV": "local", "COMPANY_OS_DB_URL": url, "COMPANY_OS_DATA_DIR": str(tmp_path),
                       "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
                       "COMPANY_OS_REQUIRE_SIGNED_CALLER": "1", "LLM_OFFLINE": "1", "WILL_SCHEDULER": "0",
                       "CONTENT_ENGINE_ENABLED": "0", "CONTENT_AGENT_ENABLED": "0", "MARKETING_EXECUTION_ENABLED": "0",
                       "MARKETING_OUTBOUND_ENABLED": "0", "CHANNELS_DRY_RUN": "true"}.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from app.authz.workspace import ensure_subject_person
        from app.core.jobs import enqueue
        from app.db.models import Venture
        from app.db.supymem_models import IdeaSession, IdeaRevision
        from app.state import set_factory
        from app.tenancy import bind
        from app.workspaces.service import create_workspace
        from core.clock import utcnow
        from core.ids import new_id

        factory = sessionmaker(engine, expire_on_commit=False)
        set_factory(factory)  # Schema is migrated; create_all must not hide a gap.
        from app.core.local_mode import is_local
        if is_local():
            from secrets import token_urlsafe
            from app.core.local_owner import ensure_owner
            monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "http://127.0.0.1:3002")
            monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://your-instance.example")
            monkeypatch.setenv("OUTREACH_UNSUB_SECRET", token_urlsafe(32))
        actor = ensure_owner().id if is_local() else "usr_pg_journey_" + uuid.uuid4().hex[:12]
        ensure_subject_person(actor)
        workspace = create_workspace(kind="company", name="Durable test", owner_id=actor)
        vid, sid, rid, now = new_id("ven"), new_id("ids"), new_id("idv"), utcnow()
        with factory() as session:
            session.add(Venture(id=vid, workspace_id=workspace.id, name="Order board", raw_idea="Bakery order board",
                                stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaSession(id=sid, workspace_id=workspace.id, venture_id=vid, case="know", state="confirmed",
                                    current_revision_id=rid, confirmed_revision_id=rid, confirmed_by_person_id=actor,
                                    confirmed_at=now, created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id=rid, session_id=sid, workspace_id=workspace.id, venture_id=vid, rev=1,
                                     source="founder", structured=json.dumps({"offer": "Bakery order board", "desired_outcome": "First paying bakery"}
                                         | getattr(request, "param", {})), created_at=now))
            session.commit()
        with bind(workspace_id=workspace.id, venture_id=vid, actor=actor), factory() as session:
            job_id = enqueue(session, "plan.compile", {"revision_id": rid}, workspace_id=workspace.id,
                             venture_id=vid, dedupe_key="plan-test:" + rid)
            session.commit()
        yield {"factory": factory, "engine": engine, "actor": actor, "wid": workspace.id,
               "vid": vid, "sid": sid, "rid": rid, "job_id": job_id, "url": url, "monkeypatch": monkeypatch}


WORKER_SCRIPT = '''
import asyncio
import json
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.state import set_factory
from app.core.jobs import lease
from app.worker.handlers import install_default_handlers
from app.worker.main import _run_one
factory = sessionmaker(create_engine(os.environ["COMPANY_OS_DB_URL"]), expire_on_commit=False)
set_factory(factory)
install_default_handlers()
# Only the provider network seam is replaced; the durable worker and domain run.
from pytest import MonkeyPatch
from tests.planning_fixtures import install_planner
patch = MonkeyPatch()
install_planner(patch)
with factory() as session:
    jobs = lease(session, "separate-process-worker", kinds=["plan.compile"], n=1)
    session.commit()
for job in jobs:
    asyncio.run(_run_one(factory, job, {job.id}))
print(json.dumps({"completed": [job.id for job in jobs]}))
'''


def test_job_survives_api_process_and_executes_in_separate_worker(journey_db):
    from app.db.models import Job
    from app.db.supymem_models import ExecutionPlan
    from app.planning.compiler import compile_plan
    d = journey_db
    from tests.planning_fixtures import seed_research
    from app.planning.resources import update_inventory
    from app.credits.accounts import ensure_account
    seed_research(d)
    update_inventory(d["wid"], d["vid"], [], True)
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    # No main worker boot, scheduler, health listener, live provider, or outreach.
    env = dict(os.environ)
    # The worker must use this checkout for both app and shared core modules;
    # an editable install may otherwise point at a different worktree.
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "services/company_os"), str(ROOT / "packages")])
    env.pop("REDIS_URL", None)
    result = subprocess.run([sys.executable, "-c", WORKER_SCRIPT], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1])["completed"] == [d["job_id"]]
    with d["factory"]() as session:
        job = session.get(Job, d["job_id"])
        assert job.status == "succeeded" and job.attempts == 1, (job.status, job.attempts, job.last_error)
        plans = session.scalars(select(ExecutionPlan).where(ExecutionPlan.venture_id == d["vid"])).all()
        assert len(plans) == 1 and plans[0].workspace_id == d["wid"]
        plan_id = plans[0].id
    # Crash after domain commit but before acknowledgment must not duplicate a plan.
    repeated = compile_plan(d["vid"], d["wid"], d["rid"], operation_id=d["job_id"])
    assert repeated["id"] == plan_id


def test_concurrent_idea_page_loads_queue_initial_clarification_once(journey_db, monkeypatch):
    from sqlalchemy import func
    from app.db.models import Job, Venture
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea import service

    d = journey_db
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state = "open"
        idea.confirmed_at = idea.confirmed_revision_id = idea.confirmed_by_person_id = None
        session.commit()

    barrier = Barrier(2)
    original = service.post_message

    def both_reads_finish_before_either_submission(*args, **kwargs):
        barrier.wait(timeout=15)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "post_message", both_reads_finish_before_either_submission)

    def load_page():
        with d["factory"]() as session:
            venture = session.get(Venture, d["vid"])
            return service.open_session(venture=venture, actor_subject=d["actor"])

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: load_page(), range(2)))

        assert {result["id"] for result in results} == {d["sid"]}
        with d["factory"]() as session:
            assert session.scalar(select(func.count(ClarifierTurn.id)).where(
                ClarifierTurn.session_id == d["sid"], ClarifierTurn.role == "founder")) == 1
            assert session.scalar(select(func.count(Job.id)).where(
                Job.venture_id == d["vid"], Job.kind == "idea.clarify_turn")) == 1
    finally:
        # The fixture's plan job and this test's clarification job share the
        # module database with other lease tests. Retire only this venture's
        # inert jobs so their global worker cannot claim them later.
        with d["factory"]() as session:
            session.execute(update(Job).where(Job.venture_id == d["vid"]).values(status="cancelled"))
            session.commit()


def test_pg_stale_worker_cannot_finish_or_fail_reclaimed_job(journey_db):
    from app.core.jobs import FatalError, complete_job, fail_job, heartbeat, lease, release_lease
    from app.db.models import Job
    from core.clock import utcnow
    d = journey_db
    with d["factory"]() as session:
        old = lease(session, "old-worker", kinds=["plan.compile"])[0]
        session.commit()
    with d["factory"]() as session:
        session.get(Job, old.id).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with d["factory"]() as session:
        new = lease(session, "new-worker", kinds=["plan.compile"])[0]
        session.commit()
    with d["factory"]() as session:
        complete_job(session, old.id, worker_id=old.worker_id, attempt=old.attempts)
        fail_job(session, old.id, FatalError("stale"), worker_id=old.worker_id, attempt=old.attempts)
        release_lease(session, old.id, worker_id=old.worker_id, attempt=old.attempts)
        heartbeat(session, old.id, worker_id=old.worker_id, attempt=old.attempts)
        session.commit()
        current = session.get(Job, old.id)
        assert current.status == "leased" and current.locked_by == "new-worker" and current.last_error is None
    with d["factory"]() as session:
        complete_job(session, new.id, worker_id=new.worker_id, attempt=new.attempts)
        session.commit()
        assert session.get(Job, new.id).status == "succeeded"


def test_pg_concurrent_enqueue_and_lease_keep_one_scoped_intent(journey_db):
    from app.core.jobs import enqueue, lease
    from app.tenancy import bind
    d = journey_db
    def schedule():
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), d["factory"]() as session:
            value = enqueue(session, "plan.compile", {"revision_id": d["rid"]}, workspace_id=d["wid"],
                            venture_id=d["vid"], dedupe_key="plan-test:" + d["rid"])
            session.commit()
            return value
    assert set(concurrent(schedule)) == {d["job_id"]}
    def claim():
        with d["factory"]() as session:
            jobs = lease(session, "worker_" + uuid.uuid4().hex[:8], kinds=["plan.compile"])
            session.commit()
            return [job.id for job in jobs]
    assert sum(concurrent(claim), []) == [d["job_id"]]
    with d["factory"]() as session, pytest.raises(ValueError, match="scope_mismatch"):
        enqueue(session, "plan.compile", {}, workspace_id="foreign", venture_id=d["vid"], dedupe_key="plan-test:" + d["rid"])


def test_worker_denies_removed_founder_and_foreign_payload(journey_db):
    from app.core.jobs import lease
    from app.db.models import Job, WorkspaceMember
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    d = journey_db
    install_default_handlers()
    with d["factory"]() as session:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"], WorkspaceMember.person_id == d["actor"]))
        member.role = "viewer"
        job = lease(session, "authority-worker", kinds=["plan.compile"])[0]
        session.commit()
    asyncio.run(_run_one(d["factory"], job, {job.id}))
    with d["factory"]() as session:
        row = session.get(Job, job.id)
        assert row.status == "failed_dead" and row.last_error == "job_authority_revoked"


def test_pg_repeated_crash_stops_at_configured_attempt_limit(journey_db):
    from app.core.jobs import lease, reclaim_stale
    from app.db.models import Job
    from core.clock import utcnow
    d = journey_db
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).max_attempts = 1
        claimed = lease(session, "one-attempt-worker", kinds=["plan.compile"])
        session.commit()
        assert len(claimed) == 1 and claimed[0].attempts == 1
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with d["factory"]() as session:
        assert reclaim_stale(session) == 1
        session.commit()
        row = session.get(Job, d["job_id"])
        # plan.compile is a paid journey kind: an exhausted lost lease ends as an
        # uncertain terminal outcome (never redialled), like clarify/validate/research.
        assert row.status == "failed_dead" and row.last_error == "job_outcome_uncertain"
        assert row.attempts == 1
        assert not lease(session, "retry-worker", kinds=["plan.compile"])
