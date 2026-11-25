"""Validation receipts and founder decisions in the actual migrated PG schema."""
import asyncio
import json

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.security.test_idea_validation_boundary import validation_provider


@pytest.fixture()
def validation(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.db.supymem_models import IdeaSession
    from app.idea.validation import start_validation
    from app.tenancy import bind
    d = journey_db
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.confirmed_at, idea.confirmed_revision_id = "clarifying", None, None
        session.commit()
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    d["calls"] = validation_provider(monkeypatch)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        d["validation_job_id"] = start_validation(d["sid"])["job_id"]
    return d


def run_one(d):
    from app.core.jobs import lease
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    with d["factory"]() as session:
        job = lease(session, "pg-validation", kinds=["idea.validate"], n=1)[0]
        session.commit()
    asyncio.run(_run_one(d["factory"], job, {job.id}))


def test_pg_validation_preserves_method_evidence_and_replays_one_paid_receipt(validation):
    from app.credits.accounts import get_account
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import IdeaSession, ValidationRun
    from app.idea.validation import decide
    from app.tenancy import bind
    d = validation
    run_one(d)
    with d["factory"]() as session:
        job = session.get(Job, d["validation_job_id"])
        assert job.status == "succeeded", job.last_error
        job.status, job.run_after, job.scheduled_for = "retry_wait", None, None
        session.commit()
    run_one(d)
    with d["factory"]() as session:
        runs = session.scalars(select(ValidationRun).where(ValidationRun.venture_id == d["vid"])).all()
        assert len(runs) == 3 and len(d["calls"]) == 1
        assert all(json.loads(run.report)["dossier_id"] for run in runs)
        spends = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.account_id == get_account(d["vid"]).id, CreditLedgerEntry.kind == "spend")).all()
        assert len(spends) == 1 and spends[0].amount_micro < 0
        run_id = runs[0].id
        assert session.get(IdeaSession, d["sid"]).state == "validating"
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        assert decide(d["sid"], run_id, "accept")["state"] == "reviewing"


def test_pg_concurrent_reopen_creates_one_assumed_question_review(journey_db):
    from concurrent.futures import ThreadPoolExecutor
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.gstack import office_hours
    from app.idea.service import reopen_assumed_questions
    from app.tenancy import bind
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), d["factory"]() as session:
        row = session.get(IdeaSession, d["sid"])
        questions = office_hours.default_picks()
        office_hours.add_question_turn(session, row, questions[0], questions[1:], 1, len(questions), utcnow())
        session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=d["wid"],
            venture_id=d["vid"], turn=2, role="founder", kind="answer", text=office_hours.SKIP, created_at=utcnow()))
        session.commit()
    def reopen():
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return reopen_assumed_questions(d["sid"], venture_id=d["vid"], revision_id=d["rid"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        reviews = list(pool.map(lambda _: reopen(), range(2)))
    assert reviews[0]["id"] == reviews[1]["id"] != d["sid"]
    assert reviews[0]["state"] == "validating"
    assert reviews[0]["turns"][-1]["options"]["total"] == 3
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), d["factory"]() as session:
        rows = session.scalars(select(IdeaSession).where(IdeaSession.venture_id == d["vid"])).all()
        assert len(rows) == 2
        assert session.get(IdeaSession, d["sid"]).state == "confirmed"
        assert not session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind == "idea.validate")).all()
