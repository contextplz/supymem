"""Actual export/archive outcomes, canonical outbox and offline delivery custody."""
import json
from datetime import timedelta

import pytest
from core.assertion import sign_assertion
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.test_email_engine import SECRET, env as env


@pytest.fixture
def receipts(env, monkeypatch):
    from app.db.models import Venture, Workspace, WorkspaceMember
    from app.email import providers, service

    main, state = env
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "owned-venture-receipt-vault-over-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "a@example.test")
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "venture_export_prepared,venture_archived")
    now = service.utcnow()
    with state.db()() as s:
        s.add(Workspace(id="ws_receipt", kind="venture", name="Receipt venture", entry_mode="idea", owner_id="usr_a",
                        created_at=now, updated_at=now))
        s.flush()
        s.add(WorkspaceMember(id="wm_receipt_owner", workspace_id="ws_receipt", person_id="usr_a", role="owner", created_at=now))
        s.add(Venture(id="ven_receipt", workspace_id="ws_receipt", name="Receipt venture", raw_idea="Owned fixture",
                      stage="idea", created_at=now, updated_at=now))
        s.commit()
    outgoing = []

    def send(message):
        outgoing.append(message)
        return providers.log_only(message)

    monkeypatch.setitem(providers.PROVIDERS, "log", send)
    with TestClient(main.create_app()) as client:
        yield {"state": state, "client": client, "outgoing": outgoing, "vid": "ven_receipt", "wid": "ws_receipt", "actor": "usr_a"}


def signed(d, suffix, method="GET", actor=None):
    path = f"/ventures/{d['vid']}/{suffix}"
    return path, {"X-COS-Assertion": sign_assertion(SECRET, sub=actor or d["actor"], email="", role="user", path=path, method=method)}


def rows(d, model, **filters):
    with d["state"].db()() as s:
        return s.scalars(select(model).filter_by(**filters)).all()


def export(d):
    path, headers = signed(d, "export", "POST")
    return d["client"].post(path, headers=headers)


def archive(d, phrase="DELETE"):
    path, headers = signed(d, "delete", "POST")
    return d["client"].post(path, headers=headers, json={"confirmation": phrase})


def test_real_export_dedupes_preparation_and_not_support_or_foreign(receipts):
    from app.db.models import EventLog
    from app.db.supymem_models import EmailMessage
    from app.email import service

    d = receipts
    path, headers = signed(d, "export", "POST")
    assert d["client"].post(path, headers={**headers, "X-Subject": "usr_b"}).status_code == 401
    path, headers = signed(d, "export", "POST", actor="usr_b")
    assert d["client"].post(path, headers=headers).status_code == 404
    path, headers = signed(d, "export")
    assert d["client"].get(path, headers=headers).status_code == 200
    assert not rows(d, EventLog, topic="venture.export_prepared") and not rows(d, EmailMessage)
    path, headers = signed(d, "support-bundle")
    assert d["client"].get(path, headers=headers).status_code == 200
    assert not rows(d, EventLog, topic="venture.export_prepared")
    assert export(d).status_code == export(d).status_code == 200
    events = rows(d, EventLog, topic="venture.export_prepared")
    assert len(events) == 2 and all(event.actor_id == "usr_a" for event in events)
    assert set(json.loads(events[0].payload_json)) == {"schema", "export_sha256"}
    assert "Owned fixture" not in events[0].payload_json
    messages = rows(d, EmailMessage)
    assert len(messages) == 1 and messages[0].person_id == "usr_a"
    assert service.deliver(messages[0].id) == service.deliver(messages[0].id) == "sent"
    assert len(d["outgoing"]) == 1
    text = d["outgoing"][0].text
    assert "export" in text and "prepared" in text and "not completion of a download" in text
    assert f"/v/{d['vid']}?chat=1" in text
    assert f"/v/{d['vid']}/settings" not in text



def test_preparation_requires_signed_actor_even_when_legacy_reads_are_enabled(receipts, monkeypatch):
    from app.db.models import EventLog
    from app.db.supymem_models import EmailMessage
    d = receipts
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    path = f"/ventures/{d['vid']}/export"
    assert d["client"].get(path, headers={"X-Subject": d["actor"]}).status_code == 200
    assert d["client"].post(path, headers={"X-Subject": d["actor"]}).status_code == 401
    assert not rows(d, EventLog, topic="venture.export_prepared") and not rows(d, EmailMessage)

def test_actual_archive_receipt_survives_archive_but_not_restore(receipts):
    from app.db.models import EventLog
    from app.db.supymem_models import EmailMessage
    from app.email import service

    d = receipts
    assert archive(d, "not DELETE").status_code == 422
    assert not rows(d, EventLog, topic="venture.soft_deleted") and not rows(d, EmailMessage)
    result = archive(d)
    assert result.status_code == 200 and result.json()["recoverable"] is True
    assert archive(d).status_code == 409
    event = rows(d, EventLog, topic="venture.soft_deleted")[0]
    assert event.venture_id == d["vid"] and event.actor_id == d["actor"]
    message = rows(d, EmailMessage)[0]
    assert service.deliver(message.id) == "sent"
    text = d["outgoing"][0].text
    assert "archived" in text and "7 days" in text and "Recently deleted" in text
    assert "Data erasure is not confirmed" in text
    assert "purged" not in text and "export" not in text

    # A second real archive cycle receives a distinct key; restoring retires it.
    path, headers = signed(d, "restore", "POST")
    assert d["client"].post(path, headers=headers).status_code == 200
    assert archive(d).status_code == 200
    pending = rows(d, EmailMessage, status="queued")[0]
    path, headers = signed(d, "restore", "POST")
    assert d["client"].post(path, headers=headers).status_code == 200
    assert service.deliver(pending.id) == "suppressed" and len(d["outgoing"]) == 1


@pytest.mark.parametrize("change", ["owner", "blocked", "expired", "changed_archive", "source", "recipient"])
def test_delivery_rechecks_private_source_and_recipient(receipts, change, monkeypatch):
    from app.db.models import Person, Venture, Workspace
    from app.db.supymem_models import EmailMessage
    from app.email import service

    d = receipts
    assert archive(d).status_code == 200
    message = rows(d, EmailMessage)[0]
    if change == "expired":
        expired_now = service.utcnow() + timedelta(days=8)
        monkeypatch.setattr(service, "utcnow", lambda: expired_now)
    with d["state"].db()() as s:
        if change == "owner":
            s.get(Workspace, d["wid"]).owner_id = "usr_b"
            from app.db.models import WorkspaceMember

            s.delete(s.get(WorkspaceMember, "wm_receipt_owner"))
        elif change == "blocked":
            s.get(Workspace, d["wid"]).settings_json = '{"blocked":true}'
        elif change == "changed_archive":
            s.get(Venture, d["vid"]).archived_at = service.utcnow() - timedelta(hours=2)
        elif change == "source":
            s.get(EmailMessage, message.id).context_json = '{"_venture_receipt_id":"missing","_venture_id":"ven_receipt"}'
        elif change == "recipient":
            s.get(Person, "usr_a").email = "new@example.test"
        s.commit()
    assert service.deliver(message.id) == "suppressed" and not d["outgoing"]


def test_disabled_then_existing_schedule_recovers_and_respects_suppression(receipts, monkeypatch):
    from app.db.supymem_models import EmailMessage
    from app.email import service, triggers

    d = receipts
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "0")
    assert export(d).status_code == 200
    assert not rows(d, EmailMessage)
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "1")
    service.set_prefs("usr_a", {"product_updates": False, "engagement_reminders": False, "digests": False, "suggestions": False})
    # Requested-operation receipts use canonical transactional policy, not optional updates.
    triggers.run_schedule()
    assert len(rows(d, EmailMessage)) == 1
    service.suppress("a@example.test", "hard_bounce")
    triggers.run_schedule()
    assert service.deliver(rows(d, EmailMessage)[0].id) == "suppressed"
    assert not d["outgoing"]


def test_cap_recovery_quiet_hour_and_retry_freeze(receipts, monkeypatch):
    from app.core.jobs import RetryableError
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import EmailMessage
    from app.email import providers, service, triggers

    d = receipts
    assert export(d).status_code == 200
    original = rows(d, EmailMessage)[0]
    for n in range(4):
        assert service.send_email("usr_a", "venture_export_prepared", {}, ref=f"cap:{n}")
    assert archive(d).status_code == 200
    assert not rows(d, EmailMessage, template="venture_archived")
    with d["state"].db()() as s:
        for row in s.scalars(select(EmailMessage)):
            row.created_at = service.utcnow() - timedelta(days=2)
        s.add(AnalyticsEvent(id="ane_fixture_activity", person_id="usr_a", event_name="session.active",
                             occurred_at=service.utcnow(), props_json="{}"))
        s.commit()
    triggers.run_schedule()
    pending = rows(d, EmailMessage, template="venture_archived")[0]
    assert service.deliver(pending.id) == "deferred" and not d["outgoing"]
    # Export's source is retired by the later archive, even for a pending export.
    assert service.deliver(original.id) == "suppressed"
    after_quiet = service.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(service, "utcnow", lambda: after_quiet)
    calls = []

    def retry_then_log(message):
        calls.append(message)
        if len(calls) == 1:
            raise providers.ProviderError("resend_http_503", True)
        return providers.log_only(message)

    monkeypatch.setitem(providers.PROVIDERS, "log", retry_then_log)
    with pytest.raises(RetryableError):
        service.deliver(pending.id)
    assert service.deliver(pending.id, 2) == "sent"
    assert calls[0] == calls[1]


def test_audit_failure_does_not_invent_receipt_or_undo_archive(receipts, monkeypatch):
    from app.db.models import Venture
    from app.db.supymem_models import EmailMessage
    from app.ventures import lifecycle

    d = receipts
    monkeypatch.setattr(lifecycle, "audit", lambda *a, **kw: None)
    assert archive(d).status_code == 200
    assert rows(d, Venture, id=d["vid"])[0].archived_at is not None
    assert not rows(d, EmailMessage)


def test_failed_pack_preparation_never_becomes_receipt(receipts, monkeypatch):
    from app.db.models import EventLog
    from app.db.supymem_models import EmailMessage
    from app.ventures import founder_control

    def failed_pack(_):
        raise RuntimeError("owned fixture export failure")

    monkeypatch.setattr(founder_control, "export_pack", failed_pack)
    with pytest.raises(RuntimeError, match="owned fixture export failure"):
        export(receipts)
    assert not rows(receipts, EventLog, topic="venture.export_prepared")
    assert not rows(receipts, EmailMessage)


@pytest.mark.parametrize("template", ["venture_export_prepared", "venture_archived"])
def test_receipt_copy_escapes_names_and_refuses_foreign_cta(receipts, template):
    from app.email import templates

    _, html, plain = templates.render(template, {"venture_name": "<script>fixture()</script>\r\n",
        "restore_until": "13 Oct 2026 at 18:00 UTC", "path": "//foreign.example/export"})
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "foreign.example" not in html and "foreign.example" not in plain
    assert html.count('class="primary-cta"') == 1
