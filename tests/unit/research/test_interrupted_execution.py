"""Real receipt cleanup and safe resumption without provider transport."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.unit.credits.conftest import account, credits_env


@pytest.fixture
def interrupted(account):
    from app.credits.reserve import protect_outcome, reserve
    from app.db.models import Job
    from app.db.supymem_models import ResearchDossier
    from app.research.execution import identifier
    from app.state import db
    venture_id = account.attribution_venture_id
    workspace_id = "ws_wallet:" + venture_id
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    job_id = "job_interrupted"
    did = identifier(job_id)
    hold = reserve(account_id=account.id, amount_micro=100, purpose="research.search", provider="synthetic",
        workspace_id=workspace_id, venture_id=venture_id, request_id=f"{did}:search:1")
    protect_outcome(hold)
    usage = {"source": "platform", "account_id": account.id, "reservation_id": hold.reservation_id,
        "estimate_micro": 100, "purpose": "research.search", "provider": "synthetic", "model": None,
        "workspace_id": workspace_id, "venture_id": venture_id, "request_id": f"{did}:search:1", "task_invocation": None}
    with db()() as session:
        session.add(Job(id=job_id, kind="research.deep", workspace_id=workspace_id, venture_id=venture_id,
            payload_json='{}', status="failed_dead", last_error="job_outcome_uncertain", created_at=now))
        session.add(ResearchDossier(id=did, workspace_id=workspace_id, venture_id=venture_id, kind="deep", state="running",
            calls_used=1, summary=json.dumps({"_runtime": {"job_id": job_id, "operations": {
                "search": {"state": "calling", "attempt": 1, "started_at": (now - timedelta(minutes=10)).isoformat(),
                    "usage": usage}}}}), created_at=now))
        session.commit()
    return did, hold, now, account.id


@pytest.mark.parametrize("confirmed", [False, True])
def test_maintenance_resolves_original_hold_once_without_enqueuing(interrupted, confirmed, monkeypatch):
    from app.credits.reserve import settle
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Job
    from app.research import execution
    from app.state import db
    did, hold, now, account_id = interrupted
    monkeypatch.setattr("app.core.jobs.enqueue", lambda *args, **kwargs: pytest.fail("must not replay research"))
    if confirmed:
        settle(hold, actual_micro=40)
    assert execution.reconcile_interrupted(now=now) == 1
    assert execution.reconcile_interrupted(now=now) == 0
    operation = execution.operation(did, "search")
    assert operation["state"] == "failed" and operation["usage"]["settled"] is True
    assert operation["usage"]["credits_micro"] == (40 if confirmed else 0)
    with db()() as session:
        wallet = session.get(CreditAccount, account_id)
        assert wallet.reserved_micro == 0 and wallet.spent_micro == (40 if confirmed else 0)
        assert session.get(CreditReservation, hold.reservation_id).status == ("settled" if confirmed else "released")
        assert session.get(Job, "job_interrupted").last_error == "research_interrupted"
        entries = list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == hold.reservation_id)))
        assert len(entries) == 1 and entries[0].kind == ("spend" if confirmed else "release")


def test_saved_response_completes_the_receipt_without_another_call(interrupted):
    from app.research import execution
    did, _, now, _ = interrupted
    execution.save_operation(did, "search", {"response": {"items": []}, "usage": {
        **execution.operation(did, "search")["usage"], "actual_micro": 40, "provider_actual_micro": 40,
        "prompt_tokens": None, "completion_tokens": None, "cached_tokens": 0}})
    assert execution.reconcile_interrupted(now=now) == 1
    assert execution.operation(did, "search")["state"] == "completed"
    assert execution.operation(did, "search")["usage"]["credits_micro"] == 40


@pytest.mark.parametrize("idea", [
    {"problem": "Reconciling warehouse inventory", "customer": "Warehouse operators"},
    {"problem": "Finding family hiking routes", "customer": "Parents planning weekend hikes"},
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("interruption,receipt_state", [
    ("shutdown", "responded"), ("worker_retry", "uncertain"), ("legacy_retry", "calling"),
])
def test_research_auto_resume_requires_confirmed_paid_outcome(interrupted, monkeypatch, idea, interruption, receipt_state):
    from app.core import jobs
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Job, Venture
    from app.db.supymem_models import ResearchFinding
    from app.research import execution
    from app.state import db
    from app.worker import main

    did, hold, now, account_id = interrupted
    factory = db()
    saved = execution.read(did)
    response = {"items": [{"title": idea["problem"], "url": "https://example.test/source"}]}
    values = {"state": receipt_state, "started_at": now.isoformat()}
    if receipt_state == "responded":
        values.update(response=response, usage={**execution.operation(did, "search")["usage"],
            "actual_micro": 40, "provider_actual_micro": 40,
            "prompt_tokens": None, "completion_tokens": None, "cached_tokens": 0})
    execution.save_operation(did, "search", values)
    with factory() as session:
        session.get(Venture, saved.venture_id).raw_idea = idea["problem"]
        row = session.get(Job, "job_interrupted")
        row.payload_json = json.dumps({"workspace_id": saved.workspace_id, "venture_id": saved.venture_id, "idea": idea})
        row.status, row.last_error, row.finished_at = "leased", None, None
        row.attempts, row.max_attempts, row.locked_by = 1, 8, "original-worker"
        row.started_at = row.locked_at = now
        session.add(ResearchFinding(id="finding_interrupted", dossier_id=did, workspace_id=saved.workspace_id,
            venture_id=saved.venture_id, category="source", title=idea["problem"], source="synthetic",
            url="https://example.test/source", summary=idea["customer"],
            evidence=json.dumps({"evidence_kind": "search_excerpt"}), created_at=now))
        session.flush()
        if interruption == "shutdown":
            jobs.release_lease(session, row.id, now=now, worker_id="original-worker", attempt=1)
        elif interruption == "worker_retry":
            jobs.fail_job(session, row.id, jobs.RetryableError("research_transient", retry_after_s=1), now=now,
                          worker_id="original-worker", attempt=1)
        else:
            row.status, row.last_error = "retry_wait", "research_interrupted"
            row.locked_by = row.locked_at = None
            row.run_after = row.scheduled_for = datetime.max.replace(tzinfo=timezone.utc)
        session.commit()

    resumed = []
    async def dispatch(kind, payload):
        assert kind == "research.deep" and payload["idea"] == idea
        resumed.append(execution.recover(did, "search", payload))
    monkeypatch.setattr(main, "dispatch", dispatch)
    if interruption == "legacy_retry":
        assert execution.reconcile_interrupted(now=now + timedelta(minutes=11)) == 1
    with factory() as session:
        assert session.get(Job, "job_interrupted").status == ("retry_wait" if receipt_state == "responded" else "failed_dead")
        assert session.get(CreditReservation, hold.reservation_id).status == ("released" if interruption == "legacy_retry" else "reconciling")
        claimed = jobs.lease(session, "replacement-worker", kinds=["research.deep"], now=now + timedelta(seconds=2))
        session.commit()
    if receipt_state == "responded":
        assert len(claimed) == 1 and claimed[0].attempts == 2
        asyncio.run(main._run_one(factory, claimed[0], {(claimed[0].id, claimed[0].attempts)}))
        assert resumed == [response]
    else:
        assert claimed == [] and resumed == []
        execution.reconcile_interrupted(now=now + timedelta(minutes=11))
        assert execution.read(did).state == "partial"
        assert json.loads(execution.read(did).summary)["review_kind"] == "retrieved_sources_only"
    with factory() as session:
        assert session.get(Job, "job_interrupted").status == ("succeeded" if receipt_state == "responded" else "failed_dead")
        assert jobs.lease(session, "third-worker", kinds=["research.deep"], now=now + timedelta(minutes=12)) == []
        assert session.get(ResearchFinding, "finding_interrupted").summary == idea["customer"]
        assert session.get(CreditAccount, account_id).spent_micro == (40 if receipt_state == "responded" else 0)
        entries = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == hold.reservation_id)).all()
        assert len(entries) == 1


@pytest.mark.parametrize("responded", [False, True])
def test_worker_maintenance_resolves_task_linked_research_receipt(interrupted, monkeypatch, caplog, responded):
    from app.db.models import CreditAccount, CreditReservation, Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.research import execution
    from app.state import db
    from app.tasks.service import attach_job
    from app.worker import main
    did, hold, now, account_id = interrupted
    monkeypatch.setattr("app.core.jobs.enqueue", lambda *args, **kwargs: pytest.fail("must not replay research"))
    with db()() as session:
        job = session.get(Job, "job_interrupted")
        attach_job(session, job)
        step = session.scalar(select(TaskStep).where(TaskStep.job_id == job.id))
        assert step is not None
        run_id = step.run_id
        task_id = session.get(TaskRun, run_id).task_id
        session.commit()
    if responded:
        execution.save_operation(did, "search", {"response": {"items": []}, "usage": {
            **execution.operation(did, "search")["usage"], "actual_micro": 40, "provider_actual_micro": 40,
            "prompt_tokens": None, "completion_tokens": None, "cached_tokens": 0}})
    monkeypatch.setattr(execution, "utcnow", lambda: now)
    main._run_maintenance()
    assert not any(getattr(record, "action", None) == "research_receipts" for record in caplog.records)
    assert execution.reconcile_interrupted(now=now) == 0
    assert execution.operation(did, "search")["state"] == ("completed" if responded else "failed")
    assert json.loads(execution.read(did).summary)["recovery"] == "new_run_required"
    with db()() as session:
        assert session.get(CreditReservation, hold.reservation_id).status == ("settled" if responded else "released")
        wallet = session.get(CreditAccount, account_id)
        assert (wallet.reserved_micro, wallet.spent_micro) == (0, 40 if responded else 0)
        assert session.get(Job, "job_interrupted").last_error == "research_interrupted"
        assert session.get(TaskRun, run_id).state == "input_required"
        assert session.get(Task, task_id).state == "input_required"


@pytest.mark.parametrize("fatal", [True, False])
@pytest.mark.parametrize("idea", ["Invoice reconciliation for finance teams", "A tent rental marketplace for campers"])
def test_d11_recovery_fatal_is_terminal_and_retryable_backs_off(interrupted, monkeypatch, caplog, fatal, idea):
    from app.core.jobs import FatalError, RetryableError
    from app.db.models import CreditReservation, Job
    from app.research import execution
    from app.state import db
    did, hold, now, _ = interrupted
    with db()() as session:
        session.get(Job, "job_interrupted").payload_json = json.dumps({"idea": idea})
        session.commit()
    attempts, notices = [], []
    def recover(*args, **kwargs):
        attempts.append(True)
        raise (FatalError("research_scope_mismatch") if fatal else RetryableError("research_settlement_pending"))
    monkeypatch.setattr(execution.journal, "reconcile_interrupted", recover)
    monkeypatch.setattr("app.chat.milestones.post_milestone", lambda *args, **kwargs: notices.append(kwargs))
    assert execution.reconcile_interrupted(now=now) == int(fatal)
    assert execution.reconcile_interrupted(now=now + timedelta(seconds=30)) == 0
    if fatal:
        assert execution.reconcile_interrupted(now=now + timedelta(days=1)) == 0
        document = json.loads(execution.read(did).summary)
        assert document["recovery"] == "new_run_required" and document["error_code"] == "research_scope_mismatch"
        assert execution.read(did).state == "failed"
        assert len(notices) == 1 and any(action.get("action") == "job.retry" for action in notices[0]["actions"])
        with db()() as session:
            assert session.get(Job, "job_interrupted").last_error == "research_scope_mismatch"
            assert session.get(CreditReservation, hold.reservation_id).status == "reconciling"
        assert attempts == [True]
    else:
        runtime = json.loads(execution.read(did).summary)["_runtime"]
        assert runtime["recovery_retry_at"] == (now + timedelta(minutes=1)).isoformat()
        assert any(record.getMessage() == "research_recovery_pending" and record.exception_type == "RetryableError"
            for record in caplog.records)
        assert execution.reconcile_interrupted(now=now + timedelta(minutes=1)) == 0
        assert execution.reconcile_interrupted(now=now + timedelta(minutes=2)) == 0
        assert attempts == [True, True] and not notices
        assert execution.reconcile_interrupted(now=now + timedelta(minutes=3)) == 0
        assert attempts == [True, True, True]


@pytest.mark.parametrize("status,minutes", [("failed_dead", 9), ("leased", 20), ("retry_wait", 20)])
def test_recent_or_live_call_stays_protected(interrupted, status, minutes):
    from app.db.models import CreditReservation, Job
    from app.research import execution
    from app.state import db
    did, hold, now, _ = interrupted
    execution.save_operation(did, "search", {"started_at": (now - timedelta(minutes=minutes)).isoformat()})
    with db()() as session:
        session.get(Job, "job_interrupted").status = status
        session.commit()
    assert execution.reconcile_interrupted(now=now) == 0
    assert execution.operation(did, "search")["state"] == "calling"
    with db()() as session:
        assert session.get(CreditReservation, hold.reservation_id).status == "reconciling"


def test_foreign_receipt_binding_cannot_release_another_hold(interrupted):
    from app.db.models import CreditReservation
    from app.research import execution
    from app.state import db
    did, hold, now, _ = interrupted
    execution.save_operation(did, "search", {"usage": {**execution.operation(did, "search")["usage"],
        "request_id": "foreign:search:1"}})
    assert execution.reconcile_interrupted(now=now) == 1
    assert execution.reconcile_interrupted(now=now + timedelta(minutes=1)) == 0
    assert json.loads(execution.read(did).summary)["recovery"] == "new_run_required"
    with db()() as session:
        assert session.get(CreditReservation, hold.reservation_id).status == "reconciling"


def test_late_response_cannot_reopen_absorbed_custody(interrupted):
    from app.research import execution
    did, _, now, _ = interrupted
    assert execution.reconcile_interrupted(now=now) == 1
    original = execution.operation(did, "search")
    execution.checkpoint_model_receipt(did, "search", original["usage"], response={"late": True})
    assert execution.operation(did, "search")["state"] == "failed"
    assert execution.operation(did, "search")["usage"]["credits_micro"] == 0


def test_interrupted_run_keeps_partial_sources(interrupted):
    from app.db.supymem_models import ResearchFinding
    from app.research import execution
    from app.state import db
    did, _, now, _ = interrupted
    row = execution.read(did)
    with db()() as session:
        session.add(ResearchFinding(id="finding_interrupted", dossier_id=did, workspace_id=row.workspace_id,
            venture_id=row.venture_id, category="source", title="Saved source", source="synthetic",
            url="https://example.test/source", summary="Saved before interruption", created_at=now))
        session.commit()
    assert execution.reconcile_interrupted(now=now) == 1
    with db()() as session:
        assert session.get(ResearchFinding, "finding_interrupted").summary == "Saved before interruption"


@pytest.mark.parametrize("known_usage", [False, True])
@pytest.mark.parametrize("byok", [False, True])
def test_modern_sdk_receipt_resolves_without_redial(interrupted, known_usage, byok, monkeypatch):
    from app.core.operation_receipts import OperationJournal
    from app.credits.reserve import release
    from app.research import execution, metered
    from tests.unit.credits.test_core_model_receipts import binding, sdk, DIGEST
    did, hold, now, _ = interrupted
    release(hold, definitive=True)
    execution.mutate(did, None, lambda row, doc, session: doc["_runtime"].update(operations={}))
    journal = OperationJournal(read=execution.read, document=lambda value: json.loads(execution.read(value).summary),
        mutate=lambda value, payload, update: execution.mutate(value, None, update),
        before_call=lambda *args: None, prefix="research")
    calls = []
    def respond():
        calls.append(True)
        response = sdk()
        if not known_usage:
            response.usage = None
        return response
    checkpoint = journal.checkpoint_model_receipt
    def interrupted_capture(*args, **kwargs):
        checkpoint(*args, **kwargs)
        raise RuntimeError("synthetic worker interruption after SDK receipt")
    monkeypatch.setattr(journal, "checkpoint_model_receipt", interrupted_capture)
    with pytest.raises(RuntimeError, match="synthetic worker interruption"):
        metered.perform(did, "review", {"_job_attempt": 1, "_worker_id": "synthetic"},
            purpose="research.deep", provider="azure_openai", model="synthetic-deployment", callback=respond,
            estimate=1200, token_ceiling=200, journal=journal, parse=json.loads, byok=byok,
            model_binding=binding(), request_digest=DIGEST)
    execution.save_operation(did, "review", {"started_at": (now - timedelta(minutes=10)).isoformat()})
    assert execution.reconcile_interrupted(now=now) == 1
    operation = execution.operation(did, "review")
    assert operation["usage"]["settled"] is True
    assert operation["usage"]["credits_micro"] == (248 if known_usage and not byok else 0)
    assert calls == [True]
    assert operation["state"] == ("completed" if known_usage else "failed")
    with pytest.raises(metered.FatalError, match="research_invalid_output" if known_usage else "research_interrupted"):
        journal.recover(did, "review")


def test_maintenance_resolves_validation_and_plan_without_provider_replay(interrupted, monkeypatch):
    from app.core.jobs import PAID_JOURNEY_KINDS, enqueue, lease, reclaim_stale
    from app.credits.reserve import protect_outcome, reserve
    from app.db.models import ChatMessage, CreditReservation, Job
    from app.db.supymem_models import ExecutionPlan, ResearchDossier
    from app.planning import execution as planning
    from app.research import execution
    from app.state import db
    from app.worker import main
    from core.clock import utcnow
    from llm_gateway import sole

    did, validation_hold, _, account_id = interrupted
    assert "plan.compile" in PAID_JOURNEY_KINDS
    now = utcnow()
    dossier = execution.read(did)
    started = (now - timedelta(minutes=11)).isoformat()
    with db()() as session:
        session.get(Job, "job_interrupted").kind = "idea.validate"
        session.get(ResearchDossier, did).kind = "validation"
        plan_job_id = enqueue(session, "plan.compile", {}, workspace_id=dossier.workspace_id,
            venture_id=dossier.venture_id)
        session.commit()
        assert len(lease(session, "lost-worker", kinds=["plan.compile"])) == 1
        session.get(Job, plan_job_id).locked_at = now - timedelta(minutes=2)
        session.commit()
    pid = planning.identifier(plan_job_id)
    plan_hold = reserve(account_id=account_id, amount_micro=100, purpose="plan.compile", provider="synthetic",
        workspace_id=dossier.workspace_id, venture_id=dossier.venture_id, request_id=f"{pid}:plan:1")
    protect_outcome(plan_hold)
    usage = {**execution.operation(did, "search")["usage"], "reservation_id": plan_hold.reservation_id,
        "purpose": "plan.compile", "request_id": f"{pid}:plan:1"}
    execution.save_operation(did, "search", {"started_at": started})
    with db()() as session:
        session.add(ExecutionPlan(id=pid, workspace_id=dossier.workspace_id, venture_id=dossier.venture_id,
            version=1, state="draft", created_at=now, structured=json.dumps({"_runtime": {
                "job_id": plan_job_id, "operations": {"plan": {
                    "state": "calling", "attempt": 1, "started_at": started, "usage": usage}}}})))
        session.commit()
        assert reclaim_stale(session, now=now) == 1
        assert lease(session, "replacement", kinds=["plan.compile"]) == []
        session.commit()
        plan_job = session.get(Job, plan_job_id)
        assert (plan_job.status, plan_job.last_error) == ("failed_dead", "job_outcome_uncertain")

    calls = []
    def provider(*args, **kwargs):
        calls.append(True)
        raise AssertionError("maintenance must never call a provider")
    monkeypatch.setattr(sole, "transmit", provider)
    monkeypatch.setattr(sole, "transmit_responses_request", provider)
    main._run_maintenance()
    assert execution.reconcile_interrupted() == 0
    assert calls == []
    assert execution.operation(did, "search")["state"] == "failed"
    assert planning.journal.operation(pid, "plan")["state"] == "failed"
    assert json.loads(execution.read(did).summary)["recovery"] == "new_run_required"
    plan = planning.read(pid)
    document = json.loads(plan.structured)
    assert document["recovery"] == "new_run_required" and document["_runtime"]["generation_failed"] is True
    assert plan.state == "draft"
    with db()() as session:
        for job_id, code, kind in (("job_interrupted", "validation_interrupted", "idea_failed"),
                (plan_job_id, "plan_interrupted", "plan_failed")):
            job = session.get(Job, job_id)
            assert (job.status, job.last_error) == ("failed_dead", code)
            messages = list(session.scalars(select(ChatMessage).where(
                ChatMessage.venture_id == dossier.venture_id, ChatMessage.kind == kind)))
            assert len(messages) == 1
            assert ("Validation" if kind == "idea_failed" else "Planning") in messages[0].body_md
        for hold in (validation_hold, plan_hold):
            assert session.get(CreditReservation, hold.reservation_id).status == "released"
