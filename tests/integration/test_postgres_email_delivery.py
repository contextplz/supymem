"""Owned migrated outbox, real worker and signed email callback acceptance.

The provider is the real local log adapter; no email leaves the test process.
One case serves the normal mounted API over an owned loopback socket.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest
from core.assertion import sign_assertion
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import SECRET, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db

KEY = b"owned-email-webhook-test-key"
WEBHOOK = "/webhooks/email/resend"


@pytest.fixture
def email(journey_db, monkeypatch):
    from app.db.models import Person
    from app.email import providers, service
    from app.worker.catalog import default_worker_globs

    d = journey_db
    address = d["actor"] + "@example.test"
    for key, value in {
        "EMAIL_PROVIDER": "log", "EMAIL_FROM": "sender@example.com", "EMAIL_PHYSICAL_ADDRESS": "Owned local test footer",
        "SUPYMEM_INTERNAL_EMAIL": "operator@example.test",
        "COS_INTERNAL_ASSERTION_SECRET": SECRET, "COS_TICK_HMAC_SECRET": SECRET,
        "RESEND_WEBHOOK_SECRET": "whsec_" + base64.b64encode(KEY).decode(),
        "COMPANY_OS_VAULT_SECRET": "owned-email-test-vault-key-more-than-32-bytes",
        "ADMIN_REQUIRE_TOTP": "1", "ADMIN_BOOTSTRAP_EMAILS": address,
        "EMAIL_DIGEST_HOUR_UTC": "0", "WORKER_KINDS": "email.*",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    from app.main import create_app
    with d["factory"]() as s:
        person = s.get(Person, d["actor"])
        person.email, person.created_at = address, service.utcnow() - timedelta(days=30)
        s.commit()
    outgoing = []
    def log_send(message):
        outgoing.append(message)
        return providers.log_only(message)
    monkeypatch.setitem(providers.PROVIDERS, "log", log_send)
    assert default_worker_globs() == ["email.*"]
    d.update(address=address, outgoing=outgoing, app=create_app())
    return d


def signed(d, path, method="GET", actor=None):
    return {"X-COS-Assertion": sign_assertion(SECRET, sub=actor or d["actor"],
        email=d["address"] if actor is None else "", role="user", path=path, method=method)}


def body(d, pid, kind="email.delivered", **data):
    return {"type": kind, "data": {"email_id": pid, "to": [d["address"]], **data}}


def post_event(client, event, event_id=None):
    raw = json.dumps(event, sort_keys=True).encode()
    event_id = event_id or "msg_" + uuid.uuid4().hex
    ts = str(int(time.time()))
    signature = base64.b64encode(hmac.new(KEY, f"{event_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return client.post(WEBHOOK, content=raw, headers={"svix-id": event_id,
        "svix-timestamp": ts, "svix-signature": "v1," + signature})


def sent(d, ref="test"):
    from app.db.supymem_models import EmailMessage, NeedsYouRequest
    from app.email import service
    request_id = "nyr_email_" + d["actor"]
    with d["factory"]() as session:
        if session.get(NeedsYouRequest, request_id) is None:
            session.add(NeedsYouRequest(id=request_id, workspace_id=d["wid"], venture_id=d["vid"], reason_code="captcha", title="Review the pending request", blocking_ref="owned-email-test", status="open", created_at=service.utcnow()))
            session.commit()
    mid = service.send_email(d["actor"], "needs_you", {"reason": "approval", "path": f"/v/{d['vid']}/now", "_needs_request_id": request_id}, ref=ref)
    assert service.deliver(mid) == "sent"
    with d["factory"]() as s:
        return mid, s.get(EmailMessage, mid).provider_id


def receipts(d):
    from app.db.models import AnalyticsEvent
    with d["factory"]() as s:
        return s.scalars(select(AnalyticsEvent).where(AnalyticsEvent.person_id == d["actor"],
            AnalyticsEvent.id.like("ane_email_%"),
            AnalyticsEvent.event_name.in_(("email.delivered", "email.bounced", "email.complained", "email.opened", "email.clicked")))).all()


def run_jobs(d, kind):
    from app.core.jobs import lease
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    install_default_handlers()
    with d["factory"]() as s:
        jobs = lease(s, "owned-email-worker", kinds=[kind], n=100)
        s.commit()
    for job in jobs:
        asyncio.run(_run_one(d["factory"], job, {job.id}))
    return jobs


@contextmanager
def running_api(app):
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Owned local API did not start"
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "Owned local API did not stop"


def test_loopback_scheduler_worker_outbox_and_real_unsubscribe(email, monkeypatch):
    from app.core import jobs
    from app.db.models import ChatMessage, Job, Person
    from app.db.supymem_models import EmailMessage
    from app.email import service, triggers
    d = email
    # Admin reporting is global; valid callbacks from other owned fixtures may
    # already exist in the module database. Sending this flow must not add any.
    delivery_before = service.status()["delivery_reporting"]["last_7_days"]
    assert receipts(d) == []
    def outgoing_for(message_id, *, person_id=None):
        with d["factory"]() as s:
            row = s.get(EmailMessage, message_id)
            assert row.person_id == (person_id or d["actor"])
            key = row.idempotency_key
        return [message for message in d["outgoing"] if message.idempotency_key == key]

    with d["factory"]() as s:
        s.get(Person, d["actor"]).created_at = service.utcnow()
        s.commit()
    # Keep a legitimate unrelated admin alert in the same worker's outbox.
    # The owned welcome/digest must each be sent once without suppressing it.
    monkeypatch.setenv("FOUNDER_ADMIN_EMAILS", "operator@example.test")
    founder_id = d["actor"] + "_founder"
    with d["factory"]() as s:
        s.add(Person(id=founder_id, email="operator@example.test", display_name="Owned founder fixture", created_at=service.utcnow()))
        s.commit()
    from app.error_reports.service import record
    alert_place = "owned-email-fixture-" + uuid.uuid4().hex
    record(source="core", feature="owned-email-acceptance", code="http_5xx", place=alert_place)
    with d["factory"]() as s:
        alerts = [row for row in s.scalars(select(EmailMessage).where(
            EmailMessage.to_email == "operator@example.test", EmailMessage.template == "error_alert"))
            if json.loads(row.context_json).get("place") == alert_place]
        assert len(alerts) == 1
        alert_id = alerts[0].id
    with running_api(d["app"]) as client:
        tick = b'{"tick":"hourly"}'
        headers = {"Content-Type": "application/json", "X-Tick-Signature": hmac.new(SECRET.encode(), tick, hashlib.sha256).hexdigest()}
        queued = client.post("/internal/tick", content=tick, headers=headers)
        assert queued.status_code == 200 and queued.json()["enqueued"] == ["email.schedule"]
        assert client.post("/internal/tick", content=tick, headers=headers).json()["ids"] == queued.json()["ids"]
        assert len(run_jobs(d, "email.schedule")) == 1
        sends = run_jobs(d, "email.send")
        assert sends
        with d["factory"]() as s:
            rows = s.scalars(select(EmailMessage).where(EmailMessage.person_id == d["actor"],
                EmailMessage.template == "welcome")).all()
            assert len(rows) == 1 and rows[0].status == "sent"
            assert all(s.get(Job, job.id).status == "succeeded" for job in sends)
            assert s.get(EmailMessage, alert_id).status == "sent"
        assert len(outgoing_for(alert_id, person_id=founder_id)) == 1
        welcome_id = rows[0].id
        assert len(outgoing_for(welcome_id)) == 1
        # A worker acknowledgement lost after the provider receipt does not redial.
        assert service.deliver(welcome_id) == "sent" and len(outgoing_for(welcome_id)) == 1
        with d["factory"]() as s:
            s.add(ChatMessage(id="msg_owned_digest_" + uuid.uuid4().hex, venture_id=d["vid"],
                role="assistant", kind="plan_ready", body_md="Owned saved milestone",
                created_at=service.utcnow() - timedelta(hours=2)))
            s.commit()
        service.set_prefs(d["actor"], {"digests": True})
        circuit_before_digest = service.status()["circuit_breaker"]
        mid = service.send_email(d["actor"], "digest_daily", {"items": ["Owned saved milestone"]}, ref="day1")
        run_jobs(d, "email.send")
        if circuit_before_digest["paused"]:
            # CI's sorted order can leave a signed complaint from an earlier owned
            # fixture in this module database. Preserve it and verify real expiry.
            with d["factory"]() as s:
                row = s.get(EmailMessage, mid)
                assert row.status == "queued" and row.error == "email_circuit_open"
            assert outgoing_for(mid) == []
            after_expiry = service.utcnow() + timedelta(hours=25)
            for clock in (service, triggers, jobs):
                monkeypatch.setattr(clock, "utcnow", lambda: after_expiry)
            with d["factory"]() as s:
                s.add(ChatMessage(id="msg_owned_digest_expiry_" + uuid.uuid4().hex, venture_id=d["vid"],
                    role="assistant", kind="plan_ready", body_md="Owned saved milestone after circuit expiry",
                    created_at=after_expiry - timedelta(hours=2)))
                s.commit()
            assert not service.status()["circuit_breaker"]["paused"]
            run_jobs(d, "email.send")
        assert len(outgoing_for(welcome_id)) == 1 and len(outgoing_for(mid)) == 1
        outgoing = outgoing_for(mid)[0]
        assert outgoing.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        path = urlsplit(outgoing.headers["List-Unsubscribe"].strip("<>")).path
        query = urlsplit(outgoing.headers["List-Unsubscribe"].strip("<>")).query
        unsubscribe = path + "?" + query
        assert client.get(unsubscribe).status_code == 200 and service.get_prefs(d["actor"])["digests"]
        assert client.post(unsubscribe, content="List-Unsubscribe=One-Click").status_code == 200
        assert not service.get_prefs(d["actor"])["digests"]
        prefs = client.get("/email/preferences", headers=signed(d, "/email/preferences"))
        assert prefs.status_code == 200 and prefs.json()["preferences"]["digests"] is False
        assert client.put("/email/preferences", json={"digests": True}).status_code == 401
        next_id = service.send_email(d["actor"], "digest_daily", {"items": ["Another day"]}, ref="day2")
        with d["factory"]() as s:
            assert s.get(EmailMessage, next_id).error == "opted_out"
            assert s.scalar(select(Job.id).where(Job.dedupe_key == "email.send:" + next_id)) is None
        run_jobs(d, "email.send")  # unrelated transactional alerts remain legitimate
        assert service.deliver(next_id) == "suppressed" and outgoing_for(next_id) == []
        assert len(outgoing_for(welcome_id)) == 1 and len(outgoing_for(mid)) == 1
        # A log-adapter receipt is never advertised as delivered.
        status = client.get("/admin/email/status", headers=signed(d, "/admin/email/status"))
        assert status.status_code == 200
        assert status.json()["delivery_reporting"]["last_7_days"] == delivery_before
        assert receipts(d) == []
        assert client.get("/admin/email/status", headers=signed(d, "/admin/email/status", actor="usr_foreign")).status_code in {401, 403}
        assert mid != next_id


def test_signed_callback_duplicates_out_of_order_and_suppression(email):
    from app.db.supymem_models import EmailMessage
    from app.email import service
    d = email
    delivery_before = service.status()["delivery_reporting"]["last_7_days"]
    mid, pid = sent(d)
    callback = body(d, pid, "email.bounced", bounce={"type": "Permanent"})
    event_id = "msg_" + uuid.uuid4().hex
    def post(_):
        return post_event(TestClient(d["app"]), callback, event_id).status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(post, range(6))) == [200] * 6
    with TestClient(d["app"]) as client:
        assert post_event(client, body(d, pid)).status_code == 200  # delivered arrives later
        assert post_event(client, body(d, pid, "email.complained")).status_code == 200
    assert len(receipts(d)) == 3 and service.is_suppressed(d["address"])
    with d["factory"]() as s:
        assert s.get(EmailMessage, mid).status == "sent"  # transport and callback state are separate
    counts = service.status()["delivery_reporting"]["last_7_days"]
    assert counts == {kind: count + 1 for kind, count in delivery_before.items()}
    blocked = service.send_email(d["actor"], "needs_you", {}, ref="after-bounce")
    assert service.deliver(blocked) == "suppressed" and len(d["outgoing"]) == 1


@pytest.mark.parametrize("event", [[], {"type": []}, {"type": "email.delivered", "data": []},
    {"type": "email.bounced", "data": {"email_id": "unknown", "to": "bad"}},
    {"type": "email.delivered", "data": {"email_id": 42, "to": ["x@example.test"]}}])
def test_signed_malformed_callbacks_ack_without_mutation(email, event):
    d = email
    with TestClient(d["app"]) as client:
        result = post_event(client, event)
        assert result.status_code == 200 and result.json()["status"] == "ignored"
    assert receipts(d) == []


def test_unknown_foreign_recipient_and_reused_event_identity_do_not_write(email):
    from app.email import service
    d = email
    _, pid = sent(d)
    with TestClient(d["app"]) as client:
        assert client.post(WEBHOOK, content=b"{}").status_code == 401
        assert post_event(client, body(d, "not-in-outbox", "email.complained")).status_code == 503
        foreign = "foreign@example.test"
        assert post_event(client, body(d, pid, "email.complained", to=[foreign])).json()["status"] == "ignored"
        event_id = "msg_" + uuid.uuid4().hex
        assert post_event(client, body(d, pid), event_id).json()["status"] == "email.delivered"
        assert post_event(client, body(d, pid, "email.complained"), event_id).json()["status"] == "ignored"
        assert post_event(client, {"type": "email.new_future_event"}).json()["status"] == "ignored"
    assert not service.is_suppressed(d["address"]) and not service.is_suppressed(foreign)
    assert len(receipts(d)) == 1


def test_enqueue_failure_rolls_back_outbox_and_retry_has_one_job(email, monkeypatch):
    from app.core import jobs
    from app.db.models import Job
    from app.db.supymem_models import EmailMessage
    from app.email import service
    d = email
    original = jobs.enqueue
    def fault(session, *args, **kwargs):
        original(session, *args, **kwargs)
        session.flush()
        raise RuntimeError("owned enqueue commit fault")
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "enqueue", fault)
        with pytest.raises(RuntimeError, match="owned enqueue"):
            service.send_email(d["actor"], "welcome", {}, ref="atomic")
    with d["factory"]() as s:
        assert s.scalars(select(EmailMessage).where(EmailMessage.person_id == d["actor"])).all() == []
    mid = service.send_email(d["actor"], "welcome", {}, ref="atomic")
    assert service.send_email(d["actor"], "welcome", {}, ref="atomic") == mid
    with d["factory"]() as s:
        assert len(s.scalars(select(Job).where(Job.dedupe_key == "email.send:" + mid)).all()) == 1


def test_concurrent_duplicate_delivery_uses_one_log_dispatch(email):
    from app.email import service
    d = email
    mid = service.send_email(d["actor"], "welcome", {}, ref="concurrent")
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: service.deliver(mid), range(4))) == ["sent"] * 4
    assert len(d["outgoing"]) == 1


def test_callback_before_send_commit_retries_without_redial(email, monkeypatch):
    from sqlalchemy import delete
    from app.analytics.feature_catalog import route_feature
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import EmailMessage, ErrorReport
    from app.email import providers, service
    from app.error_reports.service import fingerprint
    d = email
    # Run with the first occurrence, even after another callback test. Priming
    # this fingerprint hid an alert enqueue that blocked on the sender's lock.
    fp = fingerprint("server", route_feature(WEBHOOK), "http_5xx", WEBHOOK, "")
    with d["factory"]() as s:
        s.execute(delete(ErrorReport).where(ErrorReport.fingerprint == fp))
        server_errors = set(s.scalars(select(AnalyticsEvent.id).where(AnalyticsEvent.event_name == "server.error")))
        alerts = set(s.scalars(select(EmailMessage.id).where(EmailMessage.template == "error_alert")))
        s.commit()
    client = TestClient(d["app"])
    original = providers.PROVIDERS["log"]
    callback, event_id = None, "msg_" + uuid.uuid4().hex
    def send(message):
        nonlocal callback
        pid = original(message)
        callback = body(d, pid)
        assert post_event(client, callback, event_id).status_code == 503
        assert receipts(d) == []
        return pid
    monkeypatch.setitem(providers.PROVIDERS, "log", send)
    mid = service.send_email(d["actor"], "welcome", {}, ref="early-callback")
    assert service.deliver(mid) == "sent"
    assert post_event(client, callback, event_id).status_code == 200
    assert service.deliver(mid) == "sent" and len(d["outgoing"]) == 1
    assert len(receipts(d)) == 1
    with d["factory"]() as s:
        assert s.scalar(select(ErrorReport.id).where(ErrorReport.fingerprint == fp)) is None
        assert set(s.scalars(select(AnalyticsEvent.id).where(AnalyticsEvent.event_name == "server.error"))) == server_errors
        assert set(s.scalars(select(EmailMessage.id).where(EmailMessage.template == "error_alert"))) == alerts


def test_callback_receipt_and_suppression_commit_atomically(email):
    from sqlalchemy import event
    from app.email import service
    d = email
    _, pid = sent(d)
    callback = body(d, pid, "email.complained")
    event_id = "msg_" + uuid.uuid4().hex
    def fault(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO email_suppressions"):
            raise RuntimeError("owned suppression commit fault")
    event.listen(d["engine"], "before_cursor_execute", fault)
    try:
        client = TestClient(d["app"], raise_server_exceptions=False)
        assert post_event(client, callback, event_id).status_code == 500
    finally:
        event.remove(d["engine"], "before_cursor_execute", fault)
    assert receipts(d) == [] and not service.is_suppressed(d["address"])
    assert post_event(TestClient(d["app"]), callback, event_id).status_code == 200
    assert len(receipts(d)) == 1 and service.is_suppressed(d["address"])


def test_legacy_analytics_cannot_claim_persisted_delivery(email):
    from app.analytics.events import emit
    from app.email import service
    d = email
    mid, pid = sent(d)
    before = service.status()["delivery_reporting"]["last_7_days"]
    emit("email.delivered", person_id=d["actor"], props={"schema": "email_delivery_v1",
        "provider": "resend", "message_id": mid, "provider_id": pid})
    assert service.status()["delivery_reporting"]["last_7_days"] == before
    assert receipts(d) == []
