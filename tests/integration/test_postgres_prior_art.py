"""Actual migrated PG custody and locking; provider data are offline fixtures.

No provider, email or paid API calls. The shared lock test observes PostgreSQL's
wait state, rather than inferring contention from elapsed time. PG14 compatible.
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

from sqlalchemy import event, select, text

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


def test_pg_firecrawl_worker_waits_for_same_database_advisory_slot(journey_db, monkeypatch):
    from app.signals import research
    from app.tenancy import bind

    d, entered = journey_db, Event()
    tag = "prior-art-pace-" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("APP_ENV", "production")

    def submitted(conn, cursor, statement, parameters, context, executemany):
        if "pg_advisory_xact_lock" in statement:
            cursor.execute("SELECT set_config('application_name', %s, true)", (tag,))
            # A failed assertion must not leave a test worker waiting for the
            # production helper's full 20-second deadline before thread join.
            cursor.execute("SET LOCAL statement_timeout = '4000'")
            entered.set()

    def worker():
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return research._shared_firecrawl_wait(0.01)

    with d["factory"]() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": research._FIRECRAWL_SLOT_KEY})
        event.listen(d["engine"], "before_cursor_execute", submitted)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(worker)
                try:
                    assert entered.wait(5), "Worker never reached the shared database slot"
                    deadline, blocked = time.monotonic() + 5, False
                    # Fresh activity snapshot on every poll, including PG14.
                    with d["engine"].connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                        while time.monotonic() < deadline:
                            blocked = bool(observer.scalar(text("""
                                SELECT EXISTS (
                                    SELECT 1 FROM pg_stat_activity
                                    WHERE application_name = :tag AND wait_event_type = 'Lock'
                                )
                                """), {"tag": tag}))
                            if blocked:
                                break
                            time.sleep(0.01)
                    assert blocked, "PostgreSQL did not report an actual lock wait"
                    assert not future.done(), "Worker bypassed the held shared slot"
                finally:
                    holder.rollback()  # Release before any pool join, including assertion failures.
                assert future.result(timeout=5) is True
        finally:
            event.remove(d["engine"], "before_cursor_execute", submitted)


def test_pg_firecrawl_slot_commits_without_committing_caller_transaction(journey_db):
    from app.signals import research

    d = journey_db
    with d["factory"]() as caller:
        transaction_id = caller.scalar(text("SELECT txid_current()"))
        transaction = caller.get_transaction()
        assert research._shared_firecrawl_wait(0.01) is True
        assert caller.get_transaction() is transaction and transaction.is_active
        assert caller.scalar(text("SELECT txid_current()")) == transaction_id
        # A provider seam can now start: another actual session acquires the
        # same slot without waiting. There is deliberately no external HTTP.
        with d["factory"]() as observer:
            assert observer.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                   {"key": research._FIRECRAWL_SLOT_KEY}) is True
            observer.rollback()
        caller.rollback()


def test_pg_prior_art_completed_receipts_replay_without_redial_or_second_spend(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account, get_account
    from app.db.models import CreditLedgerEntry, Job
    from app.research import deep, execution
    from app.research.prior_art import adapters
    from app.research.service import get_dossier, start
    from app.tenancy import bind
    from tests.idea_fixtures import install_provider
    from tests.integration.test_postgres_research import requeue, run_one
    from tests.research_fixtures import install_research

    d = journey_db
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    d["calls"] = install_research(monkeypatch)
    monkeypatch.setenv("RESEARCH_PRIOR_ART_ENABLED", "1")
    for name in ("FIRECRAWL_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    adapter_calls, model_calls = [], []

    async def hn(query, **kwargs):
        adapter_calls.append(("hn", kwargs.get("tags")))
        return {"items": [], "calls": 1, "gaps": ["Offline HN fixture."], "provider": "hn_algolia"}

    async def yc(terms, **kwargs):
        adapter_calls.append(("yc", None))
        return {"items": [{"url": "https://www.ycombinator.com/companies/fixture-bakery", "title": "Fixture Bakery",
                "snippet": "Bakery order management software.", "provider": "yc_oss", "source_kind": "yc_company",
                "prior_art": {"company_name": "Fixture Bakery", "company_url": "https://fixture-bakery.example",
                              "status": "inactive", "yc_status": "Inactive", "date": "2021-01-01T00:00:00Z",
                              "source_observed_at": "2026-10-08T00:00:00Z", "cache_stale": False}}],
                "calls": 1, "gaps": ["Offline synthetic YC fixture, not a real company."], "provider": "yc_oss"}

    async def github(query, workspace_id, **kwargs):
        adapter_calls.append(("github", workspace_id))
        return {"items": [], "calls": 1, "gaps": ["Offline GitHub fixture."], "provider": "github"}

    async def no_comments(*args, **kwargs):
        raise AssertionError("Empty HN fixture must not trigger a discussion fetch")

    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(adapters, "search_yc", yc)
    monkeypatch.setattr(adapters, "search_github", github)
    monkeypatch.setattr(adapters, "hn_comments", no_comments)

    def completion(messages, options):
        body = json.loads(messages[-1]["content"])
        model_calls.append("review" if "sources" in body else "plan")
        if "sources" not in body:
            answer = {"category_terms": ["bakery order management"], "job_phrases": ["bakery order intake"],
                      "customer_terms": [], "seed_competitors": [], "negative_terms": [], "consumer_mobile": False}
        else:
            sid = next(source["id"] for source in body["sources"]
                       if any("Bakery order management software." in excerpt for excerpt in source["excerpts"]))
            answer = {"overview": "An earlier attempt is listed in this synthetic fixture.",
                      "observations": [{"source_id": sid, "quote_index": 0}],
                      "assessments": [{"kind": "inference", "category": "competitor_alternative",
                          "citations": [{"source_id": sid, "quote_index": 0}],
                          "assessment": "Compare this earlier bakery order workflow."}],
                      "competitor_checks": [], "service_checks": [], "coverage_gaps": [], "proposed_tests": []}
        from openai.types import CompletionUsage
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(answer)))],
            usage=CompletionUsage(prompt_tokens=300, completion_tokens=200, total_tokens=500,
                prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))

    install_provider(monkeypatch, completion)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        d["research_job_id"] = start(d["vid"])["job_id"]
    original = deep._finish
    def crash(*args):
        raise RuntimeError("Synthetic crash after durable prior-art receipts")
    monkeypatch.setattr(deep, "_finish", crash)
    run_one(d)
    did = execution.identifier(d["research_job_id"])
    document = json.loads(execution.read(did).summary)
    assert document["prior_art"]["companies"] and document["prior_art"]["lessons"]
    assert len(adapter_calls) == 4 and model_calls == ["plan", "review"]
    operations = document["_runtime"]["operations"]
    assert all(operations[key]["state"] == "completed" for key in
               ("prior-art-plan", "prior-hn-launches", "prior-hn-questions", "prior-yc", "prior-github-repos", "review"))
    account = get_account(d["vid"])
    with d["factory"]() as session:
        spends = {row.id for row in session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend",
            CreditLedgerEntry.reservation_id.is_not(None)))}
    assert spends and account.spent_micro > 0 and account.reserved_micro == 0
    before = (list(d["calls"]), list(adapter_calls), list(model_calls), account.spent_micro)
    requeue(d)
    monkeypatch.setattr(deep, "_finish", original)
    run_one(d)
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "succeeded"
        assert {row.id for row in session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend",
            CreditLedgerEntry.reservation_id.is_not(None)))} == spends
    assert (d["calls"], adapter_calls, model_calls, get_account(d["vid"]).spent_micro) == before
    assert get_account(d["vid"]).reserved_micro == 0
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        body = get_dossier(did)
    assert body["summary"]["prior_art"]["companies"] and body["summary"]["prior_art"]["lessons"]
