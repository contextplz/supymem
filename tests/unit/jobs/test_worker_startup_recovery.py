from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


@pytest.mark.parametrize("domain", ["warehouse.example", "piano.example"])
def test_local_saved_pages_reconcile_before_worker_health(monkeypatch, domain):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from app import state
    from app.builder import orchestration
    from app.core import jobs
    from app.worker import main

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    events = []
    session = SimpleNamespace(commit=lambda: events.append("commit"))
    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr(jobs, "reclaim_stale", lambda current: events.append((domain, current is session)) or 1)
    monkeypatch.setattr(orchestration, "enqueue_orphaned_watches", lambda: events.append("watches") or 0)
    assert main._recover_orphaned_builds() == 0
    assert events == [(domain, True), "commit", "watches"]


@pytest.mark.parametrize("domain", ["warehouse.example", "piano.example"])
@pytest.mark.parametrize("embedded,health_port", [(False, None), (False, "3301"), (True, "3301")])
def test_local_worker_health_starts_only_for_a_configured_standalone_process(monkeypatch, domain, embedded, health_port):
    from app import bootstrap, state
    from app.worker import main

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    if health_port is None:
        monkeypatch.delenv("WORKER_HEALTH_PORT", raising=False)
    else:
        monkeypatch.setenv("WORKER_HEALTH_PORT", health_port)
    monkeypatch.setattr(bootstrap, "bootstrap", list)
    monkeypatch.setattr(state, "db", lambda: None)
    monkeypatch.setattr(main, "install_default_handlers", lambda: None)
    monkeypatch.setattr(main, "_recover_orphaned_builds", lambda: None)
    monkeypatch.setattr(main, "enabled_kinds", frozenset)
    started = []
    monkeypatch.setattr(main, "_start_health", started.append)

    def ready():
        raise RuntimeError(domain)

    monkeypatch.setattr(main, "_Wake", ready)

    async def exercise():
        with pytest.raises(RuntimeError, match=domain):
            await main._run_worker(stop=asyncio.Event() if embedded else None)

    asyncio.run(exercise())
    assert len(started) == int(not embedded and health_port is not None)


@pytest.mark.parametrize("domain", ["warehouse.example", "piano.example"])
def test_local_queue_never_connects_to_redis(monkeypatch, domain):
    import redis
    import redis.asyncio
    from app.core import jobs, local_worker, redis_jobs
    from app.worker import main

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("REDIS_URL", f"redis://{domain}")
    connected = []

    def connect(*args, **kwargs):
        connected.append(True)
        raise AssertionError("Redis must not be used in local mode")

    monkeypatch.setattr(redis.Redis, "from_url", connect)
    monkeypatch.setattr(redis.asyncio, "from_url", connect)
    jobs._wake()
    asyncio.run(main._redis_listener(main._Wake()))
    assert connected == []
    assert redis_jobs.redis_jobs_enabled() is False
    assert local_worker.redis_url() == ""
    monkeypatch.delenv("SUPYMEM_MODE")
    assert redis_jobs.redis_jobs_enabled() is True
    assert local_worker.redis_url() == f"redis://{domain}"


@pytest.mark.parametrize("mode", [None, "hosted"])
def test_single_process_flag_does_not_start_a_hosted_worker(monkeypatch, mode):
    from app.core import local_worker
    from app.worker import main

    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "1")
    if mode is None:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    else:
        monkeypatch.setenv("SUPYMEM_MODE", mode)

    async def forbidden(**kwargs):
        pytest.fail("Hosted API must not start a worker")

    monkeypatch.setattr(main, "run_worker", forbidden)

    async def exercise():
        async with local_worker.lifespan():
            await asyncio.sleep(0)

    asyncio.run(exercise())


@pytest.mark.parametrize("domain", ["warehouse.example", "piano.example"])
@pytest.mark.parametrize("mode,single", [("local", "1"), ("local", "0"), (None, "1")])
def test_api_lifespan_starts_only_opted_in_local_worker(monkeypatch, tmp_path, domain, mode, single):
    from app import bootstrap, main
    from app.chat import rephrase
    from app.worker import main as worker

    if mode:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
    else:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", single)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    events = []
    monkeypatch.setattr(bootstrap, "bootstrap", lambda: events.append("bootstrap") or [])

    async def run_worker(*, stop):
        events.append(domain)
        await stop.wait()
        events.append("stopped")

    async def drain():
        events.append("drained")

    monkeypatch.setattr(worker, "run_worker", run_worker)
    monkeypatch.setattr(rephrase, "drain", drain)

    async def exercise():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
            assert events == (["bootstrap", domain] if mode == "local" and single == "1" else ["bootstrap"])

    asyncio.run(exercise())
    assert events == (["bootstrap", domain, "stopped", "drained"] if mode == "local" and single == "1" else ["bootstrap", "drained"])


def test_worker_startup_recovers_orphaned_build_watches(monkeypatch) -> None:
    from app.builder import orchestration
    from app.worker import main

    calls: list[bool] = []
    monkeypatch.setattr(orchestration, "enqueue_orphaned_watches", lambda: calls.append(True) or 2)

    assert main._recover_orphaned_builds() == 2
    assert calls == [True]


def test_worker_startup_recovery_failure_does_not_block_other_jobs(monkeypatch) -> None:
    from app.builder import orchestration
    from app.worker import main

    def fail() -> int:
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(orchestration, "enqueue_orphaned_watches", fail)

    assert main._recover_orphaned_builds() == 0


def test_database_scheduler_polls_immediately_and_stops(monkeypatch) -> None:
    from app.worker import main

    calls: list[bool] = []
    monkeypatch.setattr(main, "_run_scheduler", lambda: calls.append(True))

    async def exercise() -> None:
        stop = asyncio.Event()
        original = main._run_scheduler

        def run_once() -> None:
            original()
            stop.set()

        monkeypatch.setattr(main, "_run_scheduler", run_once)
        await main._scheduler_loop(stop)

    asyncio.run(exercise())
    assert calls == [True]
