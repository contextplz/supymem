"""Worker ticks use real five-minute boundaries and dedupe each window."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from tests.company_os_harness import configure_local_test_model, load_company_os_app


@pytest.mark.parametrize("kind", ["inbox.briefing.tick", "autopilot.tick", "gtm.journey.maintain", "inbox.cos_brief.tick"])
def test_scheduled_fanout_is_bounded_durable_and_founder_scoped(tick_scheduler, tmp_path, monkeypatch, kind):
    import asyncio
    import json
    from datetime import timedelta
    from app.core import jobs, scheduled
    from app.db.models import Job, Person, Venture, Workspace, WorkspaceMember
    from app.state import db, reset_db_factory
    from app.tenancy import bind
    from app.worker import handlers
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path / 'scale.sqlite'}")
    reset_db_factory()
    monkeypatch.setenv("SCHEDULED_FANOUT_BATCH_SIZE", "25")
    now = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
    with db()() as session:
        for index in range(200):
            owner, wid, vid = f"scale-owner-{index:03}", f"scale-workspace-{index:03}", f"scale-venture-{index:03}"
            domain = "Stock tracking for clinics" if index % 2 == 0 else "Nature journals for hikers"
            session.add(Person(id=owner, display_name="You", created_at=now))
            session.add(Workspace(id=wid, owner_id=owner, name=domain, kind="company", entry_mode="idea", created_at=now, updated_at=now))
            session.add(WorkspaceMember(id="member-" + wid, workspace_id=wid, person_id=owner, role="owner", created_at=now))
            session.add(Venture(id=vid, workspace_id=wid, name=domain, raw_idea=domain, stage="draft", created_at=now, updated_at=now))
        session.add(Workspace(id="scale-sibling", owner_id="scale-owner-000", name="Another workspace", kind="company",
            entry_mode="idea", created_at=now, updated_at=now))
        session.add(WorkspaceMember(id="member-scale-sibling", workspace_id="scale-sibling", person_id="scale-owner-000", role="owner", created_at=now))
        session.add(Venture(id="scale-sibling-project", workspace_id="scale-sibling", name="Another project",
            raw_idea="A different workflow", stage="draft", created_at=now, updated_at=now))
        session.commit()
    payload = {"window": "scale-window"}
    with bind(workspace_id="none", venture_id="", actor="system"):
        if kind == "inbox.briefing.tick":
            from app.inbox import briefing
            monkeypatch.setattr(briefing.store, "enabled", lambda session=None: True)
            asyncio.run(briefing.tick(payload, now=now))
        elif kind == "inbox.cos_brief.tick":
            from app.inbox import weekly
            monkeypatch.setattr(weekly, "enabled", lambda: True)
            asyncio.run(weekly.tick(payload, now=now))
        elif kind == "autopilot.tick":
            asyncio.run(handlers._autopilot_tick(payload))
        else:
            asyncio.run(handlers._gtm_journey_maintain(payload))
        assert scheduled.fanout(kind, payload, now=now) == 25
        with db()() as session:
            first = list(session.scalars(select(Job).where(Job.kind == kind)))
            assert len(first) == 26
            assert sum(bool(json.loads(row.payload_json).get("founder_id")) for row in first) == 25
        for cursor in range(24, 200, 25):
            scheduled.fanout(kind, {**payload, "fanout_cursor": f"scale-owner-{cursor:03}"}, now=now)
    with db()() as session:
        children = list(session.scalars(select(Job).where(Job.kind == kind, Job.workspace_id.is_not(None))))
        assert len(children) == 200 and len({row.dedupe_key for row in children}) == 200
        assert all(row.effect_key == row.dedupe_key and row.run_after is not None for row in children)
        assert all(json.loads(row.payload_json)["_actor_subject"] == json.loads(row.payload_json)["founder_id"] for row in children)
        claimed = jobs.lease(session, "scale-worker", kinds=[kind], n=1, now=now + timedelta(minutes=2))
        session.commit()
        assert len(claimed) == 1 and session.get(Job, claimed[0].id).locked_by == "scale-worker"
    seen = []
    async def advance(founder_id=None):
        from app.autopilot.authority import founder_scope
        from app.tenancy import current
        assert scheduled.FOUNDER_JOB.get() is True
        seen.append(founder_id)
        if founder_id == "scale-owner-000":
            with founder_scope("scale-sibling-project", autonomous=True):
                assert current().workspace_id == "scale-sibling"
            with pytest.raises(PermissionError, match="autopilot_scope_mismatch"):
                with founder_scope("scale-venture-001", autonomous=True):
                    pytest.fail("A founder job cannot access another founder")
            raise ValueError("isolated founder failure")
    from app.autopilot import service
    monkeypatch.setattr(service, "tick_all", advance)
    monkeypatch.setattr(handlers, "_autopilot_housekeeping", lambda founder_id: None)
    monkeypatch.setattr(handlers, "HANDLERS", {"autopilot.tick": handlers._autopilot_tick})
    for index in (0, 1):
        founder = f"scale-owner-{index:03}"
        with bind(workspace_id=f"scale-workspace-{index:03}", venture_id="", actor=founder):
            child = {"founder_id": founder, "workspace_id": f"scale-workspace-{index:03}", "window": "execution-window"}
            if index == 0:
                with pytest.raises(ValueError, match="isolated founder failure"):
                    asyncio.run(handlers.dispatch("autopilot.tick", child))
            else:
                asyncio.run(handlers.dispatch("autopilot.tick", child))
    assert seen == ["scale-owner-000", "scale-owner-001"]
    assert scheduled.FOUNDER_JOB.get() is False
    from app.autopilot.authority import founder_scope
    with bind(workspace_id="scale-workspace-000", venture_id="", actor="scale-owner-000"):
        with pytest.raises(PermissionError, match="autopilot_scope_mismatch"):
            with founder_scope("scale-sibling-project", autonomous=True):
                pytest.fail("An ordinary request cannot widen its scope")
    from app.core.ratelimit import RateLimitedJob
    first = {"founder_id": "scale-owner-000", "workspace_id": "scale-workspace-000", "window": "scale-window"}
    second = {"founder_id": "scale-owner-001", "workspace_id": "scale-workspace-001", "window": "scale-window"}
    with bind(workspace_id=first["workspace_id"], venture_id="", actor=first["founder_id"]):
        scheduled.authorize_founder(first)
    with bind(workspace_id=first["workspace_id"], venture_id="", actor=second["founder_id"]):
        with pytest.raises(PermissionError, match="scheduled_founder_scope_mismatch"):
            scheduled.authorize_founder(first)
    first_claim = scheduled.claim_founder(kind, first)
    with pytest.raises(RateLimitedJob):
        scheduled.claim_founder(kind, first)
    second_claim = scheduled.claim_founder(kind, second)
    scheduled.finish_founder(first_claim, succeeded=False)
    retried = scheduled.claim_founder(kind, first)
    scheduled.finish_founder(retried, succeeded=True)
    scheduled.finish_founder(second_claim, succeeded=True)
    assert scheduled.claim_founder(kind, first) is None


@pytest.fixture()
def tick_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.tasks import scheduler
    configure_local_test_model()
    monkeypatch.setattr(scheduler, "publish_wake", lambda: None)
    return scheduler


@pytest.mark.parametrize("minute,expected", [
    (0, "00"), (4, "00"), (5, "05"), (9, "05"), (10, "10"),
    (54, "50"), (55, "55"), (59, "55"),
])
def test_five_minute_window_boundaries(tick_scheduler, minute, expected):
    now = datetime(2026, 10, 2, 12, minute, 59, tzinfo=timezone.utc)
    assert tick_scheduler._window("5m", now) == "20261002T12" + expected


def test_five_minute_window_changes_at_hour_rollover(tick_scheduler):
    assert tick_scheduler._window("5m", datetime(2026, 10, 2, 12, 59)) == "20261002T1255"
    assert tick_scheduler._window("5m", datetime(2026, 10, 2, 13, 0)) == "20261002T1300"


def test_scheduler_enqueues_once_per_window_and_again_after_boundary(tick_scheduler, monkeypatch):
    from app.core.local_mode import is_local
    from app.db.models import Job
    from app.state import db
    from app.worker import main as worker_main
    from app.tasks import service

    kinds = {"autopilot.tick", "credits.sweep"}
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"run_for_me_experiments": True}, {})
        update_settings("spend", {"monthly_spend_cap": "5"}, {})
        kinds.remove("credits.sweep")

    class Clock(datetime):
        minute = 4

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 12, cls.minute, tzinfo=tz)

    monkeypatch.setattr(tick_scheduler, "datetime", Clock)
    monkeypatch.setattr(tick_scheduler, "utcnow", lambda: Clock.now(timezone.utc))
    monkeypatch.setattr(service, "fire_due", lambda session, **kwargs: [])
    monkeypatch.setattr(worker_main, "_run_maintenance", lambda: None)
    monkeypatch.setattr(worker_main, "publish_wake", lambda: None)
    first = tick_scheduler.dispatch_tick("5m")
    assert kinds <= set(first["enqueued"])
    if is_local():
        assert "credits.sweep" not in first["enqueued"]
    worker_main._run_scheduler()
    worker_main._run_scheduler()
    Clock.minute = 5
    second = tick_scheduler.dispatch_tick("5m")
    worker_main._run_scheduler()
    assert second["window"] != first["window"]
    with db()() as session:
        for result in (first, second):
            for kind in kinds:
                jobs = session.scalars(select(Job).where(
                    Job.dedupe_key == f"{kind}:{result['window']}"
                )).all()
                assert len(jobs) == 1
                assert jobs[0].id == result["ids"][result["enqueued"].index(kind)]
        from app.db.task_models import MaintenanceSchedule
        assert {row.tick for row in session.scalars(select(MaintenanceSchedule))} == set(tick_scheduler.INTERVALS)
