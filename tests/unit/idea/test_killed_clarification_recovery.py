"""Interrupted clarification calls recover without replaying unknown outcomes."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, run_queued, venture
from tests.idea_fixtures import install_provider


class _Killed(BaseException):
    pass


@pytest.mark.hosted
def test_killed_mid_call_recovers_with_founder_confirmed_fresh_attempt(client, monkeypatch):
    from app.chat.failed_jobs import body, failure_actions
    from app.core.jobs import STALE_LOCK_S, FatalError, reclaim_stale
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import runner, service
    from app.state import db
    from core.clock import utcnow

    def killed(*args):
        raise _Killed()

    calls = install_provider(monkeypatch, callback=killed)
    vid, wid, sid = venture(client)
    with pytest.raises(_Killed):
        run_queued("idea.clarify_turn")
    with db()() as session:
        job = session.scalar(select(Job).where(Job.venture_id == vid, Job.kind == "idea.clarify_turn"))
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job.id))
        assert (job.status, run.state) == ("leased", "calling")
        run_id = run.id
        usage = json.loads(run.usage)
        hold_id = usage["reservation_id"]
        account = session.get(CreditAccount, usage["account_id"])
        spent_before, reserved_before = account.spent_micro, account.reserved_micro
        job.locked_at = utcnow() - timedelta(seconds=STALE_LOCK_S + 30)
        session.commit()
        job_id = job.id
    with db()() as session:
        reclaim_stale(session)  # the real watchdog path run by every lease()
        session.commit()
    with db()() as session:
        job = session.get(Job, job_id)
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job_id))
        assert (job.status, job.last_error, run.state) == ("failed_dead", "job_outcome_uncertain", "calling")
    now = utcnow() + timedelta(minutes=5)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    receipt = runner._receipt

    def simultaneous_receipt(run_id):
        saved = receipt(run_id)
        barrier.wait(timeout=10)
        return saved

    with monkeypatch.context() as recovery:
        recovery.setattr(runner, "_receipt", simultaneous_receipt)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: runner.reconcile_interrupted_clarifications(now=now), range(2)))
    assert sorted(results) == [0, 1]
    assert runner.reconcile_interrupted_clarifications(now=now) == 0
    with db()() as session:
        run = session.get(IdeaClarificationRun, run_id)
        recovered_usage = run.usage
        assert (run.state, run.error_code) == ("uncertain", "clarification_outcome_uncertain")
        assert json.loads(run.usage) == {**usage, "settled": True, "credits_micro": 0,
            "billing_resolution": "platform_absorbed", "provider_outcome": "unknown"}
        assert session.get(CreditReservation, hold_id).status == "released"
        account = session.get(CreditAccount, usage["account_id"])
        assert account.spent_micro == spent_before
        assert account.reserved_micro == reserved_before - usage["estimate_micro"]
        entries = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == hold_id)).all()
        assert len(entries) == 1
        assert (entries[0].kind, entries[0].amount_micro) == ("release", 0)
        assert entries[0].available_after_micro == account.granted_micro - account.spent_micro - account.reserved_micro
        assert json.loads(entries[0].meta_json)["provider_outcome"] == "unknown"
        job = session.get(Job, job_id)
        payload = json.loads(job.payload_json) | {"_job_id": job.id, "_worker_id": "test-replay", "_job_attempt": 2}
        job.status, job.locked_by, job.attempts = "leased", "test-replay", 2
        session.commit()
    with pytest.raises(FatalError, match="clarification_outcome_uncertain"):
        runner._claim(payload)
    with db()() as session:
        session.get(Job, job_id).status = "failed_dead"
        session.commit()
    actions = failure_actions(vid, "idea.clarify_turn", "job_outcome_uncertain")
    assert any(action.get("action") == "job.retry" and action["label"] == "Try again" for action in actions)
    assert "Nothing was retried." in body("idea.clarify_turn", "job_outcome_uncertain")
    assert "Check your saved work before trying again." in body("idea.clarify_turn", "job_outcome_uncertain")
    assert "job_outcome_uncertain" not in body("idea.clarify_turn", "job_outcome_uncertain")
    with pytest.raises(RuntimeError, match="clarification_outcome_uncertain"):
        service.retry_failed_clarification(sid, venture_id=vid)
    fresh_calls = install_provider(monkeypatch)
    path = f"/ventures/{vid}/chat/confirm"
    prepared = request(client, "POST", path, json={"action": "job.retry", "payload": {"kind": "idea.clarify_turn"}})
    assert prepared.status_code == 200
    action = prepared.json()["actions"][0]
    assert action["label"] == "Try again"
    approved = request(client, "POST", path, json={"action": action["action"],
        "payload": action["confirmation"]["resulting_state"], "idempotency_key": "founder-killed-retry"})
    assert approved.status_code == 200
    with db()() as session:
        fresh_job = session.scalar(select(Job).where(Job.venture_id == vid, Job.id != job_id, Job.kind == "idea.clarify_turn"))
        fresh_id = fresh_job.id
        assert fresh_job.idempotency_key != session.get(Job, job_id).idempotency_key
    run_queued("idea.clarify_turn", through_job_id=fresh_id)
    assert len(calls) == len(fresh_calls) == 1
    assert runner.reconcile_interrupted_clarifications(now=now) == 0
    with db()() as session:
        old = session.get(IdeaClarificationRun, run_id)
        fresh = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == fresh_id))
        assert old.state == "uncertain" and old.usage == recovered_usage
        assert fresh.state == "completed" and json.loads(fresh.usage)["reservation_id"] != hold_id
        assert len(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == hold_id)).all()) == 1


@pytest.mark.parametrize("aged_run", [False, True])
def test_live_lease_is_untouched(client, monkeypatch, aged_run):
    from app.core.jobs import STALE_LOCK_S
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import runner
    from app.state import db
    from core.clock import utcnow

    def killed(*args):
        raise _Killed()

    calls = install_provider(monkeypatch, callback=killed)
    vid, wid, sid = venture(client)
    with pytest.raises(_Killed):
        run_queued("idea.clarify_turn")
    now = utcnow()
    with db()() as session:
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.venture_id == vid))
        job = session.get(Job, run.job_id)
        job.locked_at = now
        if aged_run:
            run.updated_at = now - timedelta(seconds=runner.CLARIFY_TIMEOUT_SECONDS + 61)
        run_id, usage, updated_at = run.id, run.usage, run.updated_at
        session.commit()
    assert runner.reconcile_interrupted_clarifications(now=now) == 0
    with db()() as session:
        run = session.get(IdeaClarificationRun, run_id)
        assert (run.state, run.usage, run.updated_at) == ("calling", usage, updated_at.replace(tzinfo=None))
        assert session.get(CreditReservation, json.loads(usage)["reservation_id"]).status == "reconciling"
        session.get(Job, run.job_id).locked_at = now - timedelta(seconds=STALE_LOCK_S + 1)
        session.commit()
    assert runner.reconcile_interrupted_clarifications(now=now) == int(aged_run)
    assert len(calls) == 1


def test_interrupted_saved_receipt_settles_without_redial(client, monkeypatch):
    from app.db.models import CreditLedgerEntry, CreditReservation, Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import runner
    from app.state import db
    from core.clock import utcnow

    save = runner._save

    def killed_after_receipt(run_id, **changes):
        if changes.get("state") == "responded":
            changes["state"] = "calling"
            save(run_id, **changes)
            raise _Killed()
        save(run_id, **changes)

    calls = install_provider(monkeypatch)
    vid, wid, sid = venture(client)
    with monkeypatch.context() as worker:
        worker.setattr(runner, "_save", killed_after_receipt)
        with pytest.raises(_Killed):
            run_queued("idea.clarify_turn")
    with db()() as session:
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.venture_id == vid))
        run_id, usage = run.id, json.loads(run.usage)
        session.get(Job, run.job_id).status = "failed_dead"
        session.commit()
    now = utcnow() + timedelta(minutes=5)
    assert runner.reconcile_interrupted_clarifications(now=now) == 1
    assert runner.reconcile_interrupted_clarifications(now=now) == 0
    assert len(calls) == 1
    with db()() as session:
        run = session.get(IdeaClarificationRun, run_id)
        hold = session.get(CreditReservation, usage["reservation_id"])
        assert run.state == "uncertain" and run.response
        assert json.loads(run.usage)["billing_resolution"] == "confirmed_charge"
        assert json.loads(run.usage)["settled"] is True
        assert hold.status == "settled"
        entries = session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == hold.id)).all()
        assert len(entries) == 1 and entries[0].kind == "spend"


@pytest.mark.parametrize("malformed", ["job_kind", "usage_source"])
def test_malformed_run_does_not_block_later_stale_run(client, monkeypatch, caplog, malformed):
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import IdeaClarificationRun
    from app.idea import runner
    from app.state import db
    from core.clock import utcnow

    def killed(*args):
        raise _Killed()

    calls = install_provider(monkeypatch, callback=killed)
    run_ids = []
    now = utcnow()
    for age in (6, 5):
        vid, wid, sid = venture(client)
        with pytest.raises(_Killed):
            run_queued("idea.clarify_turn")
        with db()() as session:
            run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.venture_id == vid))
            run_ids.append(run.id)
            job = session.get(Job, run.job_id)
            job.status = "failed_dead"
            run.updated_at = now - timedelta(minutes=age)
            if age == 6:
                if malformed == "job_kind":
                    job.kind = "research.deep"
                else:
                    run.usage = json.dumps({**json.loads(run.usage), "source": "unknown"})
                malformed_usage = run.usage
            session.commit()
    with caplog.at_level("WARNING", logger="app.idea.runner"):
        assert runner.reconcile_interrupted_clarifications(now=now) == 1
    warnings = [record for record in caplog.records if record.name == "app.idea.runner"]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "clarification_reconciliation_scope_mismatch"
    assert warnings[0].run_id == run_ids[0]
    assert len(calls) == 2
    with db()() as session:
        malformed_run, valid_run = [session.get(IdeaClarificationRun, run_id) for run_id in run_ids]
        assert (malformed_run.state, malformed_run.usage) == ("calling", malformed_usage)
        assert session.get(CreditReservation, json.loads(malformed_usage)["reservation_id"]).status == "reconciling"
        assert (valid_run.state, valid_run.error_code) == ("uncertain", "clarification_outcome_uncertain")
        assert session.get(CreditReservation, json.loads(valid_run.usage)["reservation_id"]).status == "released"
