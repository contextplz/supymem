"""Migrated PostgreSQL admission races for background lifecycle mail.

Uses an owned loopback database and the log adapter. No real email is sent.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import json

import pytest
from sqlalchemy import func, select

from tests.integration.test_postgres_email_delivery import (
    body, email as email, pg_database as pg_database, journey_db as journey_db, post_event, receipts, run_jobs,
)


def race(call, count=8):
    barrier = Barrier(count)
    def work(index):
        barrier.wait(timeout=15)
        return call(index)
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(work, range(count)))


def messages(d):
    from app.db.supymem_models import EmailMessage
    with d["factory"]() as session:
        return session.scalars(select(EmailMessage).where(EmailMessage.person_id == d["actor"])).all()


@pytest.fixture
def proof_founder(email):
    from app.db.models import Person
    from app.email.proof import ALLOWED_RECIPIENT
    d = email
    with d["factory"]() as session:
        person = session.get(Person, d["actor"])
        previous = person.email
        person.email = ALLOWED_RECIPIENT
        session.commit()
    try:
        yield d
    finally:
        with d["factory"]() as session:
            session.get(Person, d["actor"]).email = previous
            session.commit()


@pytest.mark.parametrize("default_hold", [False, True])
def test_targeted_founder_proof_uses_normal_worker_and_signed_receipt_while_other_mail_stays_held(proof_founder, monkeypatch, default_hold):
    from app.db.models import ChatMessage, Person
    from app.email import proof, providers, service
    d = proof_founder
    now = service.utcnow()
    template = "progress_summary" if default_hold else "milestone"
    other = "usr_unlisted_proof_" + d["actor"]
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", template)
    with d["factory"]() as session:
        session.add(Person(id=other, email="unlisted@example.test", display_name="Synthetic unlisted",
                           created_at=now - timedelta(days=30)))
        for n in range(2 if default_hold else 1):
            session.add(ChatMessage(id=f"msg_targeted_{d['vid']}_{n}", venture_id=d["vid"], role="assistant", kind="plan_ready",
                                   body_md="The saved plan is ready.", created_at=now - timedelta(hours=3, minutes=n)))
        session.commit()
    # A real queued message predating the scoped hold must not escape it.
    held_id = service.send_email(other, template, {"what": "plan"}, ref="prior-queue")
    if default_hold:
        monkeypatch.delenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED")
        monkeypatch.delenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST")
    else:
        monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "beta_invite")
    monkeypatch.setenv("SUPYMEM_EMAIL_PROOF_PERSON_ALLOWLIST", d["actor"])
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "offline-targeted-proof-provider")
    provider_id = "offline_targeted_" + d["vid"]
    def fake_provider(message):
        d["outgoing"].append(message)
        return provider_id if message.idempotency_key == proof_key else "offline_unrelated_" + message.idempotency_key
    monkeypatch.setitem(providers.PROVIDERS, "resend", fake_provider)
    queued = proof.queue()
    mid = queued["message_id"]
    assert queued["provider_called_by_script"] is False
    assert service.send_email(other, template, {"proof_person_id": d["actor"]}, ref="new-unlisted") is None
    from app.db.supymem_models import EmailMessage
    with d["factory"]() as session:
        proof_key = session.get(EmailMessage, mid).idempotency_key
        held_key = session.get(EmailMessage, held_id).idempotency_key
    jobs = run_jobs(d, "email.send")
    assert {job.payload["message_id"] for job in jobs} >= {held_id, mid}
    outgoing = [message for message in d["outgoing"] if message.idempotency_key == proof_key]
    assert len(outgoing) == 1
    assert outgoing[0].to == proof.ALLOWED_RECIPIENT
    assert not any(message.idempotency_key == held_key for message in d["outgoing"])
    with d["factory"]() as session:
        sent = session.get(EmailMessage, mid)
        held_message = session.get(EmailMessage, held_id)
        assert sent.status == "sent" and sent.template == template
        assert held_message.status == "queued" and held_message.error == "lifecycle_disabled"
    checked = proof.check(mid)
    assert checked["worker_succeeded"] and not checked["signed_delivered_receipt"], checked
    d["address"] = proof.ALLOWED_RECIPIENT
    from fastapi.testclient import TestClient
    with TestClient(d["app"]) as client:
        assert post_event(client, body(d, provider_id)).status_code == 200
    checked = proof.check(mid)
    assert checked["provider_accepted"] and checked["worker_succeeded"] and checked["signed_delivered_receipt"]
    assert not run_jobs(d, "email.send")
    assert len([message for message in d["outgoing"] if message.idempotency_key == proof_key]) == 1
    assert not any(message.idempotency_key == held_key for message in d["outgoing"])


def _hourly_clock(monkeypatch, offset):
    from datetime import timezone
    from app.api import internal_tick
    from app.email import service
    now = [service.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(days=20, hours=offset)]
    class Clock:
        @staticmethod
        def now(tz=None):
            return now[0].replace(tzinfo=timezone.utc)
    monkeypatch.setattr(internal_tick, "datetime", Clock)
    return now


def _hourly_worker(d):
    from app.tasks.scheduler import dispatch_tick
    tick = dispatch_tick("hourly")
    claimed = run_jobs(d, "email.schedule")
    assert any(job.id in tick["ids"] for job in claimed)
    return tick


@pytest.mark.parametrize("orphan", ["missing", "failed"])
@pytest.mark.parametrize("opt_out", [False, True])
def test_hourly_worker_recovers_orphan_once_and_rechecks_preferences(email, monkeypatch, orphan, opt_out):
    from app.tasks.scheduler import dispatch_tick
    from app.db.models import Job
    from app.db.supymem_models import EmailMessage
    from app.email import service
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "milestone")
    clock = _hourly_clock(monkeypatch, ((2 if orphan == "failed" else 0) + int(opt_out)) * 3)
    mid = service.send_email(d["actor"], "milestone", {"what": "plan", "path": f"/v/{d['vid']}/plan"}, ref="orphan")
    with d["factory"]() as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid))
        if orphan == "missing":
            session.delete(job)
        else:
            job.status, job.attempts = "failed", 1
        session.commit()
    tick = _hourly_worker(d)
    assert dispatch_tick("hourly")["ids"] == tick["ids"] and run_jobs(d, "email.schedule") == []
    with d["factory"]() as session:
        recovered = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid + ":recover"))
        assert recovered is not None and recovered.attempts == (1 if orphan == "failed" else 0)
        assert recovered.max_attempts == service.MAX_ATTEMPTS
    # Consent can change after recovery and before the ordinary send worker leases it.
    if opt_out:
        service.set_prefs(d["actor"], {"product_updates": False})
    claimed = run_jobs(d, "email.send")
    assert any(job.id == recovered.id for job in claimed)
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        assert row.status == ("suppressed" if opt_out else "sent")
        assert row.error == ("suppressed_at_send" if opt_out else None)
        key = row.idempotency_key
    outgoing = [message for message in d["outgoing"] if message.idempotency_key == key]
    assert len(outgoing) == (0 if opt_out else 1)
    clock[0] += timedelta(hours=1)
    _hourly_worker(d)
    run_jobs(d, "email.send")
    assert service.deliver(mid) == ("suppressed" if opt_out else "sent")
    assert len([message for message in d["outgoing"] if message.idempotency_key == key]) == len(outgoing)
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(
            Job.dedupe_key == "email.send:" + mid + ":recover")) == 1


def test_orphaned_prepared_send_preserves_payload_and_remaining_budget(email, monkeypatch):
    from app.core.jobs import RetryableError
    from app.db.models import Job, Person
    from app.email import providers, service
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "milestone")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "owned-local-key")
    _hourly_clock(monkeypatch, 300)
    calls = []
    def accepted_after_retry(message):
        calls.append(message)
        if len(calls) == 1:
            raise providers.ProviderError("resend_http_503", True)
        return "owned-orphan-recovered"
    monkeypatch.setitem(providers.PROVIDERS, "resend", accepted_after_retry)
    mid = service.send_email(d["actor"], "milestone", {"what": "plan", "path": f"/v/{d['vid']}/plan"}, ref="prepared-orphan")
    with pytest.raises(RetryableError):
        service.deliver(mid)
    with d["factory"]() as session:
        session.delete(session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid)))
        session.get(Person, d["actor"]).display_name = "Changed after preparation"
        session.commit()
    monkeypatch.setenv("EMAIL_REPLY_TO", "changed@example.test")
    _hourly_worker(d)
    with d["factory"]() as session:
        recovered = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid + ":recover"))
        assert recovered.attempts == 1 and recovered.max_attempts == service.MAX_ATTEMPTS
    run_jobs(d, "email.send")
    assert len(calls) == 2 and calls[0] == calls[1]
    assert service.deliver(mid) == "sent" and len(calls) == 2


def test_hourly_recovery_does_not_charge_five_successful_quiet_deferrals(email, monkeypatch):
    from app.core.jobs import enqueue
    from app.db.models import AnalyticsEvent, Job
    from app.db.supymem_models import EmailMessage
    from app.email import service, triggers
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "welcome")
    _hourly_clock(monkeypatch, 400)
    clock = [service.utcnow()]
    monkeypatch.setattr(service, "utcnow", lambda: clock[0])
    monkeypatch.setattr(triggers, "utcnow", lambda: clock[0])
    mid = service.send_email(d["actor"], "welcome", ref="quiet-orphan")
    with d["factory"]() as session:
        key = session.get(EmailMessage, mid).idempotency_key
        session.add(AnalyticsEvent(id="activity_quiet_" + d["actor"], person_id=d["actor"], event_name="session.active", occurred_at=clock[0], props_json="{}"))
        session.commit()
    def owned_outgoing():
        # The shared-database worker can also send unrelated queued fixtures.
        return [message for message in d["outgoing"] if message.idempotency_key == key]
    assert len(run_jobs(d, "email.send")) >= 1
    for index in range(4):
        with d["factory"]() as session:
            enqueue(session, "email.send", {"message_id": mid}, dedupe_key=f"email.send:{mid}:owned-replay:{index}")
            session.commit()
        run_jobs(d, "email.send")
    assert not owned_outgoing()
    with d["factory"]() as session:
        related = session.scalars(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid))).all()
        assert sum(job.status == "succeeded" and job.attempts == 1 for job in related) == 5
        for job in related:
            if job.status == "scheduled":
                session.delete(job)  # The sole future intent was lost after successful deferral.
        assert "_email_first_attempt_at" not in json.loads(session.get(EmailMessage, mid).context_json)
        session.commit()
    _hourly_worker(d)
    with d["factory"]() as session:
        recovery = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid + ":recover"))
        assert recovery.attempts == 0
    run_jobs(d, "email.send")
    assert not owned_outgoing() and messages(d)[0].error == "quiet_after_activity"
    with d["factory"]() as session:
        future = session.scalar(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid + ":defer:"), Job.status == "scheduled"))
        clock[0] = future.run_after
        future.run_after = future.scheduled_for = None  # Lease through the normal worker at the advanced policy clock.
        session.commit()
    run_jobs(d, "email.send")
    assert messages(d)[0].status == "sent" and len(owned_outgoing()) == 1
    assert service.deliver(mid) == "sent" and len(owned_outgoing()) == 1


@pytest.mark.parametrize("retried_leases,expected_attempts", [(4, 3), (6, None)])
def test_orphan_recovery_retains_retries_before_successful_deferral(email, monkeypatch, retried_leases, expected_attempts):
    from app.db.models import Job
    from app.email import service
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "welcome")
    _hourly_clock(monkeypatch, 500 + retried_leases)
    mid = service.send_email(d["actor"], "welcome", ref="retried-before-deferral")
    with d["factory"]() as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid))
        job.status, job.attempts = "succeeded", retried_leases
        session.commit()
    _hourly_worker(d)
    with d["factory"]() as session:
        recovery = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid + ":recover"))
        if expected_attempts is None:
            assert recovery is None
        else:
            assert recovery is not None and recovery.attempts == expected_attempts
        if recovery:
            recovery.status = "cancelled"  # Retire this intentionally unexecuted inspection fixture.
            session.commit()


@pytest.mark.parametrize("status", ["scheduled", "retry_wait", "leased", "running"])
def test_hourly_recovery_keeps_live_or_future_send_job(email, monkeypatch, status):
    from app.db.models import Job
    from app.email import service
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "welcome")
    _hourly_clock(monkeypatch, 100 + ["scheduled", "retry_wait", "leased", "running"].index(status))
    mid = service.send_email(d["actor"], "welcome", ref="live-send")
    with d["factory"]() as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid))
        job.status = status
        job.run_after = job.scheduled_for = service.utcnow() + timedelta(days=1)
        if status in {"leased", "running"}:
            job.locked_at, job.locked_by = service.utcnow(), "owned-existing-worker"
        session.commit()
    _hourly_worker(d)
    with d["factory"]() as session:
        assert session.scalar(select(Job.id).where(Job.dedupe_key == "email.send:" + mid + ":recover")) is None
        assert session.get(Job, job.id).status == status


@pytest.mark.parametrize("exhausted", [True, False])
@pytest.mark.parametrize("status", ["failed", "failed_dead"])
def test_hourly_recovery_does_not_restart_exhausted_or_failed_recovery(email, monkeypatch, exhausted, status):
    from app.db.models import Job
    from app.db.supymem_models import EmailMessage
    from app.email import service
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "welcome")
    _hourly_clock(monkeypatch, 200 + int(exhausted) + (2 if status == "failed_dead" else 0))
    mid = service.send_email(d["actor"], "welcome", ref="bounded-recovery")
    with d["factory"]() as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid))
        job.status, job.attempts = status, service.MAX_ATTEMPTS if exhausted else 1
        if not exhausted:
            job.dedupe_key = "email.send:" + mid + ":recover"
        session.commit()
    _hourly_worker(d)
    with d["factory"]() as session:
        related = session.scalars(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid))).all()
        assert len(related) == 1 and related[0].status == status
        assert session.get(EmailMessage, mid).status == "queued"
    assert not d["outgoing"]


def test_actual_worker_preserves_no_dispatch_clock_across_config_and_orphan_recovery(email, monkeypatch):
    from app.db.models import AnalyticsEvent, Job
    from app.db.supymem_models import EmailMessage
    from app.email import providers, service, triggers
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "welcome")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    hourly = _hourly_clock(monkeypatch, 600)
    clock = [service.utcnow() - timedelta(days=2)]
    for module in (service, triggers):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    mid = service.send_email(d["actor"], "welcome", ref="never-dispatched-recovery")
    calls = []
    monkeypatch.setitem(providers.PROVIDERS, "resend", lambda message: calls.append(message) or "owned-config-recovery")
    run_jobs(d, "email.send")
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        assert row.status == "failed" and row.error == "resend_not_configured"
        assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is None
        key = row.idempotency_key
    assert not calls
    clock[0] += timedelta(days=2)
    monkeypatch.setenv("RESEND_API_KEY", "owned-local-fake-only")
    assert service.requeue_failed() >= 1
    with d["factory"]() as session:
        requeued = session.scalar(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid + ":requeue:")))
        requeued.attempts = 1  # A retried lease had no provider preparation or I/O.
        session.add(AnalyticsEvent(id="activity_config_" + d["actor"], person_id=d["actor"], event_name="session.active", occurred_at=clock[0], props_json="{}"))
        session.commit()
    run_jobs(d, "email.send")
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        assert row.status == "queued" and row.error == "quiet_after_activity"
        assert "_email_first_attempt_at" not in json.loads(row.context_json)
        assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is None
        future = session.scalar(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid + ":defer:"), Job.status == "scheduled"))
        session.delete(future)  # Lose only the future intent; restore it via the actual hourly worker.
        session.commit()
    assert not [message for message in calls if message.idempotency_key == key]
    _hourly_worker(d)
    with d["factory"]() as session:
        recovered = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid + ":recover"))
        assert recovered is not None and recovered.attempts < recovered.max_attempts
        assert "_email_first_attempt_at" not in json.loads(session.get(EmailMessage, mid).context_json)
    clock[0] += timedelta(hours=1, seconds=1)
    claimed = run_jobs(d, "email.send")
    assert any(job.id == recovered.id for job in claimed)
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        assert row.status == "sent" and row.provider_id == "owned-config-recovery"
        assert json.loads(row.context_json)["_email_first_attempt_at"] == clock[0].isoformat()
    hourly[0] += timedelta(hours=1)
    _hourly_worker(d)
    run_jobs(d, "email.send")
    assert service.deliver(mid) == "sent"
    assert len([message for message in calls if message.idempotency_key == key]) == 1



def test_actual_fresh_worker_lease_preserves_legacy_uncertainty_after_quiet_deferral(email, monkeypatch):
    from app.db.models import AnalyticsEvent, Job
    from app.db.supymem_models import EmailMessage
    from app.email import service, triggers
    d = email
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "welcome")
    _hourly_clock(monkeypatch, 650)
    now = service.utcnow()
    clock = [now - timedelta(hours=2)]
    for module in (service, triggers):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    mid = service.send_email(d["actor"], "welcome", ref="legacy-fresh-quiet-lease")
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        key = row.idempotency_key
        context = json.loads(row.context_json)
        context.pop("_email_admission_version")  # Explicitly represent a pre-journal legacy crash.
        row.context_json = json.dumps(context)
        original = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid))
        original.attempts = 1
        session.add(AnalyticsEvent(id="activity_legacy_quiet_" + d["actor"], person_id=d["actor"],
            event_name="session.active", occurred_at=now, props_json="{}"))
        session.commit()
    clock[0] = now
    first = run_jobs(d, "email.send")
    assert any(job.id == original.id and job.attempts == 2 for job in first)
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        assert row.status == "queued" and row.error == "quiet_after_activity"
        context = json.loads(row.context_json)
        assert context["_email_first_attempt_at"] == (now - timedelta(hours=2)).isoformat()
        assert "_email_delivery_payload" not in context
        assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is not None
        future = session.scalar(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid + ":defer:"), Job.status == "scheduled"))
        assert future.attempts == 0
        future.run_after = future.scheduled_for = None
        session.commit()
    clock[0] = now + timedelta(days=2)
    fresh = run_jobs(d, "email.send")
    assert any(job.id == future.id and job.attempts == 1 for job in fresh)
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        assert row.status == "failed" and row.error == "email_retry_window_expired"
        assert session.get(Job, future.id).status == "failed_dead"
        assert json.loads(row.context_json)["_email_first_attempt_at"] == (now - timedelta(hours=2)).isoformat()
    assert not [message for message in d["outgoing"] if message.idempotency_key == key]
    assert service.deliver(mid) == "failed"

def test_distinct_marketing_events_race_for_exactly_one_daily_slot(email):
    from app.db.models import Job
    from app.email import service
    d = email
    results = race(lambda index: service.send_email(d["actor"], "milestone", {"what": "plan"}, ref="event:" + str(index)))
    queued = [mid for mid in results if mid]
    assert len(queued) == 1 and len(messages(d)) == 1
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(Job.dedupe_key == "email.send:" + queued[0])) == 1


def test_distinct_transactional_events_cannot_exceed_five_daily_reservations(email):
    from app.email import service
    d = email
    results = race(lambda index: service.send_email(d["actor"], "needs_you", ref="action:" + str(index)))
    # The founder-wide cap (2 Supymem emails a day) is tighter than the transactional cap and must hold under races too.
    cap = min(service.DAILY_TRANSACTIONAL_CAP, service.FOUNDER_DAILY_CAP)
    assert len([mid for mid in results if mid]) == cap
    assert len(messages(d)) == cap


def test_concurrent_global_hourly_cap_is_atomic_across_people(email, monkeypatch):
    from app.db.models import Person
    from app.db.supymem_models import EmailMessage
    from app.email import service
    d = email
    with d["factory"]() as session:
        already_counted = session.scalar(select(func.count()).select_from(EmailMessage).where(
            service._counted_sends(service.utcnow() - timedelta(hours=1)))) or 0
        actors = [d["actor"] + "_" + str(index) for index in range(4)]
        session.add_all(Person(id=actor, email=actor + "@example.test", display_name="Owned fixture", created_at=service.utcnow()) for actor in actors)
        session.commit()
    monkeypatch.setenv("EMAIL_GLOBAL_HOURLY_CAP", str(already_counted + 1))
    mids = [service.send_email(actor, "welcome", ref="hourly") for actor in actors]
    statuses = race(lambda index: service.deliver(mids[index]), len(mids))
    assert statuses.count("sent") == 1 and statuses.count("deferred") == 3
    assert len(d["outgoing"]) == 1
    with d["factory"]() as session:
        rows = [session.get(EmailMessage, mid) for mid in mids]
        assert sum(row.error == "global_hourly_cap" for row in rows) == 3


def test_delivery_cap_blocks_two_aged_outbox_rows_even_when_workers_race(email):
    from app.db.supymem_models import EmailMessage
    from app.email import service
    d = email
    first = service.send_email(d["actor"], "milestone", {"what": "plan"}, ref="aged")
    with d["factory"]() as session:
        session.get(EmailMessage, first).created_at = service.utcnow() - timedelta(days=3)
        session.commit()
    second = service.send_email(d["actor"], "milestone", {"what": "research"}, ref="fresh")
    mids = [first, second]
    statuses = race(lambda index: service.deliver(mids[index]), 2)
    assert sorted(statuses) == ["deferred", "sent"] and len(d["outgoing"]) == 1


def test_delivered_receipt_does_not_release_daily_send_budget(email):
    from fastapi.testclient import TestClient
    from app.email import service
    d = email
    mid = service.send_email(d["actor"], "milestone", {"what": "plan"}, ref="delivered")
    assert service.deliver(mid) == "sent"
    message = messages(d)[0]
    with TestClient(d["app"]) as client:
        assert post_event(client, body(d, message.provider_id)).status_code == 200
    assert service.send_email(d["actor"], "suggestions_waiting", {"items": ["Saved suggestion"]}, ref="same-day") is None
    assert messages(d)[0].status == "sent"


def test_concurrent_preference_changes_append_immutable_opt_in_receipts(email):
    from app.db.models import AnalyticsEvent
    from app.email import service
    d = email
    assert not service.get_prefs(d["actor"])["digests"]
    assert all(result["digests"] for result in race(lambda _: service.set_prefs(d["actor"], {"digests": True})))
    with d["factory"]() as session:
        receipts = session.scalars(select(AnalyticsEvent).where(AnalyticsEvent.person_id == d["actor"],
            AnalyticsEvent.event_name == "email.preferences_changed")).all()
        assert len(receipts) == 8 and len({event.occurred_at for event in receipts}) == 8


def test_concurrent_requeue_of_config_failure_creates_one_new_send_job(email, monkeypatch):
    import pytest
    from app.core.jobs import FatalError
    from app.db.models import Job
    from app.email import service
    d = email
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    mid = service.send_email(d["actor"], "welcome", ref="missing-config")
    with pytest.raises(FatalError, match="resend_not_configured"):
        service.deliver(mid)
    assert sum(race(lambda _: service.requeue_failed())) == 1
    with d["factory"]() as session:
        jobs = session.scalars(select(Job).where(Job.dedupe_key.startswith("email.send:" + mid + ":requeue:"))).all()
        assert len(jobs) == 1 and messages(d)[0].status == "queued"


def test_provider_acceptance_then_database_rollback_preserves_payload_and_caps(email, monkeypatch):
    import asyncio
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError
    from app.core.jobs import lease
    from app.db.models import AnalyticsEvent, Job, Person
    from app.db.supymem_models import EmailMessage
    from app.email import providers, service
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    d = email
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "owned-local-key")
    with d["factory"]() as session:
        accepted_before = session.scalar(select(func.count()).select_from(EmailMessage).where(
            service._counted_sends(service.utcnow() - timedelta(hours=1)))) or 0
        other = d["actor"] + "_other"
        session.add(Person(id=other, email=other + "@example.test", display_name="Owned fixture", created_at=service.utcnow()))
        session.commit()
    monkeypatch.setenv("EMAIL_GLOBAL_HOURLY_CAP", str(accepted_before + 1))
    calls, fail = [], [True]
    def accepted(message):
        calls.append(message)
        return "owned-recovered-provider-id"
    monkeypatch.setitem(providers.PROVIDERS, "resend", accepted)
    session_type = d["factory"].class_
    commit = session_type.commit
    def broken_commit(session):
        if calls and fail[0]:
            fail[0] = False
            raise OperationalError("owned-statement", {}, RuntimeError("private@example.test: private fixture idea"))
        return commit(session)
    monkeypatch.setattr(session_type, "commit", broken_commit)
    mid = service.send_email(d["actor"], "milestone", {"what": "plan"}, ref="database-rollback")
    install_default_handlers()
    with d["factory"]() as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid))
        job.priority = 0  # Lease this owned fixture before unrelated earlier jobs.
        session.commit()
        claimed = lease(session, "owned-rollback-worker", kinds=["email.send"])[0]
        session.commit()
        assert claimed.id == job.id
    asyncio.run(_run_one(d["factory"], claimed, {claimed.id}))
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        saved = json.loads(row.context_json)
        assert row.status == "queued" and row.provider_id is None and row.sent_at is None
        assert saved["_email_delivery_payload"] and saved["_email_first_attempt_at"]
        assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is not None
        job = session.get(Job, claimed.id)
        assert job.status == "retry_wait" and job.attempts == 1
        assert "private" not in job.last_error
        session.get(Person, d["actor"]).display_name = "Changed fixture name"
        session.commit()
    assert service.send_email(d["actor"], "suggestions_waiting", ref="distinct-same-person") is None
    distinct = service.send_email(other, "welcome", ref="distinct-other-person")
    assert service.deliver(distinct) == "deferred" and len(calls) == 1
    monkeypatch.setenv("EMAIL_REPLY_TO", "changed@example.test")
    with d["factory"]() as session:
        reclaimed = lease(session, "owned-recovery-worker", kinds=["email.send"], now=service.utcnow() + timedelta(minutes=5))[0]
        session.commit()
        assert reclaimed.id == claimed.id and reclaimed.attempts == 2
    asyncio.run(_run_one(d["factory"], reclaimed, {reclaimed.id}))
    assert len(calls) == 2 and calls[0] == calls[1]
    with d["factory"]() as session:
        row = session.get(EmailMessage, mid)
        assert row.provider_id == "owned-recovered-provider-id" and row.sent_at
        assert session.get(Job, claimed.id).status == "succeeded"
    with TestClient(d["app"]) as client:
        event_id = "msg_owned_rollback_receipt"
        assert post_event(client, body(d, "owned-recovered-provider-id"), event_id).status_code == 200
        assert post_event(client, body(d, "owned-recovered-provider-id"), event_id).status_code == 200
    assert len(receipts(d)) == 1
