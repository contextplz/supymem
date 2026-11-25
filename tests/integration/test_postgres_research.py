"""Migrated PostgreSQL research receipts: recovery, concurrency and tenant gates."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.research_fixtures import install_research


@pytest.fixture()
def research(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.research.service import start
    from app.tenancy import bind
    d = journey_db
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        d["research_job_id"] = start(d["vid"])["job_id"]
    d["calls"] = install_research(monkeypatch)
    return d


def leased(d):
    from app.core.jobs import lease
    with d["factory"]() as session:
        # Module-scoped migrated DB may contain other cases' pending work.
        # Exercise real leasing, then select only this case's exact reviewed job.
        jobs = lease(session, "pg-research", kinds=["research.deep"], n=100)
        owned = [job for job in jobs if job.id == d["research_job_id"]]
        assert len(owned) == 1, "The exact research receipt must be authoritatively leased"
        job = owned[0]
        assert job.venture_id == d["vid"] and job.workspace_id == d["wid"]
        assert job.payload.get("_actor_subject") == d["actor"]
        session.commit()
    return job


def run_one(d):
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    job = leased(d)
    asyncio.run(_run_one(d["factory"], job, {job.id}))
    return job


def requeue(d):
    from app.db.models import Job
    with d["factory"]() as session:
        row = session.get(Job, d["research_job_id"])
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()


def test_pg_research_response_recovery_without_redial_or_duplicate_credit(research, monkeypatch):
    from app.credits.accounts import get_account
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.research import deep
    d = research
    original = deep._finish
    def crash(*args):
        raise RuntimeError("worker died after receipt before report commit")
    monkeypatch.setattr(deep, "_finish", crash)
    run_one(d)
    assert len(d["calls"]) == 10
    requeue(d)
    monkeypatch.setattr(deep, "_finish", original)
    run_one(d)
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "succeeded"
        assert len(session.scalars(select(ResearchDossier).where(ResearchDossier.venture_id == d["vid"])).all()) == 1
        assert len(session.scalars(select(ResearchFinding).where(ResearchFinding.venture_id == d["vid"])).all()) == 2
        spent = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.kind == "spend", CreditLedgerEntry.account_id == get_account(d["vid"]).id, CreditLedgerEntry.reservation_id.is_not(None))).all()
        assert len(spent) == 9  # Eight searches + one model; public GitHub is free.
    assert len(d["calls"]) == 10


def test_pg_duplicate_delivery_has_one_receipt_and_one_provider_sequence(research):
    from app.core.jobs import FatalError
    from app.db.supymem_models import ResearchDossier
    from app.research.deep import run_deep
    d = research
    job = leased(d)
    payload = {**job.payload, "_job_id": job.id, "_job_attempt": job.attempts, "_worker_id": job.worker_id}
    def invoke():
        try:
            run_deep(payload)
            return "finished"
        except FatalError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))
    assert "finished" in results and len(d["calls"]) == 10
    with d["factory"]() as session:
        rows = session.scalars(select(ResearchDossier).where(ResearchDossier.venture_id == d["vid"])).all()
        assert len(rows) == 1 and rows[0].calls_used == 10


def test_pg_calling_receipt_is_not_automatically_repeated(research, monkeypatch):
    from app.db.models import Job
    from app.research import deep, execution as ex
    d = research
    async def timeout(query):
        d["calls"].append(("ambiguous", query))
        raise TimeoutError("private provider body")
    provider = deep._web_provider()
    provider.search = timeout
    monkeypatch.setattr(deep, "_web_provider", lambda: provider)
    run_one(d)
    assert len(d["calls"]) == 1
    requeue(d)
    run_one(d)
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).last_error == "research_outcome_uncertain"
    operations = json.loads(ex.read(ex.identifier(d["research_job_id"])).summary)["_runtime"]["operations"]
    assert operations["direct"]["state"] == "uncertain" and len(d["calls"]) == 1
    from datetime import timedelta
    from core.clock import utcnow
    from app.credits.accounts import get_account
    from app.credits.reserve import sweep_expired
    held = get_account(d["vid"]).reserved_micro
    assert held > 0
    sweep_expired(utcnow() + timedelta(days=2))
    assert get_account(d["vid"]).reserved_micro == held


def test_pg_reviewed_research_rechecks_revision_after_waiting_for_scope_lock(journey_db):
    """A review read before another client's commit cannot admit the changed scope."""
    import time
    import uuid
    from threading import Event
    from sqlalchemy import event, text
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.research.service import start
    from core.clock import utcnow
    d = journey_db
    changed, tag = "idv_" + uuid.uuid4().hex[:12], "review-cas-" + uuid.uuid4().hex[:12]
    entered = Event()
    def submitted(conn, cursor, statement, parameters, context, executemany):
        if "idea_sessions" in statement and "FOR UPDATE" in statement:
            cursor.execute("SELECT set_config('application_name', %s, true)", (tag,))
            entered.set()
    def reviewed_request():
        try:
            return start(d["vid"], expected_revision_id=d["rid"])
        except RuntimeError as exc:
            return str(exc)
    with d["factory"]() as writer:
        idea = writer.scalar(select(IdeaSession).where(IdeaSession.id == d["sid"]).with_for_update())
        writer.add(IdeaRevision(id=changed, session_id=d["sid"], workspace_id=d["wid"], venture_id=d["vid"],
            rev=2, source="founder", structured=json.dumps({"offer": "Different reviewed scope"}), created_at=utcnow()))
        idea.current_revision_id = idea.confirmed_revision_id = changed
        writer.flush()  # Remains uncommitted while the already-reviewed request arrives.
        event.listen(d["engine"], "before_cursor_execute", submitted)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(reviewed_request)
                try:
                    assert entered.wait(5), "Reviewed request did not reach locked admission"
                    deadline, blocked = time.monotonic() + 5, False
                    with d["engine"].connect() as observer:
                        while time.monotonic() < deadline:
                            blocked = bool(observer.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = :tag AND wait_event_type = 'Lock')"), {"tag": tag}))
                            if blocked:
                                break
                            time.sleep(0.01)
                    assert blocked and not future.done(), "Actual PostgreSQL lock contention was not observed"
                finally:
                    writer.commit()  # Even on assertion failure, release lock before joining thread.
                assert future.result(timeout=10) == "research_inputs_changed"
        finally:
            event.remove(d["engine"], "before_cursor_execute", submitted)
    with d["factory"]() as session:
        assert not session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind == "research.deep")).all()
        assert not session.scalars(select(CreditReservation).where(CreditReservation.workspace_id == d["wid"])).all()


def test_pg_current_reviewed_revision_concurrent_admission_adopts_one_job(journey_db):
    from threading import Barrier
    from app.tenancy import bind
    from app.db.models import CreditReservation, Job
    from app.research.service import start
    d, gate = journey_db, Barrier(4)
    def contender(_):
        gate.wait(timeout=10)
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return start(d["vid"], expected_revision_id=d["rid"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(contender, range(4)))
    assert len({item["job_id"] for item in outcomes}) == 1
    with d["factory"]() as session:
        jobs = session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind == "research.deep")).all()
        assert len(jobs) == 1
        payload = json.loads(jobs[0].payload_json)
        assert payload["revision_id"] == d["rid"] and payload["_actor_subject"] == d["actor"]
        assert not session.scalars(select(CreditReservation).where(CreditReservation.workspace_id == d["wid"])).all()


@pytest.mark.parametrize("usage", [None, {},
    {"prompt_tokens": 500, "completion_tokens": 250},
    {"prompt_tokens": True, "completion_tokens": 250, "total_tokens": 251},
    {"prompt_tokens": -1, "completion_tokens": 250, "total_tokens": 249},
    {"prompt_tokens": 500, "completion_tokens": 250, "total_tokens": 1},
])
def test_pg_unknown_review_usage_holds_credit_and_forbids_repair_or_replay(research, monkeypatch, usage):
    from datetime import timedelta
    from types import SimpleNamespace
    from core.clock import utcnow
    from app.credits.accounts import get_account
    from app.credits.reserve import sweep_expired
    from app.db.models import CreditReservation, Job
    from app.research import execution as ex
    d = research

    def unknown_response(messages, options):
        # Unknown usage cannot admit a repair or release the original hold.
        return SimpleNamespace(model="DeepSeek-V4-Pro", service_tier="default", usage=usage, choices=[SimpleNamespace(finish_reason="length",
            message=SimpleNamespace(content="not JSON"))])

    d["calls"] = install_research(monkeypatch, callback=unknown_response)
    run_one(d)
    did = ex.identifier(d["research_job_id"])
    operations = json.loads(ex.read(did).summary)["_runtime"]["operations"]
    assert operations["review"]["state"] == "provider_responded"
    assert operations["review"]["usage"]["model_receipt_error"] == "counts_unknown"
    assert not any(key.startswith("review-repair") for key in operations)
    assert [kind for kind, _ in d["calls"]].count("review") == 1
    usage_receipt = operations["review"]["usage"]
    assert "actual_micro" not in usage_receipt and "settled" not in usage_receipt
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "retry_wait"
        assert session.get(CreditReservation, usage_receipt["reservation_id"]).status == "reconciling"
    held = get_account(d["vid"]).reserved_micro
    assert held == usage_receipt["estimate_micro"] > 0
    sweep_expired(utcnow() + timedelta(days=2))
    assert get_account(d["vid"]).reserved_micro == held
    requeue(d)  # Even an explicit redelivery cannot obtain another paid dial.
    run_one(d)
    assert [kind for kind, _ in d["calls"]].count("review") == 1
    assert get_account(d["vid"]).reserved_micro == held
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).last_error == "provider_retryable:RetryableError"


def test_pg_complete_zero_sdk_usage_settles_zero_and_recovers_without_redial(research, monkeypatch):
    from openai.types import CompletionUsage
    from llm_gateway import sole
    from app.db.models import CreditReservation, Job
    from app.research import execution as ex
    d, original = research, sole._primary_chat

    async def zero_response(*args, **kwargs):
        response = await original(*args, **kwargs)
        response.usage = CompletionUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0,
            prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0})
        return response

    monkeypatch.setattr(sole, "_primary_chat", zero_response)
    run_one(d)
    receipt = ex.operation(ex.identifier(d["research_job_id"]), "review")
    assert receipt["state"] == "completed" and receipt["usage"]["actual_micro"] == 0
    assert receipt["usage"]["credits_micro"] == 0 and receipt["usage"]["settled"] is True
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "succeeded"
        reservation = session.get(CreditReservation, receipt["usage"]["reservation_id"])
        assert reservation.status == "settled" and reservation.settled_micro == 0
    before = len(d["calls"])
    requeue(d)
    run_one(d)
    assert len(d["calls"]) == before


def test_pg_interactive_deadline_retains_primary_hold_without_fallback_or_redial(research, monkeypatch):
    from llm_gateway import sole
    from app.credits.accounts import get_account
    from app.db.models import Job
    from app.research import deep, execution as ex
    d, original = research, deep.synthesize
    provider_calls = []

    async def slow(*args, **kwargs):
        provider_calls.append(kwargs)
        await asyncio.Event().wait()

    def interactive(*args, **kwargs):
        kwargs.update(pin_provider=True, provider_timeout_seconds=1, cap_at_reservation=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(sole, "_primary_chat", slow)
    monkeypatch.setattr(deep, "synthesize", interactive)
    run_one(d)
    receipt = ex.operation(ex.identifier(d["research_job_id"]), "review")
    assert receipt["state"] == "uncertain" and receipt["failure"]["category"] == "timeout"
    held = get_account(d["vid"]).reserved_micro
    assert held == receipt["usage"]["estimate_micro"] > 0
    assert len(provider_calls) == 1
    requeue(d)
    run_one(d)
    assert len(provider_calls) == 1 and get_account(d["vid"]).reserved_micro == held
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "failed_dead"


def test_pg_known_paid_invalid_review_settles_once_without_repair(research, monkeypatch):
    from app.research import execution as ex
    d = research
    d["calls"] = install_research(monkeypatch, invalid=True)
    run_one(d)
    operations = json.loads(ex.read(ex.identifier(d["research_job_id"])).summary)["_runtime"]["operations"]
    assert [kind for kind, _ in d["calls"]].count("review") == 1
    assert operations["review"]["state"] == "completed"
    assert operations["review"]["usage"]["settled"] is True
    assert operations["review"]["usage"]["total_tokens"] == 750
    assert operations["review"]["response"] == {}
    assert not any(key.startswith("review-repair") for key in operations)
    requeue(d)
    run_one(d)
    assert [kind for kind, _ in d["calls"]].count("review") == 1


def test_pg_unknown_primary_never_admits_a_repair(research, monkeypatch):
    from llm_gateway import sole
    from app.credits.accounts import get_account
    from app.db.models import Job
    from app.research import execution as ex
    d = research
    d["calls"] = install_research(monkeypatch, invalid=True)
    original, count = sole._primary_chat, []

    async def unknown_primary(*args, **kwargs):
        response = await original(*args, **kwargs)
        count.append(True)
        response.usage = None
        return response

    monkeypatch.setattr(sole, "_primary_chat", unknown_primary)
    run_one(d)
    with d["factory"]() as session:
        job = session.get(Job, d["research_job_id"])
        assert job.status == "retry_wait" and job.last_error == "provider_retryable:RetryableError"
    operations = json.loads(ex.read(ex.identifier(d["research_job_id"])).summary)["_runtime"]["operations"]
    assert operations["review"]["state"] == "provider_responded"
    assert operations["review"]["usage"]["model_receipt_error"] == "counts_unknown"
    assert not any(key.startswith("review-repair") for key in operations)
    held = get_account(d["vid"]).reserved_micro
    assert held == operations["review"]["usage"]["estimate_micro"] > 0
    requeue(d)
    run_one(d)
    assert len(count) == 1 and get_account(d["vid"]).reserved_micro == held


def test_pg_github_failure_is_a_gap_not_a_dead_run(research, monkeypatch):
    """Prod job_sq2fn9dyw7dt: the free GitHub lookup raised before any request (query guard) and
    its uncertain receipt failed the whole run. It is optional evidence: record a gap and finish."""
    from app.db.models import Job
    from app.research import execution as ex, github_search
    d = research
    seen = []  # query normalization itself: tests/unit/research/test_github_query.py

    async def guarded(query, workspace_id, limit=5):
        seen.append(query)
        raise ValueError("github research requires a public search query of 1-240 characters")

    monkeypatch.setattr(github_search, "search_public_repositories", guarded)
    run_one(d)
    assert seen and "\n" not in seen[0]
    row = ex.read(ex.identifier(d["research_job_id"]))
    assert row.state == "partial"
    assert any("GitHub repository search could not run" in gap for gap in json.loads(row.summary)["coverage_gaps"])
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "succeeded"


def test_pg_stopped_run_with_sources_stays_usable_partial(research, monkeypatch):
    """An uncertain review after paid retrieval keeps a source-only partial dossier that planning
    accepts; the hold stays protected and redelivery neither redials nor flips the job to success."""
    from app.credits.accounts import get_account
    from app.db.models import Job
    from app.db.supymem_models import ResearchDossier
    from app.research import execution as ex
    d = research
    def timeout(messages, options):
        raise TimeoutError("synthetic unknown transport")
    d["calls"] = install_research(monkeypatch, callback=timeout)
    run_one(d)
    did = ex.identifier(d["research_job_id"])
    row = ex.read(did)
    doc = json.loads(row.summary)
    assert row.state == "partial" and doc["review_kind"] == "retrieved_sources_only"
    assert doc["error_code"] == "research_outcome_uncertain"
    held = get_account(d["vid"]).reserved_micro
    assert held > 0
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "failed_dead"
        # The same filter planning/execution.py uses to pick research it can plan from.
        assert session.scalars(select(ResearchDossier).where(ResearchDossier.id == did,
            ResearchDossier.state.in_(["complete", "partial"]))).one()
    requeue(d)
    run_one(d)
    assert [kind for kind, _ in d["calls"]].count("review") == 1 and get_account(d["vid"]).reserved_micro == held
    with d["factory"]() as session:
        assert session.get(Job, d["research_job_id"]).status == "failed_dead"


def test_pg_run_again_creates_one_job_per_click_and_none_while_in_flight(research):
    from app.db.models import Job
    from app.db.supymem_models import FounderRequest
    from app.research.service import start
    from app.tenancy import bind
    d = research

    def click(key):
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return start(d["vid"], expected_revision_id=d["rid"], request_key=key)["job_id"]

    def jobs():
        with d["factory"]() as session:
            return session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind == "research.deep")).all()

    first = d["research_job_id"]
    assert click("click-in-flight-1") == first  # in flight: adopt it, never a duplicate run
    assert len(jobs()) == 1
    with d["factory"]() as session:
        session.get(Job, first).status = "failed_dead"  # the run ended uncertain
        session.commit()
    assert click("click-in-flight-1") == first and len(jobs()) == 1  # replay of an adopted click stays bound
    again = click("click-run-again-1")
    assert again != first and len(jobs()) == 2
    assert click("click-run-again-1") == again and len(jobs()) == 2  # replayed request: same job
    assert click("click-run-again-2") == again and len(jobs()) == 2  # new click while that run is in flight
    with d["factory"]() as session:
        session.get(Job, again).status = "failed_dead"
        session.commit()
    assert click("click-run-again-1") == again and len(jobs()) == 2  # late replay of a finished click
    assert click("click-run-again-2") == again and len(jobs()) == 2  # the in-flight adoption was durable too
    third = click("click-run-again-3")
    assert third not in {first, again} and len(jobs()) == 3
    assert click("click-in-flight-1") == first and len(jobs()) == 3  # newer active work cannot replace a replay
    assert click("click-run-again-2") == again and len(jobs()) == 3
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        for changed in ({"phase": "demand"}, {"source_digest": "changed"}, {"expected_revision_id": "stale"}):
            with pytest.raises(RuntimeError, match="research_inputs_changed"):
                start(d["vid"], request_key="click-in-flight-1", **changed)
        with pytest.raises(KeyError):
            start(d["vid"], request_key="click-in-flight-1", previous_dossier_id="missing")
    assert len(jobs()) == 3
    # Legacy V9 jobs already have the primary dedupe key, without the new per-click receipts.
    with d["factory"]() as session:
        for receipt in session.scalars(select(FounderRequest).where(
                FounderRequest.source == "research_start_v1", FounderRequest.related_id == again)):
            session.delete(receipt)
        session.commit()
    assert click("click-run-again-1") == again and len(jobs()) == 3
