"""Activity-aware lifecycle: genuine consent, durable waits, gate rollout and stale-context safety."""
import json
from datetime import timedelta

import pytest

from tests.unit.test_email_engine import _rows
from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture(scope="module")
def email_modules():
    with service_modules("company_os"):
        yield


@pytest.fixture
def env(email_modules, tmp_path, monkeypatch):
    """Domain boundary on isolated real tables; mounted API/worker proof lives in Postgres tests."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    for key, value in {"COMPANY_OS_DATA_DIR": str(tmp_path), "EMAIL_PROVIDER": "log",
                       "EMAIL_FROM": "sender@example.com",
                       "SUPYMEM_INTERNAL_EMAIL": "operator@example.test",
                       "EMAIL_PHYSICAL_ADDRESS": "Owned synthetic preview address", "LLM_OFFLINE": "1",
                       "COS_INTERNAL_ASSERTION_SECRET": "email-owned-test-secret-more-than-32"}.items():
        monkeypatch.setenv(key, value)
    from app import state
    from app.db import models, supymem_models
    from app.email import service
    from app.ventures import next_step
    engine = create_engine(f"sqlite:///{tmp_path}/email.db")
    tables = [models.Person, models.Workspace, models.Venture, models.VentureDocument, models.ChatConversation, models.ChatMessage, models.Opportunity, models.GtmContact, models.GtmTouch,
              models.AnalyticsEvent, models.Job, models.CreditAccount, supymem_models.EmailMessage,
              supymem_models.EmailPreference, supymem_models.EmailSuppression, supymem_models.IdeaSession,
              supymem_models.ClarifierTurn, supymem_models.DesktopDevice, supymem_models.NeedsYouRequest,
              supymem_models.VentureWaitlist, supymem_models.VentureWaitlistEntry, supymem_models.BetaCode, supymem_models.BetaCodeRedemption]
    models.Base.metadata.create_all(engine, tables=[model.__table__ for model in tables])
    state.set_factory(sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(service, "first_name", lambda session, person_id: "Maya")
    monkeypatch.setattr(next_step, "next_step", lambda vid: {"title": "Review your landing page", "detail": "Saved work is ready", "cta_label": "Review page", "href": f"/v/{vid}/site"})
    with state.db()() as session:
        session.add_all([models.Person(id="usr_a", display_name="Maya", email="synthetic-a@example.test", created_at=service.utcnow() - timedelta(days=30)),
                         models.Person(id="usr_b", display_name="B", email=None, created_at=service.utcnow())])
        session.commit()
    service.set_prefs("usr_a", {"digests": True})
    yield None, state
    state.set_factory(None)
    engine.dispose()


def _clock(monkeypatch, now):
    from app.email import service, triggers
    monkeypatch.setattr(service, "utcnow", lambda: now)
    monkeypatch.setattr(triggers, "utcnow", lambda: now)


@pytest.mark.parametrize("prepared", [False, True])
def test_queued_real_recipient_change_cancels_without_retargeting(env, monkeypatch, prepared):
    from app.core.jobs import RetryableError
    from app.db.models import Person
    from app.email import providers, service
    _, state = env
    calls = []
    def provider(message):
        calls.append(message)
        if prepared:
            raise providers.ProviderError("resend_unreachable:ReadTimeout", True)
        return "owned-accepted"
    monkeypatch.setitem(providers.PROVIDERS, "log", provider)
    mid = service.send_email("usr_a", "milestone", {"what": "plan"}, ref="changed-address")
    if prepared:
        with pytest.raises(RetryableError):
            service.deliver(mid)
    before = _rows(state, id=mid)[0]
    with state.db()() as session:
        session.get(Person, "usr_a").email = "new-owned-address@example.test"
        session.commit()
    expected_calls = len(calls)
    assert service.deliver(mid, attempt=2 if prepared else 1) == "suppressed"
    after = _rows(state, id=mid)[0]
    assert after.error == "recipient_changed" and len(calls) == expected_calls
    assert after.to_email == before.to_email == "synthetic-a@example.test"
    assert after.idempotency_key == before.idempotency_key
    assert after.context_json == before.context_json
    assert service.deliver(mid) == "suppressed" and len(calls) == expected_calls


@pytest.mark.parametrize("age", [timedelta(hours=2), timedelta(days=2)])
@pytest.mark.parametrize("policy", ["activity", "rollout", "global_cap"])
def test_known_never_dispatched_policy_retry_keeps_no_provider_clock(env, monkeypatch, age, policy):
    from app.db.models import AnalyticsEvent, Person
    from app.email import providers, service
    _, state = env
    now = service.utcnow()
    _clock(monkeypatch, now - age)
    mid = service.send_email("usr_a", "milestone", {"what": "plan"}, ref="known-policy-wait")
    _clock(monkeypatch, now)
    calls = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: calls.append(message) or "owned-accepted")
    if policy == "activity":
        with state.db()() as session:
            session.add(AnalyticsEvent(id="ane_owned_active", person_id="usr_a", event_name="session.active", occurred_at=now, props_json="{}"))
            session.commit()
    elif policy == "rollout":
        monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "false")
    else:
        with state.db()() as session:
            session.get(Person, "usr_b").email = "synthetic-b@example.test"
            session.commit()
        accepted = service.send_email("usr_b", "welcome", ref="occupy-global-cap")
        assert service.deliver(accepted) == "sent"
        monkeypatch.setenv("EMAIL_GLOBAL_HOURLY_CAP", "1")
    before = len(calls)
    assert service.deliver(mid, attempt=2) == "deferred"
    waiting = _rows(state, id=mid)[0]
    assert "_email_first_attempt_at" not in json.loads(waiting.context_json)
    assert "_email_delivery_payload" not in json.loads(waiting.context_json)
    with state.db()() as session:
        assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is None
    assert len(calls) == before
    later = now + timedelta(hours=2)
    _clock(monkeypatch, later)
    monkeypatch.delenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", raising=False)
    assert service.deliver(mid, attempt=2) == "sent"
    assert len(calls) == before + 1
    assert json.loads(_rows(state, id=mid)[0].context_json)["_email_first_attempt_at"] == later.isoformat()


def test_normalized_real_recipient_remains_the_same_address(env, monkeypatch):
    from app.db.models import Person
    from app.email import providers, service
    _, state = env
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "owned-accepted")
    mid = service.send_email("usr_a", "welcome", ref="normalized-identity")
    with state.db()() as session:
        session.get(Person, "usr_a").email = "  SYNTHETIC-A@EXAMPLE.TEST  "
        session.commit()
    assert service.deliver(mid) == "sent" and len(seen) == 1
    assert seen[0].to == "synthetic-a@example.test"


@pytest.mark.parametrize("recovery", ["config", "orphan"])
def test_known_pre_io_recovery_does_not_start_a_dispatch_clock(env, monkeypatch, recovery):
    from app.core.jobs import FatalError
    from app.db.models import AnalyticsEvent, Job
    from app.email import providers, service, triggers
    from sqlalchemy import select
    _, state = env
    now = service.utcnow()
    _clock(monkeypatch, now - timedelta(days=2))
    mid = service.send_email("usr_a", "milestone", {"what": "plan"}, ref="known-recovery")
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "owned-accepted")
    if recovery == "config":
        monkeypatch.delenv("COS_INTERNAL_ASSERTION_SECRET", raising=False)
        monkeypatch.setenv("EMAIL_TOKEN_SECRET", "x")
        with pytest.raises(FatalError, match="email_token_secret_missing"):
            service.deliver(mid)
        assert seen == []
        with state.db()() as session:
            assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is None
        monkeypatch.setenv("EMAIL_TOKEN_SECRET", "owned-recovery-token-secret-over-32-characters")
    else:
        with state.db()() as session:
            job = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + mid))
            job.status, job.attempts = "failed", 1
            session.commit()
    _clock(monkeypatch, now)
    if recovery == "config":
        assert service.requeue_failed() == 1
    else:
        assert triggers._recover_queued() == 1 and triggers._recover_queued() == 0
    with state.db()() as session:
        session.add(AnalyticsEvent(id="ane_owned_recovery_activity", person_id="usr_a", event_name="session.active", occurred_at=now, props_json="{}"))
        session.commit()
    assert service.deliver(mid, attempt=2) == "deferred" and seen == []
    assert "_email_first_attempt_at" not in json.loads(_rows(state, id=mid)[0].context_json)
    _clock(monkeypatch, now + timedelta(hours=2))
    assert service.deliver(mid, attempt=2) == "sent" and len(seen) == 1


def test_health_alert_direct_admission_keeps_no_dispatch_clock_during_old_policy_retry(env, monkeypatch):
    from app.db.models import AnalyticsEvent, Person
    from app.email import providers, service
    _, state = env
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    now = service.utcnow()
    _clock(monkeypatch, now - timedelta(days=2))
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "email_health_alert")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert service.run_health_check()
    [row] = _rows(state, template="email_health_alert")
    assert row.to_email == "operator@example.test"
    _clock(monkeypatch, now)
    with state.db()() as session:
        session.get(Person, "usr_a").email = "operator@example.test"
        session.add(AnalyticsEvent(id="ane_owned_health_active", person_id="usr_a", event_name="session.active", occurred_at=now, props_json="{}"))
        session.commit()
    seen = []
    monkeypatch.setenv("RESEND_API_KEY", "owned-local-fake-only")
    monkeypatch.setitem(providers.PROVIDERS, "resend", lambda message: seen.append(message) or "owned-health-accepted")
    assert service.deliver(row.id, attempt=2) == "deferred" and seen == []
    assert "_email_first_attempt_at" not in json.loads(_rows(state, id=row.id)[0].context_json)
    with state.db()() as session:
        assert session.get(AnalyticsEvent, service._uncertain_id(row.id)) is None
    _clock(monkeypatch, now + timedelta(hours=2))
    assert service.deliver(row.id, attempt=2) == "sent" and len(seen) == 1
    assert seen[0].to == "operator@example.test"



def test_legacy_uncertainty_not_erased_by_pre_io_config_requeue(env, monkeypatch):
    from app.db.models import AnalyticsEvent
    from app.db.supymem_models import EmailMessage
    from app.core.jobs import FatalError
    from app.email import providers, service
    _, state = env
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    now = service.utcnow()
    _clock(monkeypatch, now - timedelta(hours=2))
    mid = service.send_email("usr_a", "welcome", ref="legacy-config-clear")
    with state.db()() as session:
        row = session.get(EmailMessage, mid)
        context = json.loads(row.context_json)
        context.pop("_email_admission_version")
        row.context_json = json.dumps(context)
        session.commit()
    _clock(monkeypatch, now)
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(FatalError, match="resend_not_configured"):
        service.deliver(mid, attempt=2)
    with state.db()() as session:
        row = session.get(EmailMessage, mid)
        assert json.loads(row.context_json)["_email_first_attempt_at"] == (now - timedelta(hours=2)).isoformat()
        assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is not None
    _clock(monkeypatch, now + timedelta(days=2))
    monkeypatch.setenv("RESEND_API_KEY", "owned-local-fake-only")
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "resend", lambda message: seen.append(message) or "owned-local-id")
    assert service.requeue_failed() == 1
    with pytest.raises(FatalError, match="email_retry_window_expired"):
        service.deliver(mid, attempt=1)
    assert seen == []

@pytest.mark.parametrize("policy", ["activity", "rollout", "global_cap"])
def test_legacy_uncertainty_not_erased_by_policy_deferral_fresh_lease(env, monkeypatch, policy):
    from app.db.models import AnalyticsEvent, Person
    from app.db.supymem_models import EmailMessage
    from app.core.jobs import FatalError
    from app.email import providers, service
    _, state = env
    now = service.utcnow()
    _clock(monkeypatch, now - timedelta(hours=2))
    mid = service.send_email("usr_a", "milestone", {"what": "plan"}, ref="legacy-policy-clear")
    with state.db()() as session:
        row = session.get(EmailMessage, mid)
        context = json.loads(row.context_json)
        context.pop("_email_admission_version")
        row.context_json = json.dumps(context)
        session.commit()
    _clock(monkeypatch, now)
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "owned-accepted")
    if policy == "activity":
        with state.db()() as session:
            session.add(AnalyticsEvent(id="ane_owned_legacy_active", person_id="usr_a", event_name="session.active", occurred_at=now, props_json="{}"))
            session.commit()
    elif policy == "rollout":
        monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "false")
    else:
        with state.db()() as session:
            session.get(Person, "usr_b").email = "synthetic-b@example.test"
            session.commit()
        accepted = service.send_email("usr_b", "welcome", ref="legacy-occupy-global-cap")
        assert service.deliver(accepted) == "sent"
        monkeypatch.setenv("EMAIL_GLOBAL_HOURLY_CAP", "1")
    before = len(seen)
    assert service.deliver(mid, attempt=2) == "deferred" and len(seen) == before
    with state.db()() as session:
        row = session.get(EmailMessage, mid)
        context = json.loads(row.context_json)
        assert context["_email_first_attempt_at"] == (now - timedelta(hours=2)).isoformat()
        assert "_email_delivery_payload" not in context
        assert session.get(AnalyticsEvent, service._uncertain_id(mid)) is not None
    _clock(monkeypatch, now + timedelta(days=2))
    monkeypatch.delenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", raising=False)
    # The deferred job's first lease must retain the earlier unknown legacy attempt.
    with pytest.raises(FatalError, match="email_retry_window_expired"):
        service.deliver(mid, attempt=1)
    assert len(seen) == before


def _gtm_venture(state, now):
    from app.db.models import Venture, Workspace
    with state.db()() as session:
        session.add(Workspace(id="ws_gtm", kind="venture", name="Owned GTM", entry_mode="idea", owner_id="usr_a", created_at=now, updated_at=now))
        session.add(Venture(id="ven_gtm", workspace_id="ws_gtm", name="Owned GTM", raw_idea="Synthetic", stage="idea", created_at=now, updated_at=now))
        session.commit()


def _reconcile_gtm(state, now):
    from app.db.models import Venture
    from app.email import service, triggers
    with state.db()() as session:
        venture = session.get(Venture, "ven_gtm")
        triggers._outreach(session, venture, "usr_a", now,
            lambda person, template, context, *, ref: service.send_email(person, template, context, ref=ref))


def test_journey_saved_drafts_email_routes_to_real_review_ui_with_matching_copy(env, monkeypatch):
    from app.db.models import VentureDocument
    from app.email import providers, service
    _, state = env
    now = service.utcnow()
    _clock(monkeypatch, now)
    _gtm_venture(state, now)
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "outreach_drafts_ready")
    with state.db()() as session:
        for ident, channel in (("draft_email", "email"), ("draft_other_channel", "linkedin")):
            session.add(VentureDocument(id=ident, venture_id="ven_gtm", kind="outreach_draft", title="PRIVATE PROSPECT", body_md="PRIVATE DRAFT", provenance_json=json.dumps({"batch_id": "batch_gtm", "state": "draft", "channel": channel, "requires_batch_approval": True, "requires_journey_review": True}), created_at=now - timedelta(hours=3), updated_at=now))
        session.commit()
    _reconcile_gtm(state, now)
    _reconcile_gtm(state, now)
    [row] = _rows(state, template="outreach_drafts_ready")
    context = json.loads(row.context_json)
    assert context["path"] == "/gtm?venture=ven_gtm" and context["_venture_id"] == "ven_gtm"
    assert context["_batch_id"] == "batch_gtm" and context["_draft_ids"] == ["draft_email"] and context["count"] == 1
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "owned-accepted")
    assert service.deliver(row.id) == "sent" and len(seen) == 1
    for body in (seen[0].html, seen[0].text):
        assert "Review drafts" in body and "Previous chats" not in body and "Shared saved history" not in body
        assert "PRIVATE" not in body


@pytest.mark.parametrize("exact_journey_match", [False, True])
def test_sent_receipt_routes_only_when_actual_touch_matches_journey_document(env, monkeypatch, exact_journey_match):
    from app.db.models import GtmContact, GtmTouch, VentureDocument
    from app.email import providers, service
    _, state = env
    now = service.utcnow()
    _clock(monkeypatch, now)
    _gtm_venture(state, now)
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "outreach_sent")
    at = now - timedelta(hours=2)
    with state.db()() as session:
        session.add(GtmContact(id="contact_gtm", workspace_id="ws_gtm", stage_changed_at=now, created_at=now))
        session.flush()
        session.add(GtmTouch(id="touch_gtm", workspace_id="ws_gtm", venture_id="ven_gtm", contact_id="contact_gtm", direction="out", channel="email", simulated=0, send_mode="founder_gmail", message_id="owned-gmail-receipt", provider_thread_id="owned-thread", sent_at=at, created_at=at))
        session.add(VentureDocument(id="sent_journey", venture_id="ven_gtm", kind="outreach_draft", title="PRIVATE PROSPECT", body_md="PRIVATE DRAFT", provenance_json=json.dumps({"batch_id": "batch_gtm", "state": "sent", "channel": "email", "requires_journey_review": True, "contact_id": "contact_gtm" if exact_journey_match else "other_contact", "sent_at": at.isoformat()}), created_at=now - timedelta(hours=3), updated_at=now))
        session.commit()
    _reconcile_gtm(state, now)
    _reconcile_gtm(state, now)
    [row] = _rows(state, template="outreach_sent")
    context = json.loads(row.context_json)
    assert context["count"] == 1 and context["source_ids"] == ["touch:touch_gtm"]
    assert context["path"] == ("/gtm?venture=ven_gtm" if exact_journey_match else "/v/ven_gtm")
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "owned-accepted")
    assert service.deliver(row.id) == "sent" and len(seen) == 1
    for body in (seen[0].html, seen[0].text):
        assert ("Review outreach" in body) is exact_journey_match
        assert "sent 1 message" in body and "PRIVATE" not in body


def _saved_reply(state, now, ident, classification):
    from app.db.models import GtmContact, GtmTouch
    with state.db()() as session:
        if session.get(GtmContact, "contact_gtm") is None:
            session.add(GtmContact(id="contact_gtm", workspace_id="ws_gtm", stage_changed_at=now, created_at=now))
            session.flush()
        session.add(GtmTouch(id=ident, workspace_id="ws_gtm", venture_id="ven_gtm", contact_id="contact_gtm", direction="in", channel="email", simulated=0, classification=classification, message_id="owned-receipt-" + ident, provider_thread_id="owned-thread", sent_at=now - timedelta(hours=2), created_at=now - timedelta(hours=2), body_text="PRIVATE MESSAGE"))
        session.commit()


def test_automated_reply_is_excluded_from_lifecycle_and_digest_human_reply_counts(env, monkeypatch):
    from app.email import service, triggers
    _, state = env
    now = service.utcnow()
    _clock(monkeypatch, now)
    _gtm_venture(state, now)
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "replies_received")
    _saved_reply(state, now, "reply_human", "positive")
    _saved_reply(state, now, "reply_auto", "automated")
    _reconcile_gtm(state, now)
    [row] = _rows(state, template="replies_received")
    context = json.loads(row.context_json)
    assert context["count"] == 1 and context["source_ids"] == ["touch:reply_human"]
    with state.db()() as session:
        items = triggers._activity(session, "usr_a", now - timedelta(days=7), now)
    assert any("1 replies received" in item for item in items) and not any("2 replies received" in item for item in items)
    assert "PRIVATE" not in row.context_json


def test_queued_reply_reclassified_as_automated_cancels_before_provider(env, monkeypatch):
    from app.db.models import GtmTouch
    from app.email import providers, service
    _, state = env
    now = service.utcnow()
    _clock(monkeypatch, now)
    _gtm_venture(state, now)
    monkeypatch.setenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "replies_received")
    _saved_reply(state, now, "reply_human", "positive")
    _reconcile_gtm(state, now)
    [row] = _rows(state, template="replies_received")
    with state.db()() as session:
        session.get(GtmTouch, "reply_human").classification = "automated"
        session.commit()
    seen = []
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: seen.append(message) or "owned-accepted")
    assert service.deliver(row.id) == "suppressed" and seen == []
    assert _rows(state, id=row.id)[0].error == "source_no_longer_eligible"
