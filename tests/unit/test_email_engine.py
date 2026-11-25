"""Email engine: adapters, idempotency, prefs/suppression, unsubscribe, webhooks, rate limits, templates."""
import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app

SECRET = "email-test-secret-over-thirty-two-characters"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/email.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("EMAIL_FROM", "sender@example.com")
    monkeypatch.setenv("SUPYMEM_INTERNAL_EMAIL", "operator@example.test")
    monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", "Supymem, 1 Test St")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    from app.db.models import Person
    from app.email.service import utcnow

    with state.db()() as s:
        s.add_all([Person(id="usr_a", display_name="A", email="a@example.test", created_at=utcnow() - timedelta(days=30)),
                   Person(id="usr_b", display_name="B", email=None, created_at=utcnow())])
        s.commit()
    from app.email.service import set_prefs
    set_prefs("usr_a", {"digests": True})  # Digest scenarios have explicit opt-in.
    return main, state


def _rows(state, **kw):
    from app.db.supymem_models import EmailMessage
    from sqlalchemy import select

    with state.db()() as s:
        return s.scalars(select(EmailMessage).filter_by(**kw)).all()


def _needs_context(state, context=None, person_id="usr_a"):
    """Provider tests dispatch a real open request, never a source-free invented blocker."""
    from app.db.models import Venture, Workspace
    from app.db.supymem_models import NeedsYouRequest
    from app.email.service import utcnow
    now, vid, rid = utcnow(), "ven_email_" + person_id, "nyr_email_" + person_id
    with state.db()() as s:
        if s.get(Venture, vid) is None:
            wid = "ws_email_" + person_id
            s.add(Workspace(id=wid, kind="venture", name="Owned request", entry_mode="idea", owner_id=person_id, created_at=now, updated_at=now))
            s.flush()
            s.add(Venture(id=vid, workspace_id=wid, name="Owned request", raw_idea="A real request", stage="idea", created_at=now, updated_at=now))
            s.flush()
            s.add(NeedsYouRequest(id=rid, venture_id=vid, workspace_id=wid, reason_code="captcha", title="Review the pending request", blocking_ref="owned-test", status="open", created_at=now))
            s.commit()
    return {**dict(context or {}), "path": f"/v/{vid}/now", "_needs_request_id": rid}


def test_provider_selection(env, monkeypatch):
    from app.email import providers

    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    assert providers.current_provider() is providers.resend
    monkeypatch.setenv("EMAIL_PROVIDER", "SES")
    assert providers.current_provider() is providers.ses
    monkeypatch.delenv("EMAIL_PROVIDER")
    assert providers.current_provider() is providers.log_only
    monkeypatch.setenv("EMAIL_PROVIDER", "nope")
    with pytest.raises(providers.ProviderError):
        providers.current_provider()


def test_idempotent_send_and_delivery(env):
    from app.db.models import Venture, Workspace
    from app.email.service import deliver, send_email, utcnow

    _, state = env
    now = utcnow()
    with state.db()() as s:
        s.add(Workspace(id="ws_x", kind="venture", name="Acme", entry_mode="idea", owner_id="usr_a", created_at=now, updated_at=now))
        s.add(Venture(id="ven_x", workspace_id="ws_x", name="Acme", raw_idea="x", stage="idea", created_at=now, updated_at=now))
        s.commit()
    first = send_email("usr_a", "milestone", {"what": "plan", "venture_name": "Acme", "path": "/v/ven_x/plan"}, ref="k1")
    assert first and send_email("usr_a", "milestone", {"what": "plan"}, ref="k1") == first
    assert len(_rows(state, person_id="usr_a")) == 1
    assert deliver(first) == "sent"
    row = _rows(state, id=first)[0]
    assert row.status == "sent" and row.provider_id.startswith("log_") and row.sent_at
    assert deliver(first) == "sent"  # a retried job never re-sends


def test_no_email_and_opt_out_and_suppression(env):
    from app.email import service

    _, state = env
    service.send_email("usr_b", "welcome", {})
    assert _rows(state, person_id="usr_b")[0].error == "no_email"
    service.set_prefs("usr_a", {"digests": False})
    service.send_email("usr_a", "digest_daily", {"items": ["x"]}, ref="d1")
    assert _rows(state, person_id="usr_a")[0].error == "opted_out"
    service.send_email("usr_a", "needs_you", {"reason": "captcha"}, ref="t1")  # transactional ignores prefs
    assert _rows(state, template="needs_you")[0].status == "queued"
    service.suppress("A@example.test", "hard_bounce")
    mid = service.send_email("usr_a", "needs_you", {"reason": "blocked"}, ref="t2")
    assert _rows(state, id=mid)[0].error == "address_suppressed"


def test_deliver_rechecks_prefs_and_marks_failure(env, monkeypatch):
    from app.core.jobs import FatalError, RetryableError
    from app.email import providers, service

    _, state = env
    mid = service.send_email("usr_a", "weekly_insights", {"items": ["x"]}, ref="w1")
    service.set_prefs("usr_a", {"digests": False})
    assert service.deliver(mid) == "suppressed"
    mid = service.send_email("usr_a", "needs_you", _needs_context(state, {"reason": "blocked"}), ref="w2")

    def boom(_msg):
        raise providers.ProviderError("resend_http_503", True)
    monkeypatch.setitem(providers.PROVIDERS, "log", boom)
    with pytest.raises(RetryableError):
        service.deliver(mid, attempt=1)
    assert _rows(state, id=mid)[0].status == "queued"
    with pytest.raises(FatalError):
        service.deliver(mid, attempt=service.MAX_ATTEMPTS)
    assert _rows(state, id=mid)[0].status == "failed"


def test_real_provider_needs_physical_address_for_marketing(env, monkeypatch):
    from app.core.jobs import FatalError
    from app.email import service

    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.delenv("EMAIL_PHYSICAL_ADDRESS")
    mid = service.send_email("usr_a", "nudge_d2", {}, ref="n")
    with pytest.raises(FatalError):
        service.deliver(mid)


@pytest.mark.parametrize("payload", [{}, {"id": ""}, {"id": 42}, [], {"id": "bad id"}])
def test_accepted_response_without_provider_identity_is_unconfirmed_and_not_redialled(env, monkeypatch, payload):
    import httpx
    from app.core.jobs import FatalError
    from app.email import providers, service

    _, state = env
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "owned-test-only")
    calls = []
    def response(*args, **kwargs):
        calls.append(kwargs["headers"]["Idempotency-Key"])
        return httpx.Response(200, json=payload)
    monkeypatch.setattr(providers.httpx, "post", response)
    mid = service.send_email("usr_a", "welcome", {}, ref="unconfirmed")
    with pytest.raises(FatalError, match="resend_response_unconfirmed"):
        service.deliver(mid)
    row = _rows(state, id=mid)[0]
    assert row.status == "failed" and row.error == "resend_response_unconfirmed"
    assert row.provider_id is None and row.sent_at is None
    assert service.deliver(mid, attempt=2) == "failed" and len(calls) == 1
    assert service.requeue_failed() == 0


def test_unsubscribe_token_and_flow(env):
    from app.email import service

    main, _ = env
    token = service.make_token("usr_a", "digests")
    assert service.read_token(token) == ("usr_a", "digests")
    assert service.read_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert service.read_token("garbage") is None
    client = TestClient(main.create_app())
    page = client.get(f"/public/email/unsubscribe?t={token}")
    assert page.status_code == 200 and service.get_prefs("usr_a")["digests"] is True  # GET changes nothing
    assert client.post(f"/public/email/unsubscribe?t={token}", content="List-Unsubscribe=One-Click").status_code == 200
    assert service.get_prefs("usr_a")["digests"] is False and service.get_prefs("usr_a")["suggestions"] is True
    all_token = service.make_token("usr_a", "all")
    client.post(f"/public/email/unsubscribe?t={all_token}")
    assert not any(service.get_prefs("usr_a").values())
    assert "not valid" in client.post("/public/email/unsubscribe?t=bad").text


def test_headers_and_footer_on_marketing_only(env, monkeypatch):
    from app.db.models import Opportunity, OpportunitySlate, Workspace
    from app.email import providers, service

    _, state = env
    now = service.utcnow() - timedelta(hours=2)
    with state.db()() as s:
        s.add(Workspace(id="ws_headers", kind="desk", name="Desk", entry_mode="surprise", owner_id="usr_a", created_at=now, updated_at=now))
        s.flush()
        s.add(OpportunitySlate(id="slate_headers", workspace_id="ws_headers", created_at=now))
        s.flush()
        s.add(Opportunity(id="opp_headers", slate_id="slate_headers", workspace_id="ws_headers", title="Idea",
                          one_liner="An idea to review", first_customer="Founders", wedge="A focused workflow",
                          why_now="Saved research", kill_criteria="No customer interest", status="proposed", created_at=now))
        s.commit()
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda m: seen.append(m) or "id1")
    service.deliver(service.send_email("usr_a", "needs_you", _needs_context(state, {"reason": "captcha"}), ref="h1"))
    service.deliver(service.send_email("usr_a", "suggestions_waiting", {"items": ["Idea"], "count": 1}, ref="h2"))
    trans, mkt = seen
    assert "List-Unsubscribe" not in trans.headers
    assert mkt.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/public/email/unsubscribe?t=" in mkt.headers["List-Unsubscribe"] and "Supymem, 1 Test St" in mkt.text


def test_rate_limit_and_quiet_when_active(env):
    from app.analytics.events import emit
    from app.email.service import send_email

    _, state = env
    assert send_email("usr_a", "digest_daily", {"items": ["x"]}, ref="1")
    assert send_email("usr_a", "suggestions_waiting", {"items": ["y"]}, ref="2") is None  # 1 marketing mail / day
    assert send_email("usr_a", "needs_you", {}, ref="3")  # transactional unaffected
    assert len(_rows(state, person_id="usr_a")) == 2
    from app.db.models import Person
    from app.email.service import utcnow

    with state.db()() as s:
        s.add(Person(id="usr_c", display_name="C", email="c@example.test", created_at=utcnow()))
        s.commit()
    emit("session.active", person_id="usr_c")
    assert send_email("usr_c", "nudge_d2", {}, ref="a") is None  # in the app within the hour


def test_schedule_digest_and_weekly(env, monkeypatch):
    from app.db.models import ChatMessage, Venture, Workspace
    from app.email.service import utcnow
    from app.email.triggers import run_schedule

    _, state = env
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "digest_daily,weekly_insights")
    now = utcnow()
    tuesday = now.replace(hour=20) + timedelta(days=(1 - now.weekday()) % 7)
    with state.db()() as s:
        s.add(Workspace(id="ws_1", kind="venture", name="Acme", entry_mode="idea", owner_id="usr_a", created_at=now, updated_at=now))
        s.add(Venture(id="ven_1", workspace_id="ws_1", name="Acme", raw_idea="x", stage="idea", created_at=now, updated_at=now))
        s.add(ChatMessage(id="msg_1", venture_id="ven_1", role="assistant", kind="site_live", body_md="Your site is **live**.",
                          created_at=tuesday - timedelta(hours=2)))
        s.commit()
    run_schedule(tuesday.replace(hour=5))  # before the digest hour: nothing, and 30 days idle is past the nudge window
    assert _rows(state, person_id="usr_a") == []
    run_schedule(tuesday)
    run_schedule(tuesday)  # hourly reruns are idempotent
    assert [r.template for r in _rows(state, person_id="usr_a")] == ["digest_daily"]
    assert "live" in _rows(state, person_id="usr_a")[0].context_json
    monday = tuesday + timedelta(days=6)
    monkeypatch.setattr("app.email.service.utcnow", lambda: monday)
    run_schedule(monday)
    run_schedule(monday)
    assert [r.template for r in _rows(state, person_id="usr_a")] == ["digest_daily", "weekly_insights"]


def test_nudge_ladder(env):
    from app.email.triggers import run_schedule
    from app.email.service import utcnow

    _, state = env
    from app.db.models import Person

    with state.db()() as s:
        s.get(Person, "usr_a").created_at = utcnow() - timedelta(days=5, hours=1)
        s.commit()
    run_schedule()
    assert [r.template for r in _rows(state, person_id="usr_a")] == ["nudge_d5"]
    run_schedule(utcnow() + timedelta(days=30))  # long gone: no stale nudge
    assert len(_rows(state, person_id="usr_a")) == 1


def test_milestone_hook_enqueues_email(env, monkeypatch):
    from app.chat.milestones import post_milestone
    from app.db.models import Venture, Workspace
    from app.email.service import utcnow
    from app.email.triggers import run_schedule

    _, state = env
    now = utcnow()
    with state.db()() as s:
        s.add(Workspace(id="ws_1", kind="venture", name="Acme", entry_mode="idea", owner_id="usr_a", created_at=now, updated_at=now))
        s.add(Venture(id="ven_1", workspace_id="ws_1", name="Acme", raw_idea="x", stage="idea", created_at=now, updated_at=now))
        s.commit()
    assert post_milestone("ven_1", key="plan_done:p1", body_md="Your plan is ready: 3 milestones.", kind="plan_ready")
    assert _rows(state, person_id="usr_a") == []  # durable progress is batched while the founder is away
    run_schedule(now + timedelta(hours=3))
    rows = _rows(state, person_id="usr_a")
    assert len(rows) == 1 and rows[0].template == "milestone" and "plan is ready" in rows[0].subject.lower()
    assert json.loads(rows[0].context_json)["path"] == "/v/ven_1/plan"
    post_milestone("ven_1", key="plan_done:p1", body_md="Your plan is ready", kind="plan_ready")  # duplicate post
    run_schedule(now + timedelta(hours=4))
    assert len(_rows(state, person_id="usr_a")) == 1


def test_templates_render_all(env, monkeypatch):
    from app.email import templates
    from app.email.fixtures import FIXTURES

    hostile = "<script>alert(1)</script>"
    for name in templates.TEMPLATES:
        marketing = templates.category_of(name) != templates.TRANSACTIONAL
        unsub = dict(unsubscribe_url="https://u/x", prefs_url="https://p/y") if marketing else {}
        subject, html, text = templates.render(name, FIXTURES[name], **unsub)
        assert subject and text.strip() and "Charlie" in text
        assert ("https://supymem.com/downloads/Supymem.dmg" if name == "app_release" else
                "https://gateway.supymem.com/public/beta/download?code=" if name in {"beta_invite", "get_mac_app"}
                else "https://app.supymem.com/") in text
        assert len(html.encode()) < 100_000
        assert "Maya" in text or name in ("beta_invite", "suggestions_waiting", "god_mode_admin", "error_alert",
                                          "email_health_alert", "signup_summary", "credit_request_admin")
        assert ("Unsubscribe" in html and "Unsubscribe: https://u/x" in text) == marketing
        # Hostile copy and a path that tries to leave the site, with valid admission identity.
        evil = {k: ([hostile] * 3 if k == "items" else hostile if isinstance(v, str) and k not in {"path", "admission_code", "version"} else v) for k, v in FIXTURES[name].items()}
        evil["path"] = "//evil.example"
        _, html, text = templates.render(name, evil, **unsub)
        assert "<script>" not in html and "evil.example" not in html
        if any(isinstance(v, str) and v for k, v in evil.items() if k not in ("path", "reason", "what")):
            assert "&lt;script&gt;" in html or name.startswith("nudge")


def test_deliver_greets_by_first_name(env, monkeypatch):
    from app.db.models import Workspace
    from app.email import service

    _, state = env
    with state.db()() as s:
        assert service.first_name(s, "usr_a") == "A"  # display name
        s.add(Workspace(id="ws_d", kind="desk", name="Desk", entry_mode="idea", owner_id="usr_a",
                        settings_json=json.dumps({"first_name": "Maya"}), created_at=service.utcnow(), updated_at=service.utcnow()))
        s.commit()
        assert service.first_name(s, "usr_a") == "Maya"  # editable name wins


def test_resend_signature(env, monkeypatch):
    from app.email import webhooks

    key = b"k" * 24
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(key).decode())
    raw, ts = b'{"type":"email.complained"}', str(int(time.time()))
    sig = base64.b64encode(hmac.new(key, f"m1.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    good = {"svix-id": "m1", "svix-timestamp": ts, "svix-signature": "v1,AAAA v1," + sig}
    webhooks.verify_resend(raw, good)
    for bad in ({**good, "svix-signature": "v1,AAAA"}, {**good, "svix-timestamp": str(int(ts) - 4000)}, {}):
        with pytest.raises(webhooks.BadSignature):
            webhooks.verify_resend(raw, bad)
    with pytest.raises(webhooks.BadSignature):
        webhooks.verify_resend(raw + b" ", good)


def test_webhooks_suppress_on_hard_bounce_and_complaint(env, monkeypatch):
    from app.email import service

    main, state = env
    key = b"k" * 24
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(key).decode())
    client = TestClient(main.create_app())

    def post(event):
        raw, ts = json.dumps(event).encode(), str(int(time.time()))
        event_id = "m_" + hashlib.sha256(raw).hexdigest()[:16]
        sig = base64.b64encode(hmac.new(key, f"{event_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
        return client.post("/webhooks/email/resend", content=raw, headers={"svix-id": event_id, "svix-timestamp": ts, "svix-signature": "v1," + sig})

    def sent(address):
        from app.db.models import Person
        person = "usr_receipt_" + address.split("@", 1)[0]
        with state.db()() as session:
            session.add(Person(id=person, email=address, display_name="Owned fixture", created_at=service.utcnow()))
            session.commit()
        mid = service.send_email(person, "welcome", ref=address)
        service.deliver(mid)
        return _rows(state, id=mid)[0].provider_id

    assert client.post("/webhooks/email/resend", content=b"{}").status_code == 401
    assert post({"type": "email.bounced", "data": {"email_id": sent("soft@example.test"), "to": ["soft@example.test"], "bounce": {"type": "Transient"}}}).status_code == 200
    assert not service.is_suppressed("soft@example.test")
    post({"type": "email.bounced", "data": {"email_id": sent("hard@example.test"), "to": ["Hard@example.test"], "bounce": {"type": "Permanent"}}})
    post({"type": "email.complained", "data": {"email_id": sent("angry@example.test"), "to": ["angry@example.test"]}})
    assert service.is_suppressed("hard@example.test") and service.is_suppressed("angry@example.test")


def test_sns_verification_and_ses_events(env, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.x509.oid import NameOID
    import datetime as dt

    from app.email import service, webhooks

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(priv.public_key())
            .serial_number(1).not_valid_before(dt.datetime(2020, 1, 1)).not_valid_after(dt.datetime(2040, 1, 1)).sign(priv, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    monkeypatch.setenv("EMAIL_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:ses")
    msg = {"Type": "Notification", "MessageId": "1", "TopicArn": "arn:aws:sns:us-east-1:1:ses", "Timestamp": "t",
           "Message": json.dumps({"eventType": "Bounce", "mail": {"messageId": "m"},
                                  "bounce": {"bounceType": "Permanent", "bouncedRecipients": [{"emailAddress": "Gone@example.test"}]}}),
           "SignatureVersion": "2", "SigningCertURL": "https://sns.us-east-1.amazonaws.com/cert.pem"}
    to_sign = "".join(f"{f}\n{msg[f]}\n" for f in ("Message", "MessageId", "Timestamp", "TopicArn", "Type"))
    msg["Signature"] = base64.b64encode(priv.sign(to_sign.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    webhooks.verify_sns(msg, fetch_cert=lambda _u: pem)
    assert webhooks.handle_ses(msg) == "Bounce" and service.is_suppressed("gone@example.test")
    for tampered in ({**msg, "Message": "{}"}, {**msg, "TopicArn": "arn:other"},
                     {**msg, "SigningCertURL": "https://evil.example.com/cert.pem"},
                     {**msg, "SigningCertURL": "http://sns.us-east-1.amazonaws.com/cert.pem"}):
        with pytest.raises(webhooks.BadSignature):
            webhooks.verify_sns(tampered, fetch_cert=lambda _u: pem)


def test_preferences_api_and_job_kind(env):
    from app.worker.catalog import enabled_kinds, tick_kinds
    from app.worker.handlers import HANDLERS, install_default_handlers

    install_default_handlers()
    assert {"email.send", "email.schedule"} <= enabled_kinds() and "email.send" in HANDLERS
    assert "email.schedule" in tick_kinds("hourly")


def test_founder_gets_at_most_two_supymem_emails_a_day_across_categories(env):
    from app.email.service import send_email

    _, state = env
    assert send_email("usr_a", "digest_daily", {"items": ["x"]}, ref="cap-1")
    assert send_email("usr_a", "needs_you", _needs_context(state), ref="cap-2")
    assert send_email("usr_a", "needs_you", _needs_context(state), ref="cap-3") is None  # third mail in 24h, any category
    assert send_email("usr_a", "welcome", {}, ref="cap-4") is None
    assert len([r for r in _rows(state, person_id="usr_a") if r.template != "beta_invite"]) == 2
