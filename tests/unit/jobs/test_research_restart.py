"""Research restarts keep their original receipts, sources and retry budget."""
import asyncio
import json
import os
import signal
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.credits.conftest import account, credits_env


@pytest.fixture(autouse=True)
def reuse_app(monkeypatch):
    monkeypatch.setenv("COS_TEST_APP_REUSE", "1")


@pytest.mark.parametrize("snapshot", [
    {"summary": "Warehouse inventory reconciliation", "problem": "Warehouse inventory discrepancies",
     "customer": "Warehouse operators", "offer": "Inventory reconciliation software"},
    {"summary": "Family hiking route discovery", "problem": "Finding family hiking routes",
     "customer": "Parents planning weekend hikes", "offer": "Family hiking route guide"},
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("interruption,responded,exhausted", [
    ("shutdown", False, False),
    ("lease", True, True),
    ("deadline", False, True),
    ("uncertain-replay", False, False),
    ("worker-error", False, False),
])
def test_research_restart_preserves_progress_and_charges(account, monkeypatch, snapshot, interruption, responded, exhausted):
    from app import bootstrap, state
    from app.core import jobs
    from app.credits.reserve import protect_outcome, reserve
    from app.db.models import ChatMessage, CreditAccount, CreditLedgerEntry, CreditReservation, Job, Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding
    from app.research import deep, execution
    from app.research.prior_art import graveyard
    from app.tasks.service import job_step
    from app.worker import health, main
    from core.clock import utcnow

    factory = state.db()
    now = utcnow()
    venture_id = account.attribution_venture_id
    workspace_id = "ws_wallet:" + venture_id
    payload = {"workspace_id": workspace_id, "venture_id": venture_id,
               "session_id": "idea_restart", "revision_id": "revision_restart", "phase": "competitors"}
    with factory() as session:
        venture = session.get(Venture, venture_id)
        venture.raw_idea = snapshot["problem"]
        session.add(IdeaSession(id=payload["session_id"], workspace_id=workspace_id, venture_id=venture_id,
            case="know", state="confirmed", current_revision_id=payload["revision_id"],
            confirmed_revision_id=payload["revision_id"], created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id=payload["revision_id"], session_id=payload["session_id"],
            workspace_id=workspace_id, venture_id=venture_id, rev=1, source="founder",
            summary=snapshot["summary"], structured=json.dumps(snapshot), created_at=now))
        job_id = jobs.enqueue(session, "research.deep", payload, workspace_id=workspace_id,
                              venture_id=venture_id, max_attempts=8)
        did = execution.identifier(job_id)
        session.add(ResearchDossier(id=did, workspace_id=workspace_id, venture_id=venture_id,
            revision_id=payload["revision_id"], kind="deep", state="running", budget_calls=20,
            calls_used=0, summary=json.dumps({"_runtime": {"job_id": job_id,
                "session_id": payload["session_id"], "snapshot": snapshot, "operations": {},
                "prior_art_enabled": False}, "coverage_gaps": []}), created_at=now))
        session.commit()

    queries = [("direct", snapshot["problem"]), ("alternatives", snapshot["offer"])]
    calls, holds = [], []

    class Provider:
        name = "synthetic_restart"

        async def search(self, query):
            calls.append(query)
            return [{"url": "https://example.com/" + str(len(calls)),
                     "title": snapshot["problem"], "snippet": snapshot["problem"]}]

    provider = Provider()
    monkeypatch.setenv("RESEARCH_PRIOR_ART_ENABLED", "0")
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    monkeypatch.setattr(deep, "_scrape_provider", lambda _: None)
    monkeypatch.setattr(deep, "queries_for", lambda *_: queries)
    if responded:
        monkeypatch.setattr(deep, "_review_sources", lambda *args, **kwargs: None)
    monkeypatch.setattr(graveyard, "start", lambda *args: None)
    monkeypatch.setattr(bootstrap, "bootstrap", lambda: None)
    monkeypatch.setattr(health, "write", lambda *_: None)
    monkeypatch.setattr(main, "install_default_handlers", lambda: None)
    monkeypatch.setattr(main, "_recover_orphaned_builds", lambda: 0)
    monkeypatch.setattr(main, "_start_health", lambda _: None)
    monkeypatch.setattr(main, "enabled_kinds", lambda: {"research.deep"})
    monkeypatch.setattr(main, "GRACE_S", 0.02)
    monkeypatch.setattr(main, "POLL_S", 0.01)

    async def idle(stop):
        await stop.wait()

    monkeypatch.setattr(main, "_scheduler_loop", idle)

    async def save_interruption(kind, leased_payload):
        execution.claim(leased_payload, kind="deep", budget=20)
        result = await asyncio.to_thread(deep._search, did, leased_payload, provider, "direct", queries[0][1], snapshot["problem"])
        deep._store_sources(did, leased_payload, result, queries[0][1], "direct")
        if responded:
            await asyncio.to_thread(deep._search, did, leased_payload, provider, "alternatives", queries[1][1], snapshot["problem"])
        else:
            for key, model in (("alternatives", None), ("review", "synthetic_model")):
                purpose = "research.deep" if model else "research.search"
                token = execution.prepare(did, key, leased_payload)
                hold = reserve(account_id=account.id, amount_micro=100, purpose=purpose,
                    provider=provider.name, model=model, workspace_id=workspace_id, venture_id=venture_id,
                    request_id=f"{did}:{key}:1")
                holds.append(hold)
                protect_outcome(hold)
                execution.begin(did, key, leased_payload, {"source": "platform", "account_id": account.id,
                    "reservation_id": hold.reservation_id, "estimate_micro": 100, "purpose": purpose,
                    "provider": provider.name, "model": model, "workspace_id": workspace_id,
                    "venture_id": venture_id, "request_id": f"{did}:{key}:1", "task_invocation": None}, token)
        if interruption != "shutdown":
            with factory() as session:
                row = session.get(Job, job_id)
                if interruption == "lease":
                    row.locked_at = now - timedelta(minutes=2)
                    session.commit()
                    assert jobs.reclaim_stale(session) == 1
                elif interruption == "deadline":
                    row.started_at = now - timedelta(seconds=row.timeout_s + 1)
                    session.commit()
                    assert jobs.expire_overdue_paid_journey(session) == 1
                else:
                    if responded:
                        jobs.release_lease(session, job_id)
                    else:
                        error = (jobs.RetryableError("research_transient") if interruption == "worker-error"
                                 else jobs.FatalError("research_outcome_uncertain"))
                        assert jobs.fail_job(session, job_id, error) == "failed_dead"
                session.commit()

    async def interrupt(kind, leased_payload):
        try:
            await save_interruption(kind, leased_payload)
        finally:
            os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "dispatch", interrupt)
    async def run():
        await asyncio.wait_for(main.run_worker(), timeout=10)

    asyncio.run(run())
    saved_sources = [source["id"] for source in deep.sources_for(did)]
    assert len(saved_sources) == 1
    with factory() as session:
        row = session.get(Job, job_id)
        assert (row.status, row.attempts, row.max_attempts, row.locked_by) == ("retry_wait" if responded else "failed_dead", 1, 3, None)
        task, _, _ = job_step(session, job_id)
        assert task.state == ("working" if responded else "input_required")
        spent = session.get(CreditAccount, account.id).spent_micro
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id)).all()
        if responded:
            assert messages == []
        else:
            assert len(messages) == 1 and messages[0].kind == "research_failed"
        if not responded:
            assert jobs.lease(session, "premature", kinds=["research.deep"]) == []
    if not responded:
        assert execution.reconcile_interrupted(now=now + timedelta(minutes=1)) == 0
        assert execution.reconcile_interrupted(now=now + timedelta(minutes=11)) == 1
        assert execution.reconcile_interrupted(now=now + timedelta(minutes=11)) == 0
        assert execution.operation(did, "review")["error_code"] == "research_interrupted"
    if responded:
        for attempt in range(2, 4 if exhausted else 3):
            with factory() as session:
                claimed = jobs.lease(session, "replacement", kinds=["research.deep"], now=now + timedelta(minutes=12))[0]
                session.commit()
            assert claimed.id == job_id and claimed.attempts == attempt
            resumed = {**claimed.payload, "_job_id": job_id, "_worker_id": claimed.worker_id, "_job_attempt": attempt}
            deep.run_deep(resumed)
            with factory() as session:
                ownership = {"worker_id": claimed.worker_id, "attempt": attempt}
                if exhausted:
                    jobs.release_lease(session, job_id, **ownership)
                else:
                    jobs.complete_job(session, job_id, **ownership)
                session.commit()
                if attempt == 2 and exhausted:
                    assert session.get(Job, job_id).status == "retry_wait"
                    assert session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id)).all() == []
    with factory() as session:
        row = session.get(Job, job_id)
        assert row.status == ("failed_dead" if not responded or exhausted else "succeeded")
        assert row.attempts == ((3 if exhausted else 2) if responded else 1)
        failures = session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.kind == "research_failed")).all()
        assert len(failures) == (int(exhausted) if responded else 2)
        assert jobs.lease(session, "fourth-worker", kinds=["research.deep"]) == []
        assert session.get(CreditAccount, account.id).spent_micro == spent
        assert session.get(CreditAccount, account.id).reserved_micro == 0
        findings = session.scalars(select(ResearchFinding).where(ResearchFinding.dossier_id == did)).all()
        assert set(saved_sources) <= {finding.id for finding in findings}
        assert len(findings) == (2 if responded else 1)
        for hold in holds:
            assert session.get(CreditReservation, hold.reservation_id).status == "released"
            entries = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == hold.reservation_id)).all()
            assert len(entries) == 1 and entries[0].kind == "release"
    if not responded:
        from app.api.dossiers_api import RerunBody, rerun_dossier
        from app.api.research_api import get_dossier_meta
        from app.research.service import get_dossier
        from app.tenancy import bind

        result = get_dossier(did)
        assert result["state"] == "partial" and result["completed_at"]
        assert result["summary"]["review_kind"] == "retrieved_sources_only" and result["summary"]["review"] is None
        assert get_dossier_meta(venture_id)["available"] is True
        with bind(workspace_id=workspace_id, venture_id=venture_id, actor=account.owner_person_id):
            new_run = rerun_dossier(venture_id, did, RerunBody(phase="all", request_key="new-research-run"),
                                   x_subject=account.owner_person_id)
        assert new_run["job_id"] != job_id
        with factory() as session:
            assert session.get(CreditAccount, account.id).spent_micro == spent
    assert calls == [query for _, query in queries][:2 if responded else 1]
