"""A lost task worker offers recovery attached to the task's saved Work record."""
import json

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_recovery import _alt, _messages
from tests.unit.chat.test_round3_matrix import venture as venture_fixture

pytestmark = pytest.mark.hosted_only

load_company_os_app()
venture = venture_fixture


def test_exhausted_task_watchdog_recovery_keeps_task_result_identity(venture):
    from datetime import timedelta
    from sqlalchemy import select
    from app.chat import recovery
    from app.core.jobs import enqueue, lease, reclaim_stale
    from app.db.models import AnalyticsEvent, PlannedTask, VentureDocument
    from app.state import db
    from core.clock import utcnow

    _, vid, wid = venture
    with db()() as session:
        session.add(PlannedTask(id="tsk_watchdog", venture_id=vid, workspace_id=wid,
                               title="Write a pitch", detail="d", why_now="w", status="open",
                               created_at=utcnow(), updated_at=utcnow()))
        job_id = enqueue(session, "task.run", {"task_id": "tsk_watchdog"},
                         venture_id=vid, workspace_id=wid, max_attempts=1)
        session.commit()
        claimed = next(job for job in lease(session, "lost", kinds=["task.run"]) if job.id == job_id)
        from app.db.models import Job
        session.get(Job, claimed.id).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with db()() as session:
        assert reclaim_stale(session) == 1
        assert reclaim_stale(session) == 0
        session.commit()
        events = [json.loads(event.props_json) for event in session.scalars(
            select(AnalyticsEvent).where(AnalyticsEvent.event_name == "job.failed"))]
        assert sum(event.get("job_id") == job_id for event in events) == 1
    failures = [actions for kind, actions in _messages(vid) if kind == "task_failed"]
    assert len(failures) == 1
    alternative = _alt(failures[0])
    assert alternative["payload"]["job_id"] == "tsk_watchdog"
    recovery.submit(vid, {"step": "task.run", "job_id": "tsk_watchdog", "text": "Pitch completed manually."}, wid)
    with db()() as session:
        doc = session.get(VentureDocument, "task-note:tsk_watchdog")
        assert doc.kind == "task_note"
        assert json.loads(doc.provenance_json)["source"] == "founder_provided"
        assert lease(session, "replacement", kinds=["task.run"]) == []
