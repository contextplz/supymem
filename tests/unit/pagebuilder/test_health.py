"""Health is durable, expires, and reports one transition through the existing alert path."""
import asyncio
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.unit.pagebuilder.test_api import setup as setup


@pytest.mark.parametrize("minutes", [-1, 13])
def test_renderer_health_is_unknown_then_fresh_then_stale(setup, monkeypatch, minutes):
    from app.pagebuilder import health
    assert health.current()["ready"] is False
    health.record(True)
    assert health.current()["ready"] is True
    now = health.utcnow()
    monkeypatch.setattr(health, "utcnow", lambda: now + timedelta(minutes=minutes))
    assert health.current()["ready"] is False
    assert health.current()["reason"] == "warming"


def test_repeated_failed_probes_alert_only_on_transition(setup, monkeypatch):
    from app.pagebuilder import health
    from app.error_reports import service
    alerts = []
    monkeypatch.setattr(service, "record", lambda **data: alerts.append(data))
    health.record(False)
    health.record(False)
    assert len(alerts) == 1 and alerts[0]["code"] == "pagebuilder_renderer_unavailable"
    health.record(True)
    health.record(False)
    assert len(alerts) == 2


@pytest.mark.parametrize("available", [False, True])
def test_renderer_probe_records_the_actual_preflight_result(setup, monkeypatch, available):
    from app.pagebuilder import health, rendering
    recorded = []
    def preflight():
        if not available:
            raise ValueError("pagebuilder_renderer_unavailable")
    monkeypatch.setattr(rendering, "require_renderer", preflight)
    monkeypatch.setattr(health, "record", recorded.append)
    health.probe()
    assert recorded == [available]


def test_dedicated_worker_records_health_after_renderer_probe_and_successful_poll(setup, monkeypatch):
    from app.llm import founder_provider
    from app.pagebuilder import health, worker
    from app.worker import health as worker_health
    events = []
    session = SimpleNamespace(commit=lambda: events.append("commit"))
    monkeypatch.setattr(worker, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr(worker, "lease", lambda *_args, **_kwargs: events.append("poll") or [])
    monkeypatch.setattr(health, "probe", lambda: events.append("renderer_probe"))
    monkeypatch.setattr(worker_health, "write", events.append)
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    asyncio.run(worker.run_worker(once=True))
    assert events == ["renderer_probe", "poll", "commit", "running"]


def test_dedicated_worker_refreshes_health_with_owned_job_heartbeat(setup, monkeypatch):
    from app.chat.site_actions import page_built
    from app.pagebuilder import worker
    from app.worker import health as worker_health
    events = []
    session = SimpleNamespace(commit=lambda: events.append("commit"))
    job = SimpleNamespace(id="job_heartbeat", worker_id="worker_fixture", attempts=1,
        workspace_id=setup["wid"], venture_id=setup["vid"], payload={"_actor_subject": setup["actor"]})
    ownership = {"worker_id": job.worker_id, "attempt": job.attempts}
    monkeypatch.setattr(worker, "heartbeat", lambda _session, job_id, **owner: events.append(("heartbeat", job_id, owner)))
    monkeypatch.setattr(worker, "complete_job", lambda _session, job_id, **owner: events.append(("complete", job_id, owner)))
    monkeypatch.setattr(worker_health, "write", lambda state: events.append(("health", state)))
    original_wait = asyncio.wait_for
    expired = False
    async def immediate_first_heartbeat(awaitable, timeout):
        nonlocal expired
        if not expired:
            expired = True
            awaitable.close()
            raise TimeoutError
        return await original_wait(awaitable, timeout=timeout)
    async def run_without_provider(function, payload):
        if function is page_built:  # the chat result posted after the job completes
            assert payload == job.id
            return
        assert function is worker.jobs.run and payload["_job_id"] == job.id
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    monkeypatch.setattr(worker.asyncio, "wait_for", immediate_first_heartbeat)
    monkeypatch.setattr(worker.asyncio, "to_thread", run_without_provider)
    asyncio.run(worker.run_one(lambda: nullcontext(session), job))
    assert events == [("heartbeat", job.id, ownership), "commit", ("health", "running"),
                      ("complete", job.id, ownership), "commit"]
