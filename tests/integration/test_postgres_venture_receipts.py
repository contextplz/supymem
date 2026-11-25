"""Migrated, signed real endpoints and the canonical offline email worker."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_email_delivery import email as email, run_jobs, signed
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


def setup(email, monkeypatch):
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "venture_export_prepared,venture_archived")
    return email


def test_signed_export_race_and_real_worker_send_once(email, monkeypatch):
    from app.db.models import EventLog
    from app.db.supymem_models import EmailMessage
    from app.email import triggers

    d = setup(email, monkeypatch)
    path = f"/ventures/{d['vid']}/export"

    def prepare(_):
        with TestClient(d["app"]) as client:
            result = client.post(path, headers=signed(d, path, "POST"))
            assert result.status_code == 200, result.text
            return result.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        packs = list(pool.map(prepare, range(4)))
    assert all(pack == packs[0] for pack in packs)
    triggers.run_schedule()
    with d["factory"]() as s:
        messages = s.scalars(select(EmailMessage).where(EmailMessage.person_id == d["actor"])).all()
        assert len(messages) == 1 and messages[0].status == "queued"
        events = s.scalars(select(EventLog).where(EventLog.topic == "venture.export_prepared",
                                                EventLog.venture_id == d["vid"])).all()
        assert len(events) == 4 and all(event.actor_id == d["actor"] for event in events)
    assert len(run_jobs(d, "email.send")) == 1
    assert len(d["outgoing"]) == 1 and "prepared" in d["outgoing"][0].text
    triggers.run_schedule()
    assert not run_jobs(d, "email.send") and len(d["outgoing"]) == 1


def test_signed_member_archive_receipt_is_private_and_revocation_rechecked(email, monkeypatch):
    from app.db.models import Person, Venture, WorkspaceMember
    from app.db.supymem_models import EmailMessage
    from app.email import service, triggers

    d = setup(email, monkeypatch)
    member_id = "usr_member_" + d["actor"]
    with d["factory"]() as s:
        s.add(Person(id=member_id, display_name="Member", email=member_id + "@example.test", created_at=service.utcnow()))
        s.flush()
        s.add(WorkspaceMember(id="wm_" + member_id, workspace_id=d["wid"], person_id=member_id, role="owner", created_at=service.utcnow()))
        s.commit()
    path = f"/ventures/{d['vid']}/delete"
    with TestClient(d["app"]) as client:
        result = client.post(path, headers=signed(d, path, "POST", actor=member_id), json={"confirmation": "DELETE"})
        assert result.status_code == 200 and result.json()["retention_days"] == 7
    with d["factory"]() as s:
        assert s.get(Venture, d["vid"]).archived_at is not None
        messages = s.scalars(select(EmailMessage).where(EmailMessage.template == "venture_archived")).all()
        assert len(messages) == 1 and messages[0].person_id == member_id
        s.delete(s.get(WorkspaceMember, "wm_" + member_id))
        s.commit()
    triggers.run_schedule()
    assert len(run_jobs(d, "email.send")) == 1
    with d["factory"]() as s:
        row = s.get(EmailMessage, messages[0].id)
        assert row.status == "suppressed" and row.error == "source_no_longer_eligible"
    assert not d["outgoing"]
