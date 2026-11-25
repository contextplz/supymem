"""Migrated PostgreSQL: actual queued planned research cannot close on fallback."""
import asyncio
import json

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@pytest.mark.parametrize("live_page", [False, True])
def test_queued_planned_research_state_and_chat_match_actual_live_page(journey_db, monkeypatch, live_page):
    from app.chat.store import list_messages
    from app.core.jobs import lease
    from app.db.models import Job, PlannedTask, VentureDocument
    from app.db.supymem_models import ExecutionPlan
    from app.planning.execute import start_planned_task
    from app.planning.store import save_tasks
    from app.tenancy import bind
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from core.clock import utcnow
    from core.ids import new_id

    d, plan_id = journey_db, new_id("pln")
    with d["factory"]() as session:
        session.add(ExecutionPlan(id=plan_id, workspace_id=d["wid"], venture_id=d["vid"],
            version=1, state="approved", created_at=utcnow(),
            structured=json.dumps({"scope_details": {"tsk_source": {"scope": "in"}}})))
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        task = save_tasks(d["vid"], [{"title": "Compare bakery alternatives using source pages",
            "detail": "Prepare observed notes only; contact nobody.\n\nDone when:\n- Compare three alternatives and verify current prices.", "why_now": "Approved research",
            "tags": ["plan_task:tsk_source", "capability:research.deep"], "runnable": True}],
            source="approved_plan:" + plan_id)[0]
        assert start_planned_task(d["vid"], task["id"])["queued"] is True

    async def explore(*_args, **_kwargs):
        if not live_page:
            raise RuntimeError("private provider exception")
        text = "This source describes bakery order boards."
        return {"mode": "live", "summary": "Live source read.",
            "hits": [{"url": "https://example.com/bakery"}],
            "pages": [{"url": "https://example.com/bakery", "excerpt": text, "chars": len(text)}]}

    monkeypatch.setattr("app.research.explore.explore_web", explore)
    install_default_handlers()
    with d["factory"]() as session:
        job_id = session.scalar(select(Job.id).where(Job.venture_id == d["vid"], Job.kind == "task.run"))
        claimed = lease(session, "owned-planned-research", kinds=["task.run"], n=100)
        jobs = [row for row in claimed if row.id == job_id]
        assert len(jobs) == 1 and jobs[0].payload["_actor_subject"] == d["actor"]
        session.commit()
    asyncio.run(_run_one(d["factory"], jobs[0], {job_id}))
    with d["factory"]() as session:
        row, job = session.get(PlannedTask, task["id"]), session.get(Job, job_id)
        note = session.get(VentureDocument, "task-note:" + task["id"])
        assert note and "private provider" not in note.body_md
        if live_page:
            assert row.status == "open" and row.runnable == 0 and row.finished_at is None
            assert row.last_error == "incomplete:research_coverage_unverified"
            assert job.status == "failed_dead" and job.last_error == "research_coverage_unverified"
            assert "https://example.com/bakery" in note.body_md and "acceptance criteria have not been verified" in note.body_md
        else:
            assert row.status == "open" and row.runnable == 0 and row.finished_at is None
            assert row.last_error == "incomplete:research_sources_unverified"
            assert job.status == "failed_dead" and job.last_error == "research_sources_unverified"
            assert "Status: not done" in note.body_md
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        finished = any("Finished" in message["body_md"] for message in list_messages(d["vid"]))
        assert finished is False
