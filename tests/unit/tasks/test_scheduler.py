"""Synthetic clocks exercise durable maintenance timing without provider calls."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 8, 12, 4, tzinfo=timezone.utc)


@pytest.fixture()
def scheduler(tmp_path, monkeypatch):
    from core import clock

    monkeypatch.setattr(clock, "_clock", lambda: NOW)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path / 'scheduler.db'}")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.state import db
    from app.core import local_worker
    from app.tasks import scheduler, service
    from app.worker import main

    maintenance = main._run_maintenance
    fire_due = service.fire_due
    spend = getattr(local_worker, "spend_available", None)
    capabilities = getattr(local_worker, "capability_enabled", None)
    monkeypatch.setattr(service, "fire_due", lambda session, **kwargs: [])
    monkeypatch.setattr(main, "publish_wake", lambda: None)
    monkeypatch.setattr(main, "_run_maintenance", lambda: None)
    monkeypatch.setattr(local_worker, "capability_enabled", lambda name: True, raising=False)
    monkeypatch.setattr(local_worker, "spend_available", lambda: True, raising=False)
    return SimpleNamespace(module=scheduler, factory=db(), worker=main, service=service, maintenance=maintenance,
        spend=spend, capabilities=capabilities, fire_due=fire_due)


def fire(scheduler, moment=NOW):
    with scheduler.factory() as session:
        result = scheduler.module.run_due(session, now=moment)
        session.commit()
    return result


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_local_schedule_skips_hosted_ticks_but_hosted_keeps_them(scheduler, monkeypatch, domain, mode):
    from app.core import local_worker

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    ticks = fire(scheduler)["ticks"]
    scheduled = {kind for tick in ticks for kind in tick["enqueued"]}
    if mode == "local":
        assert [tick["tick"] for tick in ticks if "retention.purge" in tick["enqueued"]] == ["daily"], domain
        assert "credits.sweep" not in scheduled, domain
        assert local_worker.blocked_reason("retention.purge") is None
        for kind in {"credits.sweep", "ops.signal_sweep", "infra.reconcile", "learning.cycle"}:
            assert "Settings > Capabilities" in local_worker.blocked_reason(kind), (domain, kind)
    else:
        assert {"credits.sweep", "retention.purge"} <= scheduled, domain


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_every_local_background_job_is_described_under_its_switch(scheduler, monkeypatch, domain):
    from app.core import local_worker
    from app.worker.catalog import load_catalogue

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    scheduled = {kind for tick in fire(scheduler)["ticks"] for kind in tick["enqueued"]}
    for kind in (scheduled - {"retention.purge"}) | {"routines.fire", "routines.report"}:
        name = load_catalogue()["kinds"][kind]["local_capability"]
        jobs = local_worker.background_jobs(name)
        assert any(item["kind"] == kind and item["description"] and item["schedule"] for item in jobs), (domain, kind)
    assert all(item["kind"] != "knowledge.loot_drop_refresh" for item in local_worker.background_jobs("background_work"))


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_local_maintenance_keeps_recovery_without_hosted_rewards(scheduler, monkeypatch, domain, mode):
    from app.builder import orchestration
    from app.consumer import referrals
    from app.credits import reserve
    from app.imports import cleanup
    from app.inbox import store

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    calls = []
    monkeypatch.setattr(reserve, "sweep_expired", lambda: calls.append("holds"))
    monkeypatch.setattr(orchestration, "enqueue_orphaned_watches", lambda: calls.append("watches"))
    monkeypatch.setattr(cleanup, "sweep", lambda: calls.append("imports"))
    monkeypatch.setattr(store, "cleanup", lambda: calls.append("notices"))
    monkeypatch.setattr(referrals, "sweep_activations", lambda session: calls.append("rewards"), raising=False)
    scheduler.maintenance()
    assert "holds" in calls, domain
    if mode == "local":
        assert {"watches", "imports", "notices"} <= set(calls), domain
        assert "rewards" not in calls, domain
    else:
        assert calls == ["rewards", "holds"], domain


def test_windows_worker_health_is_nondestructive_and_checks_identity(scheduler, monkeypatch, tmp_path):
    import json
    from app.worker import health

    monkeypatch.setenv("COS_WORKER_HEALTH_PATH", str(tmp_path / "worker-health.json"))
    monkeypatch.setattr(health.sys, "platform", "win32")
    monkeypatch.setattr(health, "_windows_process_started_at", lambda pid: 123, raising=False)
    monkeypatch.setattr(health.os, "kill", lambda *args: pytest.fail("Windows health must not send a signal"))
    health.write("running")
    assert health.healthy()
    data = json.loads(health.path().read_text())
    data.pop("process_started_at")
    health.path().write_text(json.dumps(data))
    assert not health.healthy()
    health.write("running")
    monkeypatch.setattr(health, "_windows_process_started_at", lambda pid: 456)
    assert not health.healthy()
    monkeypatch.setattr(health, "_windows_process_started_at", lambda pid: None)
    assert not health.healthy()


def test_worker_health_default_path_uses_platform_temporary_directory(scheduler, monkeypatch, tmp_path):
    import tempfile
    from app.worker import health

    monkeypatch.delenv("COS_WORKER_HEALTH_PATH", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    assert health.path() == tmp_path / "supymem-worker-health.json"


@pytest.mark.parametrize("wait_result", [0, 258, 4294967295])
def test_windows_health_queries_liveness_and_closes_its_handle(scheduler, monkeypatch, wait_result):
    import ctypes
    from app.worker import health

    calls = []

    class NativeCall:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args):
            return self.function(*args)

    def process_times(handle, created, *unused):
        assert handle == 42
        created._obj.dwLowDateTime = 123
        created._obj.dwHighDateTime = 1
        return True

    native = SimpleNamespace(
        OpenProcess=NativeCall(lambda access, inherit, pid: calls.append((access, inherit, pid)) or 42),
        WaitForSingleObject=NativeCall(lambda handle, timeout: wait_result),
        GetProcessTimes=NativeCall(process_times),
        CloseHandle=NativeCall(lambda handle: calls.append(("close", handle)) or True),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: native, raising=False)
    assert health._windows_process_started_at(77) == ((1 << 32) | 123 if wait_result == 258 else None)
    assert calls == [(0x1000 | 0x100000, False, 77), ("close", 42)]
    native.OpenProcess = NativeCall(lambda *args: None)
    assert health._windows_process_started_at(77) is None


def test_tick_transaction_belongs_to_scheduler_service(scheduler):
    from app.api import internal_tick

    assert internal_tick.dispatch_tick is scheduler.module.dispatch_tick


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("mode", [None, "local"])
def test_explicit_local_mode_overrides_hosted_worker_gates(scheduler, monkeypatch, domain, mode):
    from fastapi import HTTPException
    from app.core.jobs import FatalError
    from app.worker import handlers

    monkeypatch.setenv("APP_ENV", "production")
    if mode is None:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
        assert handlers.hosted_mode(), domain
        with pytest.raises(FatalError, match="capability_unavailable"):
            handlers.require_capability("idea.recon")
        with pytest.raises(HTTPException) as failure:
            handlers.require_capability_http("idea.recon")
        assert failure.value.status_code == 503
    else:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        assert not handlers.hosted_mode(), domain
        handlers.require_capability("idea.recon")
        handlers.require_capability_http("idea.recon")


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("kind", ["knowledge.loot_drop_refresh", "suggestions.generate", "inbox.cos_brief.tick",
    "gtm.gmail.read", "autopilot.tick", "experiment.beliefs.tick", "experiment.evaluate", "inbox.briefing.tick"])
def test_local_ticks_do_not_start_unrequested_work(scheduler, monkeypatch, domain, kind):
    import asyncio
    from app.core import local_worker
    from app.credits import metered
    from app.db.models import Person, Venture, Workspace, WorkspaceMember
    from app.inbox import store, weekly
    from app.research.prior_art import loot_drop
    from app.suggestions import service as suggestions
    from app.autopilot import outreach
    from app.db.supymem_models import AutopilotSetting
    from app.tenancy import bind
    from app.worker import handlers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with scheduler.factory() as session:
        session.add(Person(id="unrequested-owner", display_name="You", created_at=NOW))
        session.add(Workspace(id="unrequested-workspace", owner_id="unrequested-owner", name=domain,
            kind="company", entry_mode="idea", created_at=NOW, updated_at=NOW))
        session.add(Venture(id="unrequested-venture", workspace_id="unrequested-workspace", name=domain,
            raw_idea=domain, stage="draft", created_at=NOW, updated_at=NOW))
        session.add(WorkspaceMember(id="unrequested-owner-member", workspace_id="unrequested-workspace",
            person_id="unrequested-owner", role="owner", can_approve=1, created_at=NOW))
        session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail(f"Unrequested {kind} started for {domain}")

    monkeypatch.setattr(loot_drop, "run_refresh", forbidden)
    monkeypatch.setattr(suggestions, "generate_all", forbidden)
    monkeypatch.setattr(suggestions, "generate", forbidden)
    monkeypatch.setattr(weekly, "run_founder", forbidden)
    monkeypatch.setattr(outreach, "poll_replies", forbidden)
    monkeypatch.setattr(metered, "call", forbidden)
    monkeypatch.setattr(metered, "charge_sync", forbidden)
    monkeypatch.setattr(local_worker, "blocked_reason", lambda kind: None)
    handlers.install_default_handlers()
    payload = {"tick": "5m", "window": "current", "founder_id": "unrequested-owner",
        "workspace_id": "unrequested-workspace", "venture_id": "unrequested-venture"}
    with bind(workspace_id="unrequested-workspace", venture_id="unrequested-venture", actor="unrequested-owner"):
        asyncio.run(handlers.dispatch(kind, payload))
    if kind not in {"knowledge.loot_drop_refresh", "suggestions.generate", "inbox.cos_brief.tick"}:
        return
    calls = []
    monkeypatch.setattr(loot_drop, "run_refresh", lambda payload: calls.append(kind))
    monkeypatch.setattr(suggestions, "generate", lambda identifier: calls.append(kind))
    monkeypatch.setattr(weekly, "run_founder", lambda *args, **kwargs: calls.append(kind))
    with scheduler.factory() as session:
        session.add(AutopilotSetting(venture_id="unrequested-venture", god_mode=True, updated_at=NOW))
        row, values = store.settings(session, "unrequested-owner", "unrequested-workspace")
        store.save(row, {**values, "cos_brief": {"weekday": 0, "time": "08:30"}})
        session.commit()
    if kind == "knowledge.loot_drop_refresh":
        payload = {}
    else:
        payload = {**payload, "window": "next"}
    if kind in {"knowledge.loot_drop_refresh", "suggestions.generate", "inbox.cos_brief.tick"}:
        with bind(workspace_id="unrequested-workspace", venture_id="unrequested-venture", actor="unrequested-owner"):
            asyncio.run(handlers.dispatch(kind, payload))
        assert calls == [kind]


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_local_reply_polling_requires_an_original_sent_invitation(scheduler, monkeypatch, domain):
    import asyncio
    from contextlib import nullcontext
    from app.autopilot import authority, outreach
    from app.core import local_worker
    from app.tenancy import Scope

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_worker, "_targets", lambda payload: [domain])
    monkeypatch.setattr(authority, "founder_scope", lambda identifier, **kwargs: nullcontext(Scope("owned", identifier, "owner")))
    sent, calls = [], []
    monkeypatch.setattr(outreach, "drafts", lambda identifier, *states: list(sent))

    async def poll(identifier):
        calls.append(identifier)

    monkeypatch.setattr(outreach, "poll_replies", poll)
    asyncio.run(local_worker.read_replies({}))
    assert calls == []
    sent.append({"state": "sent", "_actor_subject": "owner"})
    asyncio.run(local_worker.read_replies({}))
    assert calls == [domain]


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("mode", [None, "local"])
def test_scheduler_only_fires_saved_owner_requests(scheduler, monkeypatch, domain, mode):
    import json
    from app.db.models import Job, Person, Venture, Workspace
    from app.db.task_models import Task, TaskRun
    from app.tasks.contracts import Contract

    if mode is None:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    else:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setattr(scheduler.service, "fire_due", scheduler.fire_due)
    assert scheduler.worker._run_scheduler()["tasks"] == []
    with scheduler.factory() as session:
        assert session.scalar(select(Task.id)) is None
        assert session.scalar(select(TaskRun.id)) is None
        session.add(Person(id="requested-owner", display_name="You", created_at=NOW))
        session.add(Workspace(id="requested-workspace", owner_id="requested-owner", name=domain,
            kind="company", entry_mode="idea", created_at=NOW, updated_at=NOW))
        session.add(Venture(id="requested-venture", workspace_id="requested-workspace", name=domain,
            raw_idea=domain, stage="draft", created_at=NOW, updated_at=NOW))
        session.flush()
        task = scheduler.service.create(session, workspace_id="requested-workspace", venture_id="requested-venture",
            owner_person_id="requested-owner", start=False, contract=Contract(title="Summarize saved signups",
                goal=f"Summarize saved signups for {domain}", kind="scheduled",
                trigger={"rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0", "tz": "UTC"},
                steps=[{"title": "Summarize", "skill": "tasks.waitlist_summary"}]))
        task.next_fire_at = NOW.replace(tzinfo=None)
        task_id = task.id
        session.commit()
    result = scheduler.worker._run_scheduler()
    assert len(result["tasks"]) == 1
    with scheduler.factory() as session:
        run = session.get(TaskRun, result["tasks"][0])
        assert run.task_id == task_id
        job = session.get(Job, run.control_job_id)
        assert (job.workspace_id, job.venture_id) == ("requested-workspace", "requested-venture")
        assert json.loads(job.payload_json)["_actor_subject"] == "requested-owner"
    assert scheduler.worker._run_scheduler()["tasks"] == []


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("reader_name", ["firecrawl", "duckduckgo"])
def test_local_ticks_run_owner_work_by_capability(scheduler, monkeypatch, domain, reader_name):
    from contextlib import nullcontext
    import httpx
    from app.credits import metered
    from app.research import local_providers
    from app.research.prior_art import loot_drop
    from app.tasks.scheduler import dispatch_tick

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    result = fire(scheduler)
    kinds = {kind for tick in result["ticks"] for kind in tick["enqueued"]}
    assert kinds == {
        "retention.purge",
        "gtm.gmail.read", "gtm.journey.maintain",
        "autopilot.tick", "experiment.beliefs.tick", "experiment.evaluate",
        "suggestions.generate", "inbox.briefing.tick", "inbox.cos_brief.tick",
    }, domain
    assert result["maintenance_due"]
    assert dispatch_tick("weekly")["enqueued"] == []
    original_refresh = loot_drop.refresh
    monkeypatch.setattr(loot_drop, "refresh", lambda: (_ for _ in ()).throw(OSError(domain)))
    with pytest.raises(OSError):
        loot_drop.run_refresh({})
    monkeypatch.setattr(loot_drop.httpx, "Client", lambda **kwargs: pytest.fail("Direct external reader"))
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: None)
    with pytest.raises(ValueError, match="Retry with another public URL"):
        loot_drop.Reader()
    calls = []

    async def post(client, path, body):
        calls.append(body["url"])
        return httpx.Response(200, json={"data": {"rawHtml": "User-agent: *\nAllow: /" if body["url"].endswith("robots.txt") else domain,
            "metadata": {"sourceURL": body["url"], "statusCode": 200}}})

    async def metered_read(call, **kwargs):
        assert kwargs["purpose"] == "research.fetch"
        assert kwargs["estimate_micro"] == (0 if reader_name == "duckduckgo" else metered.price_micro("firecrawl_scrape"))
        return await call()

    monkeypatch.setattr(local_providers, "scrape_provider", lambda: SimpleNamespace(name=reader_name, _firecrawl_post=post))
    monkeypatch.setattr(metered, "call", metered_read)
    monkeypatch.setattr(loot_drop.time, "sleep", lambda seconds: None)
    with loot_drop.Reader() as reader:
        assert reader.get(loot_drop.ROOT + "/startup/1-test").text == domain
    assert len(calls) == 2

    def failed_read(url, **kwargs):
        if url.endswith("sitemap.xml"):
            return httpx.Response(200, text=f"<urlset><url><loc>{loot_drop.ROOT}/startup/1-test</loc></url></urlset>")
        raise OSError(domain)

    monkeypatch.setattr(loot_drop, "Reader", lambda: nullcontext(SimpleNamespace(get=failed_read)))
    with pytest.raises(OSError):
        original_refresh(limit=1)


def test_enqueued_jobs_use_the_scheduler_clock_and_can_be_leased(scheduler):
    from app.core.jobs import lease
    from app.db.models import Job
    from core.clock import as_utc

    result = fire(scheduler)
    with scheduler.factory() as session:
        rows = session.scalars(select(Job).where(Job.id.in_(result["ids"]))).all()
        assert rows
        assert all(as_utc(row.created_at) == NOW and as_utc(row.scheduled_for) == NOW for row in rows)
        claimed = lease(session, "synthetic-scheduler-worker", kinds=[rows[0].kind], now=NOW)
        # Several due jobs may share a kind; lease order among them is not part of the contract.
        assert len(claimed) == 1 and claimed[0].id in {row.id for row in rows if row.kind == rows[0].kind}


@pytest.mark.parametrize("mode", [None, "local"])
def test_all_maintenance_intervals_fire_and_survive_restart(scheduler, monkeypatch, mode):
    from app.db.task_models import MaintenanceSchedule

    if mode is None:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    else:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
    first = fire(scheduler)
    assert {tick["tick"] for tick in first["ticks"]} == set(scheduler.module.INTERVALS) - {"maintenance"}
    if mode == "local":
        assert {kind for tick in first["ticks"] for kind in tick["enqueued"]} >= {
            "gtm.gmail.read", "experiment.evaluate", "suggestions.generate"}
    else:
        assert {kind for tick in first["ticks"] for kind in tick["enqueued"]} >= {
            "ops.signal_sweep", "ops.brain_cycle", "retention.purge", "ops.slate",
            "experiment.evaluate", "learning.cycle", "credits.sweep", "autopilot.tick", "lead_library.sweep",
        }
    assert first["maintenance_due"]
    with scheduler.factory() as session:
        for row in session.scalars(select(MaintenanceSchedule)):
            assert row.next_fire_at > NOW.replace(tzinfo=None)
    # A new session/process reads the saved timestamp; polling is not a fire.
    second = fire(scheduler, NOW + timedelta(seconds=30))
    assert second["ticks"] == [] and not second["maintenance_due"]
    third = fire(scheduler, NOW + timedelta(minutes=1))
    assert {tick["tick"] for tick in third["ticks"]} == {"5m"}
    assert third["maintenance_due"]


def test_schedule_advance_and_job_enqueue_rollback_together(scheduler):
    from app.db.models import Job
    from app.db.task_models import MaintenanceSchedule

    fire(scheduler)
    with scheduler.factory() as session:
        before = session.get(MaintenanceSchedule, "5m").next_fire_at
        initial_jobs = len(session.scalars(select(Job)).all())
    with scheduler.factory() as session:
        rolled_back = scheduler.module.run_due(session, now=NOW + timedelta(minutes=1))
        assert rolled_back["ids"]
        session.rollback()
    with scheduler.factory() as session:
        assert session.get(MaintenanceSchedule, "5m").next_fire_at == before
        assert len(session.scalars(select(Job)).all()) == initial_jobs
    assert fire(scheduler, NOW + timedelta(minutes=1))["ids"]


def test_earlier_clock_does_not_refire_claimed_maintenance_windows(scheduler):
    from app.db.task_models import MaintenanceSchedule

    first = fire(scheduler, NOW + timedelta(minutes=1))
    assert first["maintenance_due"] and len(first["ticks"]) == 7
    with scheduler.factory() as session:
        advances = {row.tick: row.next_fire_at for row in session.scalars(select(MaintenanceSchedule))}
    for _ in range(2):
        result = fire(scheduler)
        assert not result["maintenance_due"] and result["ticks"] == [] and result["ids"] == []
    with scheduler.factory() as session:
        assert {row.tick: row.next_fire_at for row in session.scalars(select(MaintenanceSchedule))} == advances


def test_downtime_coalesces_to_latest_window_without_catchup_storm(scheduler):
    from app.db.task_models import MaintenanceSchedule

    fire(scheduler)
    later = NOW + timedelta(days=8)
    result = fire(scheduler, later)
    assert len(result["ticks"]) == 7
    assert next(tick["window"] for tick in result["ticks"] if tick["tick"] == "5m") == "20261016T1200"
    with scheduler.factory() as session:
        assert session.get(MaintenanceSchedule, "weekly").next_fire_at == datetime(2026, 10, 19)


def test_signed_tick_and_scheduler_share_dedupe_window(scheduler, monkeypatch):
    from app.api import internal_tick
    from app.db.models import Job

    monkeypatch.setattr(scheduler.module, "publish_wake", lambda: None)
    manual = internal_tick.dispatch_tick("5m", now=NOW)
    result = fire(scheduler)
    assert set(manual["ids"]) <= set(result["ids"])
    with scheduler.factory() as session:
        for kind in manual["enqueued"]:
            assert len(session.scalars(select(Job).where(Job.dedupe_key == f"{kind}:{manual['window']}")).all()) == 1


@pytest.mark.parametrize("mode", [None, "local"])
def test_specialist_leader_still_enqueues_global_maintenance(scheduler, monkeypatch, mode):
    from app.api import internal_tick

    if mode:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
    else:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("WORKER_KINDS", "credits.sweep")
    result = fire(scheduler)
    if mode == "local":
        assert {kind for tick in result["ticks"] for kind in tick["enqueued"]} >= {
            "gtm.gmail.read", "suggestions.generate"}
    else:
        assert {kind for tick in result["ticks"] for kind in tick["enqueued"]} >= {"credits.sweep", "ops.signal_sweep", "learning.cycle"}
    monkeypatch.setattr(scheduler.module, "publish_wake", lambda: None)
    dispatched = internal_tick.dispatch_tick("5m", now=NOW)["enqueued"]
    if mode == "local":
        assert {"gtm.gmail.read", "autopilot.tick"} <= set(dispatched)
    else:
        assert dispatched == ["credits.sweep"]


def test_follower_cannot_claim_schedule_or_fire_tasks(scheduler, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.service, "fire_due", lambda *args, **kwargs: calls.append(True))

    class Follower:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalar(self, statement, params):
            assert "pg_try_advisory_xact_lock" in str(statement)
            assert params["key"] == "task-maintenance-scheduler"
            return False

    result = scheduler.module.run_due(Follower(), now=NOW)
    assert result == {"ticks": [], "ids": [], "maintenance_due": False, "tasks": []}
    assert calls == []


def test_worker_commits_before_safety_sweep_and_runs_approval_hooks(scheduler, monkeypatch):
    from app.approvals import service as approvals
    from app.credits import reserve
    from app.worker import main

    calls = []
    monkeypatch.setattr(main, "_run_maintenance", scheduler.maintenance)
    # The safety functions own their sessions. The database pass must already
    # be committed when they run, or SQLite's write lock makes this fail.
    def sweep():
        from app.db.task_models import MaintenanceSchedule
        with scheduler.factory() as session:
            row = session.get(MaintenanceSchedule, "maintenance")
            assert row.next_fire_at == datetime(2026, 10, 8, 12, 5)
        calls.append("credits")

    monkeypatch.setattr(reserve, "sweep_expired", sweep)
    monkeypatch.setattr(approvals, "expire_due", lambda: calls.append("expire"))
    monkeypatch.setattr(approvals, "sweep_notifications", lambda: calls.append("notify"))
    main._run_scheduler()
    main._run_scheduler()
    assert calls == ["credits", "expire", "notify"]


def test_credit_sweep_failure_does_not_skip_approval_maintenance(scheduler, monkeypatch):
    from app.approvals import service as approvals
    from app.build import device_orders
    from app.credits import reserve

    calls = []
    def fail():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(reserve, "sweep_expired", fail)
    monkeypatch.setattr(device_orders, "sweep_device_leases", lambda: calls.append("devices"))
    monkeypatch.setattr(approvals, "expire_due", lambda: calls.append("expire"))
    monkeypatch.setattr(approvals, "sweep_notifications", lambda: calls.append("notify"))
    scheduler.maintenance()
    assert calls == ["devices", "expire", "notify"]


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("name,kinds", [
    ("read_replies", {"gtm.gmail.read"}),
    ("send_email", {"gtm.journey.maintain", "gtm.outreach.retry"}),
    ("run_for_me_experiments", {"autopilot.tick", "experiment.beliefs.tick", "experiment.evaluate"}),
    ("background_work", {"suggestions.generate", "inbox.briefing.tick", "inbox.cos_brief.tick", "knowledge.loot_drop_refresh"}),
    ("routines", {"routines.fire", "routines.report"}),
])
def test_local_capability_off_blocks_dispatch(scheduler, monkeypatch, domain, name, kinds):
    from app.core import local_worker

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_worker, "capability_enabled", lambda capability: capability != name)
    for kind in kinds:
        assert local_worker.blocked_reason(kind), (domain, kind)
    result = fire(scheduler)
    dispatched = {kind for tick in result["ticks"] for kind in tick["enqueued"]}
    assert dispatched.isdisjoint(kinds), domain
    assert "credits.sweep" not in dispatched and "retention.purge" in dispatched
    calls = []
    monkeypatch.setattr(scheduler.service, "fire_due", lambda *args, **kwargs: calls.append(True) or [])
    scheduler.worker._run_scheduler()
    assert bool(calls) == (name != "routines"), domain


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_local_timer_catches_up_once_after_sleep(scheduler, monkeypatch, domain):
    import asyncio

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    stop = asyncio.Event()
    moments = iter((NOW, NOW + timedelta(days=8), NOW + timedelta(days=8)))
    results = []
    delays = []

    def run():
        try:
            results.append(fire(scheduler, next(moments)))
        except Exception:
            stop.set()
            raise
        if len(results) == 3:
            stop.set()

    def jitter(minimum, maximum):
        assert 0 < minimum < maximum
        delays.append((minimum, maximum))
        return 0.001

    monkeypatch.setattr(scheduler.worker, "_run_scheduler", run)
    monkeypatch.setattr(scheduler.worker.random, "uniform", jitter)
    asyncio.run(scheduler.worker._scheduler_loop(stop))
    assert len(delays) == 2, domain
    assert len(results[1]["ticks"]) == 7
    assert results[2]["ticks"] == []
    assert next(tick["window"] for tick in results[1]["ticks"] if tick["tick"] == "5m") == "20261016T1200"


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_monthly_spend_cap_stops_work_not_safety(scheduler, monkeypatch, domain):
    from app.core import local_secrets, local_worker
    from app.credits import local_meter

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_worker, "spend_available", scheduler.spend)
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {"monthly_spend_cap": "1.00"}})
    with local_meter._database() as connection:
        connection.execute("INSERT INTO calls (call_id, month, purpose, estimate_usd_micro, status) VALUES (?, ?, ?, ?, ?)",
            ("cap-test", local_meter._month(), domain, 1_000_000, "pending"))
    calls = []
    monkeypatch.setattr(scheduler.service, "fire_due", lambda *args, **kwargs: calls.append(True))
    result = scheduler.worker._run_scheduler()
    assert {kind for tick in result["ticks"] for kind in tick["enqueued"]} == {"retention.purge"}, domain
    assert local_worker.blocked_reason("retention.purge") is None
    assert result["maintenance_due"] and calls == []
    assert "Keys and connections" in local_worker.blocked_reason("suggestions.generate")


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_worker_readiness_requires_a_recent_loop(scheduler, monkeypatch, domain):
    monkeypatch.setattr(scheduler.worker.time, "time", lambda: 100.0)
    monkeypatch.setattr(scheduler.worker, "_last_loop_at", None)
    payload, status = scheduler.worker._readiness(domain)
    assert status == 503 and payload["ready"] is False
    monkeypatch.setattr(scheduler.worker, "_last_loop_at", 99.0)
    payload, status = scheduler.worker._readiness(domain)
    assert status == 200 and payload["last_loop_at"] == 99.0
    monkeypatch.setattr(scheduler.worker.time, "time", lambda: 200.0)
    assert scheduler.worker._readiness(domain)[1] == 503


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_local_followups_only_advance_owner_started_campaigns(scheduler, monkeypatch, domain):
    import asyncio
    from contextlib import nullcontext
    from app.autopilot import authority, guard, outreach
    from app.core import local_worker
    from app.gtm import journey
    from app.tenancy import Scope
    from app.ventures import lifecycle

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_worker, "_targets", lambda payload: ["stopped", "started", "paused"])
    monkeypatch.setattr(authority, "founder_scope", lambda identifier, **kwargs: nullcontext(Scope(domain, identifier, "owner")))
    monkeypatch.setattr(guard, "get_settings", lambda identifier: {"allow_send": identifier != "stopped"})
    monkeypatch.setattr(lifecycle, "is_paused", lambda identifier: identifier == "paused")
    monkeypatch.setattr(journey, "prepare", lambda *args, **kwargs: pytest.fail("A recurring send must not start discovery"))
    calls = []
    monkeypatch.setattr(outreach, "schedule_followups", lambda identifier: calls.append(("followups", identifier)))

    async def send(workspace_id, venture_id, actor):
        assert workspace_id == domain and actor == "owner"
        calls.append(("send", venture_id))

    monkeypatch.setattr(journey, "auto_send", send)
    asyncio.run(local_worker.maintain_campaigns({}))
    assert calls == [("followups", "started"), ("send", "started")]


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
@pytest.mark.parametrize("name,kind", [("background_work", "suggestions.generate"),
    ("background_work", "gtm.journey.maintain"), ("send_email", "gtm.journey.maintain"),
    ("read_replies", "gtm.gmail.read"), ("routines", "routines.fire")])
def test_already_queued_recurring_work_waits_when_capability_turns_off(scheduler, monkeypatch, domain, name, kind):
    import asyncio
    from app.core import jobs, local_worker
    from app.core.local_capabilities import blocked_message
    from app.core.local_secrets import update_settings
    from app.db.models import Job

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("capabilities", {name: False}, {})
    monkeypatch.setattr(local_worker, "capability_enabled", lambda capability: capability != name)
    monkeypatch.setattr(scheduler.worker, "dispatch", lambda *args: pytest.fail("Switched-off work was dispatched"))
    with scheduler.factory() as session:
        identifier = jobs.enqueue(session, kind, {"domain": domain})
        claimed = jobs.lease(session, "local-owner-worker", kinds=[kind], now=NOW)
        session.commit()
    assert len(claimed) == 1
    asyncio.run(scheduler.worker._run_one(scheduler.factory, claimed[0], {(identifier, claimed[0].attempts)}))
    with scheduler.factory() as session:
        row = session.get(Job, identifier)
        assert row.status == "retry_wait" and row.attempts == 0
        assert row.run_after == (NOW + timedelta(minutes=1)).replace(tzinfo=None)
        assert row.last_error == blocked_message(name)


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_local_recurring_targets_cannot_widen_current_scope(scheduler, domain):
    from app.core import local_worker
    from app.db.models import Person, Venture, Workspace
    from app.tenancy import bind

    with scheduler.factory() as session:
        for index in range(2):
            owner, workspace, venture = (f"background-{field}-{index}" for field in ("owner", "workspace", "venture"))
            session.add(Person(id=owner, display_name="You", created_at=NOW))
            session.add(Workspace(id=workspace, owner_id=owner, name=domain, kind="company", entry_mode="idea",
                created_at=NOW, updated_at=NOW))
            session.add(Venture(id=venture, workspace_id=workspace, name=domain, raw_idea=domain, stage="draft",
                created_at=NOW, updated_at=NOW))
        session.commit()
    with bind(workspace_id="background-workspace-0", venture_id="", actor="background-owner-0"):
        assert local_worker._targets({}) == ["background-venture-0"]
        assert local_worker._targets({"founder_id": "background-owner-1"}) == []
    with bind(workspace_id="none", venture_id="", actor="system"):
        assert local_worker._targets({"founder_id": "background-owner-1"}) == ["background-venture-1"]


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_capability_fallback_reads_saved_mailbox_settings(scheduler, monkeypatch, domain):
    import sys
    from app.core import local_secrets, local_worker

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", None)
    monkeypatch.setattr(local_worker, "capability_enabled", scheduler.capabilities)
    values = {"host": domain, "imap_host": domain, "user": "owner@example.test", "postal_address": domain}
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": values})
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: "test-only-password")
    assert local_worker.capability_enabled("read_replies")
    assert local_worker.capability_enabled("send_email")
    values.pop("imap_host")
    assert not local_worker.capability_enabled("read_replies")
    values.pop("postal_address")
    assert not local_worker.capability_enabled("send_email")


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_local_evaluation_uses_scoped_real_readouts_not_synthetic_observations(scheduler, monkeypatch, domain):
    from contextlib import nullcontext
    from app.autopilot import authority
    from app.core import local_worker
    from app.experiments import ledger, service
    from app.tenancy import Scope

    monkeypatch.setattr(local_worker, "_targets", lambda payload: [domain])
    monkeypatch.setattr(authority, "founder_scope", lambda identifier, **kwargs: nullcontext(Scope("owned", identifier, "owner")))
    called = []
    monkeypatch.setattr(service, "run_evaluate", lambda payload: called.append(payload))
    monkeypatch.setattr(ledger, "autopilot_enabled", lambda: False)
    local_worker.evaluate_experiments({"window": "current"})
    assert called == []
    monkeypatch.setattr(ledger, "autopilot_enabled", lambda: True)
    local_worker.evaluate_experiments({"window": "current"})
    assert called == [{"window": "current", "venture_id": domain, "workspace_id": "owned"}]


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_embedded_worker_runs_recurring_work_without_an_external_tick(scheduler, monkeypatch, domain):
    import asyncio

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(scheduler.worker, "_recover_orphaned_builds", lambda: 0)
    monkeypatch.setattr(scheduler.worker, "enabled_kinds", lambda: {"suggestions.generate"})
    monkeypatch.setattr(scheduler.worker, "POLL_S", 0.005)
    called = []

    async def exercise():
        stop = asyncio.Event()

        async def dispatch(kind, payload):
            assert scheduler.worker._readiness(domain)[1] == 200
            assert kind == "suggestions.generate" and payload["tick"] == "hourly"
            called.append(kind)
            stop.set()

        monkeypatch.setattr(scheduler.worker, "dispatch", dispatch)
        await asyncio.wait_for(scheduler.worker._run_worker(stop=stop), timeout=5)

    asyncio.run(exercise())
    assert called == ["suggestions.generate"], domain
    assert scheduler.worker._readiness(domain)[1] == 503
