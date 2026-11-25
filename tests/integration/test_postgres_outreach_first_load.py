"""Real migrated PostgreSQL: parallel first-load reads share one settings row."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def readers(service):
    return [service.outreach_status, service.onboarding_status, service.list_queue] * 2


def test_parallel_status_onboarding_queue_initialize_once(journey_db, monkeypatch):
    from app.db.models import GtmOutreachSettings
    from app.gtm.relationships import service
    d = journey_db
    barrier = Barrier(6)
    original = d["factory"].class_.get
    # Deterministically recreate the native race: all six requests observe the
    # missing row before any initializer may insert. No database I/O is mocked.
    def synchronized_get(session, model, key, *args, **kwargs):
        row = original(session, model, key, *args, **kwargs)
        if model is GtmOutreachSettings and key == d["wid"] and row is None:
            barrier.wait(timeout=20)
        return row
    monkeypatch.setattr(d["factory"].class_, "get", synchronized_get)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda read: read(d["wid"]), readers(service)))
    assert all(result["ready"] is False for result in results)
    assert results[2]["items"] == results[5]["items"] == []
    with d["factory"]() as session:
        rows = session.scalars(select(GtmOutreachSettings).where(GtmOutreachSettings.workspace_id == d["wid"])).all()
        assert len(rows) == 1
        assert rows[0].outbound_mode == "human_clipboard"
        assert rows[0].sender_geo is None and rows[0].aup_accepted_at is None


def test_initialization_conflict_preserves_winning_settings(journey_db, monkeypatch):
    from app.db.models import GtmOutreachSettings
    from app.gtm.relationships import service
    from core.clock import utcnow
    d = journey_db
    now = utcnow().replace(tzinfo=None)  # Settings columns use timestamp without time zone.
    original = d["factory"].class_.get
    injected = False
    preserved = dict(outbound_mode="gmail_composio", sender_geo="US", sender_postal_address="Founder office",
                     composio_user_id="owned-reference", aup_accepted_at=now, dpa_accepted_at=now,
                     max_connect_per_week=3, max_dm_per_week=1, max_email_per_week=2,
                     send_enabled=0, created_at=now, updated_at=now, onboarding_status='{"preserve":true}')
    def concurrent_winner(session, model, key, *args, **kwargs):
        nonlocal injected
        row = original(session, model, key, *args, **kwargs)
        if model is GtmOutreachSettings and key == d["wid"] and row is None and not injected:
            injected = True
            with d["factory"]() as winner:
                winner.add(GtmOutreachSettings(workspace_id=d["wid"], **preserved))
                winner.commit()
        return row
    monkeypatch.setattr(d["factory"].class_, "get", concurrent_winner)
    with d["factory"]() as session:
        row = service._settings(session, d["wid"])
        assert {key: getattr(row, key) for key in preserved} == preserved
        session.commit()
    # Real page reads may recompute onboarding flags, but must preserve every
    # founder-authored setting, acceptance receipt, timestamp and send rail.
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda read: read(d["wid"]), readers(service)))
    with d["factory"]() as session:
        row = session.get(GtmOutreachSettings, d["wid"])
        assert {key: getattr(row, key) for key in preserved if key != "onboarding_status"} == {
            key: value for key, value in preserved.items() if key != "onboarding_status"}


def test_settings_initializer_does_not_swallow_other_integrity_errors(journey_db, monkeypatch):
    from app.db.models import GtmOutreachSettings
    from app.gtm.relationships import service
    d = journey_db
    monkeypatch.setattr(service, "utcnow", lambda: None)
    with d["factory"]() as session:
        with pytest.raises(IntegrityError) as raised:
            service._settings(session, d["wid"])
        assert raised.value.orig.sqlstate == "23502"  # NOT NULL, not the allowed PK conflict.
        session.rollback()
        assert session.get(GtmOutreachSettings, d["wid"]) is None
