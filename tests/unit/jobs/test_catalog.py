"""Registered handlers ⊆ catalogue; later kinds listed but not enabled."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.jobs.test_lease import factory
from tests.unit.memory.test_capture_conflicts_v1 import founders, facts

load_company_os_app()

from app.worker.catalog import all_kind_names, enabled_kinds, load_catalogue, load_trigger_policy, stage2_kinds
from app.worker.handlers import HANDLERS, install_default_handlers


def test_catalogue_lists_runtime_and_later_kinds() -> None:
    from app.core.jobs import JOB_CLASSES, job_class

    names = all_kind_names()
    assert names == JOB_CLASSES.keys() | {"browse.web"}
    assert job_class("browse.web") == "bulk"
    assert job_class("new.unmapped_kind") == "bulk"
    assert "credits.sweep" in names
    assert "ops.slate" in names
    assert "build.run" in names
    assert "research.deep" in names
    assert "chat.respond" in names


def test_stage2_enabled_is_subset_of_catalogue() -> None:
    load_catalogue.cache_clear()
    load_trigger_policy.cache_clear()
    enabled = enabled_kinds()
    assert enabled <= all_kind_names()
    assert enabled <= stage2_kinds()
    assert "credits.sweep" in enabled
    assert "ops.signal_sweep" in enabled
    assert "build.run" in enabled
    assert "research.deep" in enabled
    assert "idea.recon" in enabled


@pytest.mark.parametrize("domain", ["warehouse teams", "weekend gardeners"])
def test_local_catalog_ignores_hosted_worker_kind_filters(monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("WORKER_KINDS", "credits.sweep")
    assert {"gtm.gmail.read", "gtm.journey.maintain", "routines.fire", "experiment.evaluate",
        "suggestions.generate"} <= enabled_kinds(), domain


def test_registered_handlers_subset_of_catalogue_and_enabled() -> None:
    load_catalogue.cache_clear()
    load_trigger_policy.cache_clear()
    install_default_handlers()
    assert set(HANDLERS) <= all_kind_names()
    assert set(HANDLERS) <= enabled_kinds()
    assert "build.run" in HANDLERS
    assert "idea.recon" in HANDLERS


def test_memory_capture_registered_bulk_lease_preserves_each_founders_private_chat_and_voice(founders, monkeypatch):
    import asyncio
    from app.analytics.feature_catalog import JOB_FEATURES, job_feature
    from app.chat.store import append_message
    from app.core.jobs import job_class, lease
    from app.worker import catalog, handlers

    suite = founders
    suite.flags.pop("memory_conflicts_v1")
    monkeypatch.delenv("WORKER_KINDS", raising=False)
    catalog.load_catalogue.cache_clear()
    handlers.install_default_handlers()
    assert "memory.extract" in catalog.enabled_kinds() and handlers.HANDLERS["memory.extract"] is suite.extraction.run_capture
    assert job_class("memory.extract") == "bulk"
    assert job_feature("memory.extract") == JOB_FEATURES["memory.extract"] == "chat"
    called = []
    monkeypatch.setattr(suite.extraction, "capture_model_decisions", lambda payload: called.append(payload) or [])
    saved = {}
    for domain in suite.domains:
        with suite.bound(domain):
            saved[domain.actor] = {append_message(domain.venture, role="user", kind=kind,
                body_md=f"Our audience is {domain.audience}.", idempotency_key="worker_" + kind)["id"]
                for kind in ("text", "voice_transcript")}
    with suite.d.factory() as session:
        assert lease(session, "interactive_worker", kinds=["memory.extract"], job_classes=["interactive"], n=4) == []
        claimed = lease(session, "memory_worker", kinds=["memory.extract"], job_classes=["bulk"], n=4)
        session.commit()
    assert len(claimed) == 4
    for job in claimed:
        domain = next(domain for domain in suite.domains if domain.actor == job.payload["_actor_subject"])
        assert job.payload["_chat_conversation"]["conversation_id"] == domain.chat
        assert job.payload["source_message_id"] in saved[domain.actor]
        with suite.bound(domain):
            asyncio.run(handlers.dispatch(job.kind, {**job.payload, "_job_id": job.id,
                "_job_attempt": job.attempts, "_worker_id": job.worker_id}))
            with suite.d.factory() as session:
                assert {row.person_id for row in suite.d.api.query_facts(session, venture_id=domain.venture)} == {domain.actor}
    assert len(called) == 4 and {payload["_actor_subject"] for payload in called} == set(saved)


def test_godmode_driver_drains_founder_jobs_before_reading_progress(factory, tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.autopilot import service
    from app.core import scheduled
    from app.db.models import Job, Person, Workspace
    from app.state import set_factory
    from app.tenancy import bind, current
    from app.ventures import next_step
    from app.worker import handlers
    from app.core.local_mode import is_local
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from core.clock import utcnow
    from tests.integration.test_postgres_godmode_simulation import Runner

    domains = {LOCAL_PERSON_ID if is_local() else "driver-first": "Field inspection teams",
               "driver-second": "Family recipe groups"}
    now, progress = utcnow(), []
    set_factory(factory)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    if is_local():
        from app.core.local_secrets import update_settings
        from app.core.providers_local import save_settings

        save_settings(provider="openai", api_key="synthetic-worker-key", models={"chat": "gpt-6.1-sol"})
        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    monkeypatch.setenv("WORKER_KINDS", "autopilot.tick")
    monkeypatch.setenv("SCHEDULED_FANOUT_SPREAD_SECONDS", "0")
    monkeypatch.setattr(handlers, "HANDLERS", {})
    monkeypatch.setattr(handlers, "_autopilot_housekeeping", lambda founder_id: None)
    with factory() as session:
        for founder_id, domain in domains.items():
            person = session.get(Person, founder_id)
            if person is None:
                session.add(Person(id=founder_id, display_name=domain, created_at=now))
            else:
                person.display_name = domain
        session.flush()
        for founder_id, domain in domains.items():
            session.add(Workspace(id="workspace-" + founder_id, owner_id=founder_id, kind="company", name=domain, entry_mode="idea",
                created_at=now, updated_at=now))
        session.commit()

    async def advance(founder_id):
        scope = current()
        assert scheduled.FOUNDER_JOB.get() and scope.actor == founder_id
        assert scope.workspace_id == "workspace-" + founder_id
        progress.append(domains[founder_id])

    monkeypatch.setattr(service, "tick_all", advance)
    monkeypatch.setattr(service, "status", lambda venture_id: {"state": "active"})
    monkeypatch.setattr(next_step, "next_step", lambda venture_id: {"key": "continue"})
    monkeypatch.setattr(Runner, "_new", lambda runner: ([], [], []))
    runner = Runner(SimpleNamespace(vid="driver-venture", clock=SimpleNamespace(now=now)))
    with bind(workspace_id="none", venture_id="", actor="system"):
        asyncio.run(runner.tick())
    assert sorted(progress) == sorted(domains.values())
    with factory() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == "autopilot.tick")))
        assert len(jobs) == 2 and all(job.status == "succeeded" and job.attempts == 1 for job in jobs)


@pytest.mark.parametrize("idea", ["Inventory counts for pharmacies", "Trail planning for hikers"])
def test_every_enqueued_kind_has_a_registered_worker_lease(factory, monkeypatch, idea):
    from app.core.jobs import enqueue
    from app.core.local_mode import is_local
    from app.worker import main as worker

    monkeypatch.delenv("WORKER_KINDS", raising=False)
    install_default_handlers()
    enqueued = set(stage2_kinds())
    root = Path(worker.__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        source = path.read_text()
        if "app.core.jobs import" not in source:
            continue
        tree = ast.parse(source)
        names = {alias.asname or alias.name for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module == "app.core.jobs"
                 for alias in node.names if alias.name == "enqueue"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in names:
                continue
            kind = next((keyword.value for keyword in node.keywords if keyword.arg == "kind"),
                        node.args[1] if len(node.args) > 1 else None)
            if isinstance(kind, ast.Attribute) and isinstance(kind.value, ast.Name):
                module = next((f"{statement.module}.{alias.name}" for statement in ast.walk(tree)
                               if isinstance(statement, ast.ImportFrom) and statement.module
                               for alias in statement.names if (alias.asname or alias.name) == kind.value.id), None)
                assert module and module.startswith("app."), f"Unresolved job kind at {path}:{node.lineno}"
                imported = ast.parse(root.joinpath(*module.split(".")[1:]).with_suffix(".py").read_text())
                kind = next((assignment.value for assignment in imported.body if isinstance(assignment, ast.Assign)
                             for target in assignment.targets if isinstance(target, ast.Name) and target.id == kind.attr), None)
            elif isinstance(kind, ast.Name):
                kind = next((assignment.value for assignment in ast.walk(tree) if isinstance(assignment, ast.Assign)
                             and isinstance(assignment.value, ast.Constant)
                             for target in assignment.targets if isinstance(target, ast.Name) and target.id == kind.id), kind)
            if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                enqueued.add(kind.value)
            elif isinstance(kind, ast.BinOp) and isinstance(kind.op, ast.Add) and isinstance(kind.left, ast.Constant):
                candidates = {name for name in stage2_kinds() if name.startswith(kind.left.value)}
                assert candidates, f"Unregistered job prefix at {path}:{node.lineno}"
                enqueued.update(candidates)
            elif isinstance(kind, ast.Name) and kind.id == "kind" and path.relative_to(root).as_posix() == "core/scheduled.py":
                from app.core.scheduled import KINDS, fanout
                with pytest.raises(ValueError, match="unsupported_scheduled_kind"):
                    fanout("not_a_scheduled_kind", {})
                enqueued.update(KINDS)
            else:
                assert isinstance(kind, ast.Name) and kind.id == "kind" and path.relative_to(root).as_posix() in {
                    "api/internal_tick.py", "tasks/scheduler.py", "tasks/service.py",
                }, f"Unresolved job kind at {path}:{node.lineno}"
    assert enqueued <= enabled_kinds()
    assert enqueued <= HANDLERS.keys()
    monkeypatch.setattr(worker, "CONCURRENCY", len(enqueued) + 1)
    monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", str(len(enqueued) + 1))
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    with factory() as session:
        hosted_kinds = {"infra.provision", "qa.run"} if is_local() else set()
        for kind in hosted_kinds:
            with pytest.raises(ValueError, match="connected coding tool"):
                enqueue(session, kind, {"idea": idea}, venture_id="venture_under_test")
        ids = {enqueue(session, kind, {"idea": idea}, venture_id="venture_under_test") for kind in enqueued - hosted_kinds}
        session.commit()
        leased = worker._lease_available(session, "worker", sorted(enabled_kinds()), set(), set(), {"interactive", "bulk"})
        assert {job.id for job in leased} == ids
