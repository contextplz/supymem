"""Actual registered release notice delivery, owned migrated PG and offline feed.

No claim that 0.1.84 has actually been published; log provider only.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from tests.integration.test_postgres_email_delivery import (
    email as email, pg_database as pg_database, journey_db as journey_db, run_jobs,
)
from tests.unit.release_notices.test_release_notices import enable, fixture_feed


@pytest.fixture
def releases(email, monkeypatch):
    from app.db.supymem_models import DesktopDevice
    from app.email import service as canonical
    from app.release_notices import service, publication as source
    d = email
    feed = fixture_feed(source, monkeypatch)
    enable(service, monkeypatch)
    now = canonical.utcnow() + timedelta(days=20)
    monkeypatch.setattr(canonical, "utcnow", lambda: now)
    with d["factory"]() as s:
        s.add(DesktopDevice(id="dev_release_" + d["actor"], person_id=d["actor"], name="Owned fixture Mac",
                            platform="macos", app_version="0.1.83"))
        s.commit()
    return d, service, source, feed, now


def test_concurrent_verified_publication_queues_one_person_version_and_reuses_witness(releases):
    from app.db.models import AnalyticsEvent, Job
    from app.db.supymem_models import EmailMessage
    d, service, source, feed, _now = releases
    barrier = Barrier(4)
    def reconcile(_):
        barrier.wait(timeout=15)
        return service.reconcile()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result["status"] == "verified" for result in pool.map(reconcile, range(4)))
    with d["factory"]() as s:
        rows = s.scalars(select(EmailMessage).where(EmailMessage.person_id == d["actor"], EmailMessage.template == "app_release")).all()
        assert len(rows) == 1 and rows[0].status == "queued" and rows[0].category == "product_updates"
        mid, key = rows[0].id, rows[0].idempotency_key
        assert s.scalar(select(func.count()).select_from(Job).where(Job.dedupe_key == "email.send:" + mid)) == 1
        assert s.scalar(select(func.count()).select_from(AnalyticsEvent).where(AnalyticsEvent.event_name == service.EVENT)) == 1
        witness = s.scalar(select(AnalyticsEvent).where(AnalyticsEvent.event_name == service.EVENT))
        assert witness.person_id is witness.workspace_id is witness.venture_id is None
        assert d["actor"] not in witness.props_json and d["address"] not in witness.props_json
    count = sum(url != source.MANIFEST_URL for url in feed["hits"])
    assert service.reconcile()["queued"] == 0
    assert sum(url != source.MANIFEST_URL for url in feed["hits"]) == count
    claimed = run_jobs(d, "email.send")
    assert any(job.id for job in claimed)
    with d["factory"]() as s:
        assert s.get(EmailMessage, mid).status == "sent"
    assert len([out for out in d["outgoing"] if out.idempotency_key == key]) == 1
    assert service.reconcile()["queued"] == 0


def test_queued_notice_current_device_retirement_prevents_actual_log_delivery(releases):
    from app.db.supymem_models import DesktopDevice, EmailMessage
    d, service, _source, _feed, now = releases
    assert service.reconcile()["queued"] == 1
    with d["factory"]() as s:
        row = s.scalar(select(EmailMessage).where(EmailMessage.person_id == d["actor"], EmailMessage.template == "app_release"))
        mid, ctx = row.id, json.loads(row.context_json)
        s.get(DesktopDevice, "dev_release_" + d["actor"]).revoked_at = now
        s.commit()
        assert service.refresh_context(s, d["actor"], ctx) is None
    run_jobs(d, "email.send")
    with d["factory"]() as s:
        row = s.get(EmailMessage, mid)
        assert row.status == "suppressed" and row.error == "source_no_longer_eligible"
    assert not d["outgoing"]


def test_unavailable_release_defers_existing_outbox_then_actual_worker_sends_once(releases):
    from app.db.models import Job
    from app.db.supymem_models import EmailMessage
    d, service, _source, feed, now = releases
    assert service.reconcile()["queued"] == 1
    with d["factory"]() as s:
        row = s.scalar(select(EmailMessage).where(EmailMessage.person_id == d["actor"], EmailMessage.template == "app_release"))
        mid, key, original = row.id, row.idempotency_key, row.context_json
    feed["status"] = 404
    run_jobs(d, "email.send")
    with d["factory"]() as s:
        row = s.get(EmailMessage, mid)
        assert row.status == "queued" and row.error == "release_source_unavailable"
        assert row.context_json == original and row.idempotency_key == key
        deferred = s.scalar(select(Job).where(Job.dedupe_key.like(f"email.send:{mid}:defer:%")))
        assert deferred is not None and deferred.status == "scheduled"
        assert deferred.run_after == now + timedelta(hours=1)
    assert not d["outgoing"]
    feed["status"] = 200
    # Lease the actual deferred job when due; no second admission or outbox.
    with d["factory"]() as s:
        deferred = s.scalar(select(Job).where(Job.dedupe_key.like(f"email.send:{mid}:defer:%")))
        from core.clock import utcnow
        deferred.run_after = deferred.scheduled_for = utcnow().replace(tzinfo=None) - timedelta(seconds=1)
        deferred_id = deferred.id
        s.commit()
    claimed = run_jobs(d, "email.send")
    assert [job.id for job in claimed] == [deferred_id]
    with d["factory"]() as s:
        assert s.get(EmailMessage, mid).status == "sent"
        assert s.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.idempotency_key == key)) == 1
    assert len([out for out in d["outgoing"] if out.idempotency_key == key]) == 1
