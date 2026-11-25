"""Real migrated PostgreSQL receipts, billing recovery and worker fencing."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.idea_fixtures import completion, install_provider


@pytest.fixture()
def clarifier(journey_db, monkeypatch):
    from app.credits.accounts import ensure_account
    from app.db.supymem_models import IdeaSession
    from app.idea.service import post_message
    from app.tenancy import bind
    d = journey_db
    with d["factory"]() as session:
        row = session.get(IdeaSession, d["sid"])
        row.state, row.confirmed_at, row.confirmed_revision_id = "clarifying", None, None
        session.commit()
    ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        result = post_message(d["sid"], "Create a reviewable proposal for the bakery order board")
    d["clarify_job_id"] = result["job_id"]
    d["calls"] = install_provider(monkeypatch, lambda *args: completion())
    return d


def run_one(d, worker="pg-clarifier"):
    from app.core.jobs import lease
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    with d["factory"]() as session:
        jobs = lease(session, worker, kinds=["idea.clarify_turn"], n=1)
        session.commit()
    assert len(jobs) == 1
    asyncio.run(_run_one(d["factory"], jobs[0], {jobs[0].id}))
    return jobs[0]


@pytest.mark.parametrize("case", ["know", "explore"])
def test_accepted_idea_card_reoffers_fresh_validation_phase_and_rejects_stale_binding(journey_db, case):
    from app.autopilot import actions, lifecycle
    from app.autopilot.authority import founder_scope
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession, Suggestion, ValidationRun
    from app.idea.service import confirm
    from app.idea.validation import METHODS, start_validation
    from app.suggestions import service as suggestions
    from core.clock import utcnow
    from core.ids import new_id

    d = journey_db
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.case, idea.state, idea.confirmed_at, idea.confirmed_revision_id = case, "reviewing", None, None
        session.get(Job, d["job_id"]).status = "cancelled"
        session.commit()
    initial = lifecycle._idea_advance_payload(d["vid"])
    assert initial["idea_phase"] == "reviewing:False"
    with founder_scope(d["vid"], d["actor"]):
        ids = suggestions.generate(d["vid"])
    with d["factory"]() as session:
        first = next(session.get(Suggestion, sid) for sid in ids if session.get(Suggestion, sid).action_kind == "idea.advance")
    queued = asyncio.run(suggestions.accept(first.id, d["actor"]))
    assert queued["status"] == "accepted" and queued["result_state"] == "running"
    queued_result = json.loads(queued["result_ref"])
    assert queued_result["outcome"] == "running" and queued_result["jobs"]
    with d["factory"]() as session:
        validation = session.scalar(select(Job).where(Job.venture_id == d["vid"], Job.kind == "idea.validate"))
        validation.status = "succeeded"
        for method in METHODS:
            session.add(ValidationRun(id=new_id("val"), session_id=d["sid"], revision_id=d["rid"], workspace_id=d["wid"],
                venture_id=d["vid"], method=method, state="complete", verdict="proceed_with_test", report="{}", created_at=utcnow()))
        session.commit()
    fresh = lifecycle._idea_advance_payload(d["vid"])
    assert fresh["idea_phase"] == "validating:True" and fresh != initial
    # These comparisons run inside each producer's locked mutation transaction,
    # after the original proposal/driver read has already become stale.
    with pytest.raises(RuntimeError, match="idea_advance_scope_changed"):
        start_validation(d["sid"], revision_id=d["rid"], expected_advance_binding=initial)
    with pytest.raises(RuntimeError, match="idea_advance_scope_changed"):
        confirm(d["sid"], revision_id=d["rid"], person_id=d["actor"], expected_advance_binding=initial)
    stale = asyncio.run(actions.execute_proposed_action(d["vid"], {"kind": "idea.advance", "payload": initial, "title": "Old review"},
        d["actor"], approved_ceiling_micro=0))
    assert stale["reason"] == "needs_you" and stale["completion_verified"] is False
    with founder_scope(d["vid"], d["actor"]):
        new_ids = suggestions.generate(d["vid"])
    with d["factory"]() as session:
        second = next(session.get(Suggestion, sid) for sid in new_ids if session.get(Suggestion, sid).action_kind == "idea.advance")
        assert second.dedupe_key != first.dedupe_key
        assert session.get(Suggestion, first.id).status == "running"  # no global replay allowance
        assert session.get(IdeaSession, d["sid"]).state == "validating"
        assert not list(session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind == "research.deep")))
    completed = asyncio.run(suggestions.accept(second.id, d["actor"]))
    # Confirmation also queues research. The saved phase advanced, while the
    # whole action is still queued rather than falsely declared complete.
    assert completed["status"] == "accepted" and completed["result_state"] == "running"
    assert json.loads(completed["result_ref"])["jobs"]
    with d["factory"]() as session:
        assert session.get(IdeaSession, d["sid"]).confirmed_revision_id == d["rid"]
        assert len(list(session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind == "research.deep")))) == 1
        if case == "explore":
            assert all(row.founder_decision == "accept" for row in session.scalars(select(ValidationRun).where(ValidationRun.session_id == d["sid"])))


def test_idea_advance_producers_reject_revision_changed_after_review(journey_db):
    from app.autopilot import lifecycle
    from app.db.models import Job
    from app.db.supymem_models import IdeaSession
    from app.idea.service import confirm, patch_revision
    from app.idea.validation import start_validation

    d = journey_db
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        idea.state, idea.confirmed_at, idea.confirmed_revision_id = "reviewing", None, None
        session.commit()
    reviewed = lifecycle._idea_advance_payload(d["vid"])
    patch_revision(d["sid"], {"offer": "The founder's newer scoped bakery proposal"}, venture_id=d["vid"], workspace_id=d["wid"])
    current = lifecycle._idea_advance_payload(d["vid"])
    assert current["idea_phase"] == reviewed["idea_phase"]  # same phase, different real source revision
    assert current["idea_revision_id"] != reviewed["idea_revision_id"]
    with pytest.raises(RuntimeError, match="idea_advance_scope_changed"):
        start_validation(d["sid"], revision_id=d["rid"], expected_advance_binding=reviewed)
    with pytest.raises(RuntimeError, match="idea_advance_scope_changed"):
        confirm(d["sid"], revision_id=d["rid"], person_id=d["actor"], expected_advance_binding=reviewed)
    with d["factory"]() as session:
        assert session.get(IdeaSession, d["sid"]).state == "reviewing"
        assert not list(session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind.in_(["idea.validate", "research.deep"]))))


def test_chat_started_clarification_saves_followup_in_same_transaction(clarifier):
    from app.db.models import ChatMessage, Workspace

    d = clarifier
    with d["factory"]() as session:
        workspace = session.get(Workspace, d["wid"])
        settings = json.loads(workspace.settings_json or "{}")
        settings["chat_entry_defer_clarification"] = True
        settings["entry_goal"] = "startup"
        workspace.settings_json = json.dumps(settings)
        session.commit()
    run_one(d)
    with d["factory"]() as session:
        replies = list(session.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == d["vid"],
            ChatMessage.kind == "idea_clarification_ready",
        )))
        assert len(replies) == 1
        assert replies[0].role == "assistant"
        assert "idea" in replies[0].body_md.lower()
        assert replies[0].workspace_id == d["wid"]


def requeue(d):
    from app.db.models import Job
    with d["factory"]() as session:
        row = session.get(Job, d["clarify_job_id"])
        row.status, row.run_after, row.scheduled_for = "retry_wait", None, None
        session.commit()


@pytest.mark.parametrize("usage", [None, {},
    {"prompt_tokens": 200, "completion_tokens": 100},
    {"prompt_tokens": True, "completion_tokens": 100, "total_tokens": 101},
    {"prompt_tokens": -1, "completion_tokens": 100, "total_tokens": 99},
    {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 1},
])
def test_pg_unconfirmed_clarifier_usage_cannot_charge_apply_or_redial(clarifier, monkeypatch, usage):
    from app.credits.accounts import get_account
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    d = clarifier
    with d["factory"]() as session:
        revision = session.get(IdeaSession, d["sid"]).current_revision_id

    def unconfirmed(*args):
        response = completion()
        response.usage = usage
        return response

    calls = install_provider(monkeypatch, unconfirmed)
    run_one(d)
    with d["factory"]() as session:
        receipt = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        assert receipt.state == "uncertain" and receipt.response is None
        assert receipt.error_code == "clarification_outcome_uncertain"
        saved = json.loads(receipt.usage)
        assert "credits_micro" not in saved and "actual_micro" not in saved
        assert session.get(CreditReservation, saved["reservation_id"]).status == "reconciling"
        assert session.get(Job, d["clarify_job_id"]).status == "failed_dead"
        assert session.get(IdeaSession, d["sid"]).current_revision_id == revision
        assert session.get(IdeaRevision, revision).source == "founder"
    held = get_account(d["vid"]).reserved_micro
    assert held == saved["estimate_micro"] > 0
    requeue(d)
    run_one(d)
    assert len(calls) == 1 and get_account(d["vid"]).reserved_micro == held


def test_pg_complete_zero_clarifier_usage_settles_zero(clarifier, monkeypatch):
    from openai.types import CompletionUsage
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaClarificationRun
    d = clarifier

    def zero(*args):
        response = completion()
        response.usage = CompletionUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        return response

    calls = install_provider(monkeypatch, zero)
    run_one(d)
    with d["factory"]() as session:
        receipt = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        saved = json.loads(receipt.usage)
        assert receipt.state == "completed" and saved["credits_micro"] == 0
        assert saved["prompt_tokens"] == saved["completion_tokens"] == saved["total_tokens"] == 0
        hold = session.get(CreditReservation, saved["reservation_id"])
        assert hold.status == "settled" and hold.settled_micro == 0
        assert session.get(Job, d["clarify_job_id"]).status == "succeeded"
    assert len(calls) == 1


def test_pg_invalid_content_requires_explicit_retry_with_distinct_receipt(clarifier, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea.service import retry_failed_clarification
    from app.tenancy import bind
    d, count = clarifier, []

    def respond(*args):
        response = completion()
        count.append(True)
        if len(count) == 1:
            response.choices[0].message.content = "not JSON"
        return response

    calls = install_provider(monkeypatch, respond)
    run_one(d)
    with d["factory"]() as session:
        old = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        old_hold = json.loads(old.usage)["reservation_id"]
        assert old.state == "failed" and old.error_code == "clarification_invalid_output"
        assert json.loads(old.usage)["settled"] is True
        assert session.get(Job, d["clarify_job_id"]).status == "failed_dead"
    assert len(calls) == 1
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        retried = retry_failed_clarification(d["sid"], venture_id=d["vid"], actor_subject=d["actor"])
    d["clarify_job_id"] = retried["job_id"]
    run_one(d)
    with d["factory"]() as session:
        fresh = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        assert fresh.state == "completed" and json.loads(fresh.usage)["reservation_id"] != old_hold
    assert len(calls) == 2


@pytest.mark.parametrize("status", [None, 400, 408, 409])
def test_pg_initial_question_uncertainty_pauses_without_canned_turn_or_new_job(clarifier, monkeypatch, status):
    from app.credits.accounts import get_account
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaSession
    from app.idea.service import post_message
    from app.tenancy import bind
    from llm_gateway.errors import ProviderCallError
    d = clarifier
    with d["factory"]() as session:
        job = session.get(Job, d["clarify_job_id"])
        payload = json.loads(job.payload_json)
        payload["first_question_fallback"] = True
        job.payload_json = json.dumps(payload)
        revision = session.get(IdeaSession, d["sid"]).current_revision_id
        before = len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"])).all())
        session.commit()

    def timeout(*args):
        if status is not None:
            raise ProviderCallError("provider_down", "private transport body", status=status, retryable=True)
        raise TimeoutError("private transport body")

    calls = install_provider(monkeypatch, timeout)
    run_one(d)
    from app.idea.runner import post_fallback_question
    assert post_fallback_question(payload) is False
    with d["factory"]() as session:
        assert session.get(Job, d["clarify_job_id"]).status == "failed_dead"
        receipt = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        assert receipt.state == "uncertain" and receipt.response is None
        assert session.get(IdeaSession, d["sid"]).current_revision_id == revision
        assert len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"])).all()) == before
    held = get_account(d["vid"]).reserved_micro
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]), pytest.raises(RuntimeError, match="clarification_outcome_uncertain"):
        post_message(d["sid"], "A new reply must not dispatch another paid attempt", actor_subject=d["actor"])
    with d["factory"]() as session:
        assert len(session.scalars(select(Job).where(Job.venture_id == d["vid"], Job.kind == "idea.clarify_turn")).all()) == 1
        assert len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"])).all()) == before
    assert len(calls) == 1 and get_account(d["vid"]).reserved_micro == held > 0


def test_pg_response_recovery_does_not_redial_or_charge_twice(clarifier, monkeypatch):
    from app.idea import runner
    from app.db.models import CreditLedgerEntry, Job
    from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
    from core.clock import utcnow
    from core.ids import new_id
    d = clarifier
    original = runner._apply
    def crash_after_receipt(*args):
        raise RuntimeError("simulated worker death before domain commit")
    monkeypatch.setattr(runner, "_apply", crash_after_receipt)
    run_one(d)
    with d["factory"]() as session:
        run = session.scalars(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"])).one()
        assert run.state == "responded" and json.loads(run.usage)["settled"]
        reservation = json.loads(run.usage)["reservation_id"]
        # A separate prepared attempt must fence new I/O, not prevent recovery
        # of this exact already-paid response with the same current revision.
        session.add(IdeaClarificationRun(id=new_id("icr"), job_id=d["job_id"], session_id=d["sid"],
            workspace_id=d["wid"], venture_id=d["vid"], revision_id=run.revision_id,
            state="prepared", attempt=1, worker_id="later-worker", snapshot="{}", usage="{}",
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    requeue(d)
    monkeypatch.setattr(runner, "_apply", original)
    run_one(d, "recovered-worker")
    with d["factory"]() as session:
        assert session.get(Job, d["clarify_job_id"]).status == "succeeded"
        # Reaching a proposal queues nothing: GStack waits for the founder's explicit Looks right.
        assert session.get(IdeaSession, d["sid"]).state == "reviewing"  # GStack waits for an explicit Looks right
        validation_jobs = session.scalars(select(Job).where(Job.kind == "idea.validate", Job.venture_id == d["vid"])).all()
        assert len(validation_jobs) == 0
        assert len(session.scalars(select(IdeaRevision).where(IdeaRevision.session_id == d["sid"], IdeaRevision.source == "agent")).all()) == 1
        spends = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == reservation, CreditLedgerEntry.kind == "spend")).all()
        assert len(spends) == 1 and spends[0].amount_micro < 0
    assert len(d["calls"]) == 1


def test_pg_completed_clarifier_replay_is_no_io_with_other_pending_attempt(clarifier):
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun
    from core.clock import utcnow
    from core.ids import new_id
    d = clarifier
    run_one(d)
    with d["factory"]() as session:
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        assert run.state == "completed"
        saved_usage = run.usage
        session.add(IdeaClarificationRun(id=new_id("icr"), job_id=d["job_id"], session_id=d["sid"],
            workspace_id=d["wid"], venture_id=d["vid"], revision_id=run.revision_id,
            state="prepared", attempt=1, worker_id="later-worker", snapshot="{}", usage="{}",
            created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    requeue(d)
    run_one(d)
    with d["factory"]() as session:
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        assert run.state == "completed" and run.usage == saved_usage
        assert session.get(Job, d["clarify_job_id"]).status == "succeeded"
    assert len(d["calls"]) == 1


@pytest.mark.parametrize("state", ["prepared", "calling", "uncertain"])
def test_pg_other_unresolved_attempt_blocks_queued_job_input_retry_and_fallback(clarifier, state):
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaSession
    from app.idea.runner import post_fallback_question
    from app.idea.service import post_message, retry_failed_clarification
    from app.tenancy import bind
    from core.clock import utcnow
    from core.ids import new_id
    d = clarifier
    with d["factory"]() as session:
        idea = session.get(IdeaSession, d["sid"])
        prior_id = new_id("icr")
        session.add(IdeaClarificationRun(id=prior_id, job_id=d["job_id"], session_id=idea.id,
            workspace_id=idea.workspace_id, venture_id=idea.venture_id, revision_id=idea.current_revision_id,
            state=state, attempt=1, worker_id="prior-worker", snapshot="{}", usage="{}",
            created_at=utcnow(), updated_at=utcnow()))
        before = len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"])).all())
        payload = json.loads(session.get(Job, d["clarify_job_id"]).payload_json)
        session.commit()
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        with pytest.raises(RuntimeError, match="clarification_outcome_uncertain"):
            post_message(d["sid"], "Do not create another paid input", actor_subject=d["actor"])
        with pytest.raises(RuntimeError, match="clarification_outcome_uncertain"):
            retry_failed_clarification(d["sid"], venture_id=d["vid"], actor_subject=d["actor"])
    assert post_fallback_question(payload) is False
    run_one(d)
    with d["factory"]() as session:
        assert session.get(Job, d["clarify_job_id"]).status == "failed_dead"
        assert session.get(Job, d["clarify_job_id"]).last_error == "clarification_outcome_uncertain"
        assert len(session.scalars(select(IdeaClarificationRun).where(IdeaClarificationRun.session_id == d["sid"])).all()) == 1
        assert session.get(IdeaClarificationRun, prior_id).state == state
        assert len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"])).all()) == before
    assert not d["calls"]


@pytest.mark.parametrize("code,status", [("rate_limited", 429), ("bad_api_key", 401)])
def test_pg_rejected_clarifier_with_failed_release_stays_unknown_without_retry_or_fallback(clarifier, monkeypatch, code, status):
    from app.credits import gate
    from app.credits.accounts import get_account
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaSession
    from llm_gateway.errors import ProviderCallError
    d = clarifier
    with d["factory"]() as session:
        job = session.get(Job, d["clarify_job_id"])
        payload = json.loads(job.payload_json)
        payload["first_question_fallback"] = True
        job.payload_json = json.dumps(payload)
        revision = session.get(IdeaSession, d["sid"]).current_revision_id
        before = len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"])).all())
        session.commit()

    def rejected(*args):
        raise ProviderCallError(code, "private rejection body", status=status, retryable=status == 429)

    def failed_release(*args, **kwargs):
        raise RuntimeError("private release failure")

    calls = install_provider(monkeypatch, rejected)
    monkeypatch.setattr(gate, "release", failed_release)
    run_one(d)
    with d["factory"]() as session:
        receipt = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"]))
        usage = json.loads(receipt.usage)
        assert receipt.state == "uncertain" and receipt.error_code == "clarification_outcome_uncertain"
        assert receipt.response is None and "credits_micro" not in usage
        assert session.get(CreditReservation, usage["reservation_id"]).status == "reconciling"
        assert session.get(Job, d["clarify_job_id"]).status == "failed_dead"
        assert session.get(IdeaSession, d["sid"]).current_revision_id == revision
        assert len(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"])).all()) == before
    held = get_account(d["vid"]).reserved_micro
    requeue(d)
    run_one(d)
    assert len(calls) == 1 and get_account(d["vid"]).reserved_micro == held > 0


def test_pg_reclaimed_worker_cannot_apply_receipt(clarifier, monkeypatch):
    from app.db.models import Job
    from app.db.supymem_models import IdeaClarificationRun, IdeaSession
    d = clarifier
    def reclaimed(*args):
        with d["factory"]() as session:
            job = session.get(Job, d["clarify_job_id"])
            job.locked_by, job.attempts = "new-owner", job.attempts + 1
            session.commit()
        return completion()
    calls = install_provider(monkeypatch, reclaimed)
    run_one(d)
    with d["factory"]() as session:
        run = session.scalars(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == d["clarify_job_id"])).one()
        assert run.state == "responded"
        assert session.get(IdeaSession, d["sid"]).state == "clarifying"
        assert session.get(Job, d["clarify_job_id"]).locked_by == "new-owner"
    assert len(calls) == 1


def test_pg_duplicate_lease_delivery_only_one_provider_call(clarifier):
    from app.core.jobs import FatalError, lease
    from app.idea.runner import run
    from app.db.supymem_models import IdeaClarificationRun
    d = clarifier
    with d["factory"]() as session:
        job = lease(session, "delivered-twice", kinds=["idea.clarify_turn"], n=1)[0]
        session.commit()
    payload = {**job.payload, "_job_id": job.id, "_job_attempt": job.attempts, "_worker_id": job.worker_id}
    def invoke():
        try:
            run(payload)
            return "finished"
        except FatalError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))
    assert "finished" in results
    assert len(d["calls"]) == 1
    with d["factory"]() as session:
        assert len(session.scalars(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job.id)).all()) == 1


def test_pg_duplicate_before_reservation_does_not_release_active_hold(clarifier, monkeypatch):
    from threading import Event
    from app.core.jobs import FatalError, lease
    from app.db.models import CreditLedgerEntry
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import runner
    d = clarifier
    with d["factory"]() as session:
        job = lease(session, "same-delivery", kinds=["idea.clarify_turn"], n=1)[0]
        session.commit()
    assert job.id == d["clarify_job_id"], "duplicate delivery fixture leased another synthetic job"
    payload = {**job.payload, "_job_id": job.id, "_job_attempt": job.attempts, "_worker_id": job.worker_id}
    ready, release = Event(), Event()
    original = runner._perform
    invocations = []
    def paused(run_id, payload):
        invocations.append(run_id)
        if len(invocations) == 1:
            ready.set()
            assert release.wait(5)
        return original(run_id, payload)
    monkeypatch.setattr(runner, "_perform", paused)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="clarifier-reservation-fixture") as executor:
        first = executor.submit(runner.run, payload)
        try:
            entered_perform = ready.wait(5)
            if not entered_perform:
                if first.done():
                    first.result()  # Expose the original pre-perform error instead of discarding it.
                    print("clarifier fixture returned normally before entering _perform", flush=True)
                else:
                    import sys
                    from threading import enumerate as threads
                    from traceback import extract_stack
                    from sqlalchemy import text

                    frames = sys._current_frames()
                    for thread in threads():
                        frame = frames.get(thread.ident)
                        if thread.name.startswith("clarifier-reservation-fixture") and frame is not None:
                            print("clarifier fixture worker stack", [(item.filename, item.lineno, item.name)
                                  for item in extract_stack(frame)], flush=True)
                    try:
                        with d["engine"].connect() as connection:
                            waits = connection.execute(text(
                                "SELECT pid, wait_event_type, wait_event, pg_blocking_pids(pid) "
                                "FROM pg_stat_activity WHERE datname = current_database()"))
                            print("clarifier fixture PG waits", [tuple(row) for row in waits], flush=True)
                    except Exception as exc:
                        print("clarifier fixture PG diagnostics unavailable", type(exc).__name__, flush=True)
            assert entered_perform
            with pytest.raises(FatalError, match="clarification_operation_in_progress"):
                runner.run(payload)
        finally:
            release.set()
        first.result(timeout=15)
    assert len(d["calls"]) == 1 and len(invocations) == 1
    with d["factory"]() as session:
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job.id))
        usage = json.loads(run.usage)
        assert run.state == "completed" and usage["credits_micro"] > 0 and usage["settled"]
        spends = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == usage["reservation_id"], CreditLedgerEntry.kind == "spend")).all()
        assert len(spends) == 1 and spends[0].amount_micro < 0


def test_pg_duplicate_worker_dispatch_preserves_active_lease_and_heartbeat(clarifier, monkeypatch):
    from app.core.jobs import FatalError, lease
    from app.db.models import Job
    from app.worker import main
    d = clarifier
    with d["factory"]() as session:
        job = lease(session, "active-owner", kinds=["idea.clarify_turn"], n=1)[0]
        session.commit()
    async def duplicate(*args):
        raise FatalError("clarification_operation_in_progress")
    monkeypatch.setattr(main, "dispatch", duplicate)
    running = {(job.id, job.attempts)}
    asyncio.run(main._run_one(d["factory"], job, running))
    assert running == {(job.id, job.attempts)}
    with d["factory"]() as session:
        row = session.get(Job, job.id)
        assert row.status == "leased" and row.locked_by == "active-owner"


@pytest.mark.parametrize("understanding", [
    {"summary": "Collision repair estimating", "problem": "Manual collision repair estimating",
     "customer": "Collision repair shop owners", "offer": "A tool that helps shops prepare estimates"},
    {"summary": "Mentoring marketplace", "problem": "First-year students lack practical guidance",
     "customer": "First-year engineering students", "offer": "Paid sessions with retired engineers"},
])
def test_pg_confirmed_proposal_reaches_the_first_gstack_question(clarifier, monkeypatch, understanding):
    """Production 2026-10-24: the in-job question stage failed with ProgrammingError on Postgres."""
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn
    from app.config import settings
    monkeypatch.setattr(settings, "new_user_flow", "on")
    from app.db.models import Venture
    from app.db.session import serialize_writes
    from app.ventures import startup_review
    from sqlalchemy import text
    d = clarifier
    with d["factory"]() as session:  # production's migration-installed rule: revisions are append-only
        session.execute(text("CREATE OR REPLACE FUNCTION idea_revisions_append_only() RETURNS trigger AS $$ "
                             "BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql;"))
        session.execute(text("DROP TRIGGER IF EXISTS trg_idea_revisions_append_only ON idea_revisions; "
                             "CREATE TRIGGER trg_idea_revisions_append_only BEFORE UPDATE OR DELETE ON idea_revisions "
                             "FOR EACH ROW EXECUTE FUNCTION idea_revisions_append_only();"))
        session.commit()
    with d["factory"]() as session:
        serialize_writes(session, key=f"startup_review:{d['vid']}")
        startup_review._document(session, d["vid"], "internal_new_user_flow", {"enabled": True})
        session.commit()
    install_provider(monkeypatch, lambda *args: completion(proposal=True, understanding=understanding))
    run_one(d)
    with d["factory"]() as session:
        job = session.get(Job, d["clarify_job_id"])
        assert job.status == "succeeded", job.last_error
        turns = list(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == d["sid"]).order_by(ClarifierTurn.turn)))
        assert '"phase": "gstack"' in (turns[-1].options or ""), [t.text for t in turns]
