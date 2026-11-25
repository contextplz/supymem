"""send_email, delivery, preferences, signed unsubscribe tokens and the suppression list."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from contextlib import suppress as ignore_errors
from datetime import datetime, timedelta

from core.clock import utcnow as _aware_now
from core.ids import new_id
from sqlalchemy import func, literal, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db.models import AnalyticsEvent, Person, Workspace
from app.db.supymem_models import EmailMessage, EmailPreference, EmailSuppression
from app.email import templates
from app.email.providers import Outgoing, ProviderError, current_provider, provider_name as configured_provider_name
from app.state import db

log = logging.getLogger("app.email")


def utcnow():
    """Naive UTC: every timestamp column here (and analytics_events) is naive."""
    return _aware_now().replace(tzinfo=None)

MAX_ATTEMPTS = 5
DAILY_TRANSACTIONAL_CAP = 5
# Founder decision 2026-10-23: never more than 2 Supymem emails a day per person, across every category.
# Invites, internal admin/error alerts and receipts for actions the founder just took (export, archive) do not count.
FOUNDER_DAILY_CAP = 2
# Receipts for the founder's own action (export, archive, and action_failed: their research/build/publish
# failed with a saved retry step) are never capped; unsolicited engagement mail stays under FOUNDER_DAILY_CAP.
CAP_EXEMPT_TEMPLATES = ("beta_invite", "god_mode_admin", "error_alert", "venture_export_prepared", "venture_archived",
                        "action_failed")
QUIET_AFTER_ACTIVITY = timedelta(hours=1)
ACTIVE_EVENTS = ("session.active", "ui.screen_view", "chat.message", "feature.used")
# Existing mail stays enabled during rollout; new cases require an explicit flag or allowlist.
EXISTING_TEMPLATES = frozenset({"welcome", "milestone", "needs_you", "digest_daily", "weekly_insights",
    "suggestions_waiting", "nudge_d2", "nudge_d5", "nudge_d14", "beta_invite",
    "god_mode_requested", "god_mode_admin", "error_alert", "reminder"})
INTERNAL_TEMPLATES = frozenset({"error_alert", "god_mode_admin", "signup_summary",
    "credit_request_admin", "email_health_alert"})
PROOF_TEMPLATES = frozenset({"milestone", "progress_summary"})
SEND_STATUSES = ("sent", "delivered", "bounced", "complained")


def internal_recipient() -> str:
    address = os.environ.get("SUPYMEM_INTERNAL_EMAIL", "").strip().lower()
    if address and not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", address):
        raise ValueError("Set SUPYMEM_INTERNAL_EMAIL to one valid operator address and retry.")
    return address


def template_enabled(template: str, person_id: str | None = None) -> bool:
    flag = os.environ.get("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "").strip().lower()
    if flag and flag not in {"1", "true", "on", "yes"}:
        return False
    allowlist = os.environ.get("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST")
    ordinary = (template in {name.strip() for name in allowlist.split(",")} if allowlist is not None
                else bool(flag) or template in EXISTING_TEMPLATES)
    if ordinary:
        return True
    # Only held progress can use the proof exception; normal mail is unchanged
    # and the false global flag above remains an unconditional emergency stop.
    return (template in PROOF_TEMPLATES and bool(person_id) and person_id in {
        value.strip() for value in os.environ.get("SUPYMEM_EMAIL_PROOF_PERSON_ALLOWLIST", "").split(",")})


def _lock(session, scope: str, *, wait: bool = True) -> bool:
    """Transaction-scoped admission fence, including pseudo-person invite recipients."""
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(("email:" + scope).encode()).digest()[:8], "big", signed=True)
        statement = "SELECT pg_advisory_xact_lock(:key)" if wait else "SELECT pg_try_advisory_xact_lock(:key)"
        result = session.execute(text(statement), {"key": key})
        return True if wait else bool(result.scalar())
    return True


def _setting(name: str, default: int, *, maximum: int = 100_000) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if not 1 <= value <= maximum:
            raise ValueError
        return value
    except ValueError:
        raise ProviderError("email_rate_config_invalid", False) from None


def _rate_settings() -> dict[str, int]:
    return {"hourly_cap": _setting("EMAIL_GLOBAL_HOURLY_CAP", 100),
            "bounce_min_messages": _setting("EMAIL_CIRCUIT_BOUNCE_MIN_MESSAGES", 20),
            "bounce_percent": _setting("EMAIL_CIRCUIT_BOUNCE_PERCENT", 5, maximum=100),
            "complaint_count": _setting("EMAIL_CIRCUIT_COMPLAINT_COUNT", 1)}


def _uncertain_id(message_id: str) -> str:
    return "ane_email_uncertain_" + message_id


def _counted_sends(since):
    uncertain = select(AnalyticsEvent.id).where(
        AnalyticsEvent.id == literal("ane_email_uncertain_") + EmailMessage.id,
        AnalyticsEvent.person_id == EmailMessage.person_id,
        AnalyticsEvent.event_name == "email.outcome_uncertain", AnalyticsEvent.occurred_at > since).exists()
    return or_((EmailMessage.status.in_(SEND_STATUSES) & (EmailMessage.sent_at > since)),
               (EmailMessage.status.in_(("queued", "failed", "suppressed")) & uncertain))


def _reserve_uncertain(session, row, now) -> None:
    """Durable dispatch reservation: acceptance may survive a later DB rollback."""
    if session.get(AnalyticsEvent, _uncertain_id(row.id)) is None:
        session.add(AnalyticsEvent(id=_uncertain_id(row.id), person_id=row.person_id,
            event_name="email.outcome_uncertain", occurred_at=now,
            props_json=json.dumps({"schema": "email_attempt_v1", "message_id": row.id,
                                   "template": row.template, "category": row.category})))


def _fail_delivery(session, row, error: str, now) -> None:
    """Commit failure time with the outbox state, including delayed and requeued mail."""
    row.status, row.error = "failed", error
    session.add(AnalyticsEvent(id="ane_email_failure_" + new_id("failure"), person_id=row.person_id,
        event_name="email.failed", occurred_at=now,
        props_json=json.dumps({"schema": "email_failure_v1", "message_id": row.id,
                               "template": row.template, "category": row.category})))


def _recent_failures(session, since) -> int:
    events = session.scalars(select(AnalyticsEvent).where(
        AnalyticsEvent.id.startswith("ane_email_failure_", autoescape=True),
        AnalyticsEvent.event_name == "email.failed", AnalyticsEvent.occurred_at > since)).all()
    matched = set()
    for event in events:
        try:
            props = json.loads(event.props_json)
            if not isinstance(props, dict) or props.get("schema") != "email_failure_v1":
                continue
            row = session.get(EmailMessage, props.get("message_id"))
            if row and row.status == "failed" and row.person_id == event.person_id:
                matched.add(row.id)
        except (ValueError, TypeError):
            continue
    return len(matched)


def _daily_query(person_id: str, category: str, since, *, include_queued: bool):
    sent = _counted_sends(since)
    counted = or_(sent, (EmailMessage.status == "queued") & (EmailMessage.created_at > since)) if include_queued else sent
    category_match = (EmailMessage.category == templates.TRANSACTIONAL if category == templates.TRANSACTIONAL
                      else EmailMessage.category != templates.TRANSACTIONAL)
    return select(func.count()).select_from(EmailMessage).where(
        EmailMessage.person_id == person_id, category_match, counted)



def _total_query(person_id: str, since, *, include_queued: bool):
    """Every counted Supymem email to this person in the window, whatever its category."""
    sent = _counted_sends(since)
    counted = or_(sent, (EmailMessage.status == "queued") & (EmailMessage.created_at > since)) if include_queued else sent
    return select(func.count()).select_from(EmailMessage).where(
        EmailMessage.person_id == person_id, EmailMessage.template.not_in(CAP_EXEMPT_TEMPLATES), counted)


def _analytics(name: str, person_id: str, template: str, category: str) -> None:
    from app.analytics.events import emit
    try:
        emit(name, person_id=person_id, props={"template": template, "category": category})
    except Exception:  # Analytics must never invalidate an already committed provider outcome.
        log.warning("email_analytics_failed")


# ---------------------------------------------------------------- tokens
def _secret() -> bytes:
    s = os.environ.get("EMAIL_TOKEN_SECRET") or os.environ.get("COS_INTERNAL_ASSERTION_SECRET") or ""
    if len(s) < 16:
        raise ProviderError("email_token_secret_missing", False)
    return s.encode()


def _sig(payload: str) -> str:
    return hmac.new(_secret(), b"unsub|" + payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(person_id: str, category: str) -> str:
    payload = base64.urlsafe_b64encode(f"{person_id}|{category}".encode()).decode().rstrip("=")
    return f"{payload}.{_sig(payload)}"


def read_token(token: str) -> tuple[str, str] | None:
    """(person_id, category) or None. Never expires: an old email must still unsubscribe."""
    try:
        payload, sig = (token or "").split(".", 1)
        if not hmac.compare_digest(sig, _sig(payload)):
            return None
        person_id, category = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode().split("|", 1)
    except (ValueError, ProviderError):
        return None
    return (person_id, category) if category in (*templates.CATEGORIES, "all") else None


def unsubscribe_url(person_id: str, category: str) -> str:
    base = (os.environ.get("EMAIL_PUBLIC_URL") or os.environ.get("PUBLIC_GATEWAY_URL") or "https://gateway.supymem.com").rstrip("/")
    return f"{base}/public/email/unsubscribe?t={make_token(person_id, category)}"


def preferences_url(person_id: str) -> str:
    return unsubscribe_url(person_id, "all") + "&view=preferences"


# ----------------------------------------------------------- preferences
def _consent_id(person_id: str) -> str:
    return "ane_email_prefs_" + hashlib.sha256(person_id.encode()).hexdigest()


def _latest_consent(session, person_id: str):
    return session.scalar(select(AnalyticsEvent).where(AnalyticsEvent.person_id == person_id,
        AnalyticsEvent.event_name == "email.preferences_changed",
        AnalyticsEvent.id.startswith(_consent_id(person_id) + "_", autoescape=True))
        .order_by(AnalyticsEvent.occurred_at.desc(), AnalyticsEvent.id.desc()).limit(1))


def _prefs(session, person_id: str) -> dict[str, bool]:
    row = session.get(EmailPreference, person_id)
    prefs = {c: (c != "digests" if row is None else bool(getattr(row, c))) for c in templates.CATEGORIES}
    # Legacy digests=True was a default, not consent. Reaffirm once in preferences.
    consent = _latest_consent(session, person_id)
    try:
        opted_in = (consent is not None and consent.event_name == "email.preferences_changed"
                    and json.loads(consent.props_json) == {"schema": "email_preferences_v1", "digests": True})
    except (ValueError, TypeError):
        opted_in = False
    prefs["digests"] = prefs["digests"] and opted_in
    return prefs


def get_prefs(person_id: str) -> dict[str, bool]:
    with db()() as s:
        return _prefs(s, person_id)


def set_prefs(person_id: str, changes: dict[str, bool]) -> dict[str, bool]:
    with db()() as s:
        _lock(s, "person:" + person_id)
        row = s.get(EmailPreference, person_id)
        if row is None:
            row = EmailPreference(person_id=person_id, digests=False, updated_at=utcnow())
            s.add(row)
        for cat, on in changes.items():
            if cat in templates.CATEGORIES:
                setattr(row, cat, bool(on))
        row.updated_at = utcnow()
        if "digests" in changes:
            previous = _latest_consent(s, person_id)
            occurred = max(utcnow(), previous.occurred_at + timedelta(microseconds=1)) if previous else utcnow()
            s.add(AnalyticsEvent(id=_consent_id(person_id) + "_" + new_id("pref"), person_id=person_id,
                event_name="email.preferences_changed", occurred_at=occurred,
                props_json=json.dumps({"schema": "email_preferences_v1", "digests": bool(changes["digests"])})))
        s.commit()
    return get_prefs(person_id)


def unsubscribe(token: str) -> bool:
    parsed = read_token(token)
    if parsed is None:
        return False
    person_id, category = parsed
    set_prefs(person_id, {c: False for c in (templates.CATEGORIES if category == "all" else (category,))})
    from app.analytics.events import emit
    emit("email.unsubscribed", person_id=person_id, props={"category": category})
    return True


def suppress(email: str, reason: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    with db()() as s:
        if s.get(EmailSuppression, email) is None:
            s.add(EmailSuppression(email=email, reason=reason, created_at=utcnow()))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()


def is_suppressed(email: str) -> bool:
    with db()() as s:
        return s.get(EmailSuppression, (email or "").strip().lower()) is not None


# ------------------------------------------------------------- activity
def last_active(session, person_id: str, *, recipient_email: str | None = None):
    """Latest app activity for a person from analytics_events (own events or their workspaces')."""
    people = select(Person.id).where(func.lower(func.trim(Person.email)) == recipient_email.strip().lower()) if recipient_email else select(Person.id).where(Person.id == person_id)
    owned = select(Workspace.id).where((Workspace.owner_id == person_id) | Workspace.owner_id.in_(people))
    return session.scalar(select(func.max(AnalyticsEvent.occurred_at)).where(
        AnalyticsEvent.event_name.in_(ACTIVE_EVENTS),
        (AnalyticsEvent.person_id == person_id) | AnalyticsEvent.person_id.in_(people) | AnalyticsEvent.workspace_id.in_(owned)))


# ------------------------------------------------------------------ send
def _key(person_id: str, template: str, ref: str) -> str:
    return hashlib.sha256(json.dumps([person_id, template, ref]).encode()).hexdigest()[:40]


def send_email(to_person: str, template: str, context: dict | None = None, category: str | None = None,
               *, ref: str = "", to_email: str | None = None) -> str | None:
    """Queue one email for a person. Returns the outbox id, or None when it was not queued.

    Idempotent per (person, template, ref). Permanent refusals (no address, opted out, suppressed
    address) are recorded as ``suppressed``; temporary ones (daily cap, user is active right now) leave
    no row so a later scheduler pass can still send. ``to_email`` addresses someone with no Person row
    (beta invitees, admins); callers then pass a stable pseudo id such as ``invite:<hash>`` as ``to_person``.
    """
    if template not in templates.TEMPLATES:
        raise ValueError("email_template_unknown")
    category = category or templates.category_of(template)
    if category != templates.category_of(template):
        raise ValueError("email_category_mismatch")
    if not template_enabled(template, to_person):
        return None
    context = {key: value for key, value in dict(context or {}).items() if not key.startswith("_email_")}
    # All new admissions commit a prepared journal before I/O; no journal means no dispatch.
    # Legacy rows without this server-only marker retain conservative crash inference.
    context["_email_admission_version"] = 1
    key = _key(to_person, template, ref or str(context.get("ref", "")))
    transactional = category == templates.TRANSACTIONAL
    now = utcnow()
    with db()() as s:
        _lock(s, "person:" + to_person)
        existing = s.scalar(select(EmailMessage.id).where(EmailMessage.idempotency_key == key))
        if existing:
            return existing
        person = s.get(Person, to_person)
        address = (to_email or (person.email if person else "") or "").strip().lower()
        if not template_enabled(template) and (person is None or not person.email
                or address != person.email.strip().lower()):
            return None  # The proof exception requires the canonical person's current address.
        status, error = "queued", None
        if not address:
            status, error = "suppressed", "no_email"
        elif template in INTERNAL_TEMPLATES and address != internal_recipient():
            status, error = "suppressed", "internal_recipient_disallowed"
        elif s.get(EmailSuppression, address) is not None:
            status, error = "suppressed", "address_suppressed"
        elif not transactional and not _prefs(s, to_person).get(category, False):
            status, error = "suppressed", "opted_out"
        else:
            cap = DAILY_TRANSACTIONAL_CAP if transactional else 1
            if s.scalar(_daily_query(to_person, category, now - timedelta(hours=24), include_queued=True)) >= cap:
                return None
            if template not in CAP_EXEMPT_TEMPLATES and s.scalar(
                    _total_query(to_person, now - timedelta(hours=24), include_queued=True)) >= FOUNDER_DAILY_CAP:
                return None
            if not transactional:
                active = last_active(s, to_person)
                if active is not None and now - active < QUIET_AFTER_ACTIVITY:
                    return None
        subject = templates.render(template, context)[0]
        row = EmailMessage(id=new_id("eml"), person_id=to_person, to_email=address, template=template,
                           category=category, subject=subject, status=status, idempotency_key=key,
                           context_json=json.dumps(context, sort_keys=True), error=error, created_at=now)
        s.add(row)
        message_id = row.id
        try:
            if status == "queued":
                from app.core.jobs import enqueue, publish_wake
                enqueue(s, "email.send", {"message_id": message_id}, dedupe_key=f"email.send:{message_id}", priority=10)
            s.commit()
        except IntegrityError:  # lost a race with a concurrent identical send
            s.rollback()
            return s.scalar(select(EmailMessage.id).where(EmailMessage.idempotency_key == key))
    if status == "queued":
        publish_wake()
        _analytics("email.queued", to_person, template, category)
    return message_id


def _sender() -> str:
    return os.environ.get("EMAIL_FROM", "").strip()


def first_name(session, person_id: str) -> str:
    """Name Charlie greets: the editable first name on the Desk, else the real display name; never the "Founder" placeholder."""
    from app.api.greeting_api import clean_first_name

    desk = session.scalars(select(Workspace).where(Workspace.owner_id == person_id, Workspace.kind == "desk")).first()
    try:
        saved = json.loads(desk.settings_json or "{}").get("first_name") if desk else None
    except (ValueError, AttributeError):
        saved = None
    person = session.get(Person, person_id)
    shown = ((person.display_name if person else "") or "").split(" ")[0]
    return clean_first_name(saved) or ("" if shown.lower() == "founder" else clean_first_name(shown) or "")


def _delivery_receipts(session, since) -> list[tuple[str, str, str]]:
    """Only receipts persisted by the signed-callback path count as delivery evidence."""
    events = session.scalars(select(AnalyticsEvent).where(
        AnalyticsEvent.id.startswith("ane_email_", autoescape=True),
        AnalyticsEvent.event_name.in_(("email.delivered", "email.bounced", "email.complained")),
        AnalyticsEvent.occurred_at > since)).all()
    parsed = []
    for event in events:
        try:
            props = json.loads(event.props_json)
            if (isinstance(props, dict) and props.get("schema") == "email_delivery_v1"
                    and props.get("provider") == "resend" and isinstance(props.get("message_id"), str)
                    and isinstance(props.get("provider_id"), str) and isinstance(props.get("payload_digest"), str)):
                parsed.append((event, props))
        except (ValueError, TypeError):
            continue
    rows = {row.id: row for row in session.scalars(select(EmailMessage).where(
        EmailMessage.id.in_({props["message_id"] for _, props in parsed})))} if parsed else {}
    return [(event.event_name.removeprefix("email."), rows[props["message_id"]].template, props["message_id"])
            for event, props in parsed if props["message_id"] in rows
            and rows[props["message_id"]].provider_id == props["provider_id"]
            and rows[props["message_id"]].person_id == event.person_id]


def _circuit_state(session, now, settings: dict[str, int]) -> dict:
    since = now - timedelta(hours=24)
    receipts = _delivery_receipts(session, since)
    counts = {kind: len({mid for event, _, mid in receipts if event == kind})
              for kind in ("delivered", "bounced", "complained")}
    sent = session.scalar(select(func.count()).select_from(EmailMessage).where(
        EmailMessage.status.in_(SEND_STATUSES), EmailMessage.sent_at > since)) or 0
    reason = ("complaint_spike" if counts["complained"] >= settings["complaint_count"] else
              "bounce_spike" if sent >= settings["bounce_min_messages"]
              and counts["bounced"] * 100 >= sent * settings["bounce_percent"] else "")
    return {"paused": bool(reason), "reason": reason, "window_hours": 24,
            "accepted": sent, **counts}


def _health_alert(session, reason: str, counts: dict, now) -> bool:
    template = "email_health_alert"
    if template not in templates.TEMPLATES or not template_enabled(template):
        return False
    # Internal health mail has exactly one allowed recipient. Never copy user content.
    address = internal_recipient()
    if not address:
        return False
    person = "admin:" + hashlib.sha256(address.encode()).hexdigest()[:20]
    key = _key(person, template, now.strftime("%Y%m%d"))
    if session.scalar(select(EmailMessage.id).where(EmailMessage.idempotency_key == key)):
        return False
    if session.get(EmailSuppression, address) is not None:
        return False
    if session.scalar(_daily_query(person, templates.TRANSACTIONAL, now - timedelta(hours=24), include_queued=True)) >= DAILY_TRANSACTIONAL_CAP:
        return False
    context = {"reason": reason, "counts": {k: int(v) for k, v in counts.items() if isinstance(v, int)},
               "path": "/admin/settings", "_email_admission_version": 1}
    row = EmailMessage(id=new_id("eml"), person_id=person, to_email=address, template=template,
                       category=templates.TRANSACTIONAL, subject=templates.render(template, context)[0],
                       status="queued", idempotency_key=key, context_json=json.dumps(context, sort_keys=True), created_at=now)
    session.add(row)
    from app.core.jobs import enqueue
    enqueue(session, "email.send", {"message_id": row.id}, dedupe_key="email.send:" + row.id, priority=10)
    return True


def _defer(session, row, reason: str, after, *, circuit: dict | None = None) -> str:
    from app.core.jobs import enqueue, publish_wake
    row.error = reason
    enqueue(session, "email.send", {"message_id": row.id}, run_after=after,
            dedupe_key=f"email.send:{row.id}:defer:{after.strftime('%Y%m%d%H%M')}", priority=10)
    if circuit and circuit["paused"]:
        _health_alert(session, circuit["reason"], circuit, utcnow())
    session.commit()
    publish_wake()
    return "deferred"


def deliver(message_id: str, attempt: int = 1, *, job_id: str | None = None, _prepared: bool = False) -> str:
    """Worker side of email.send. Returns the final status; raises for retry/failure."""
    from app.core.jobs import FatalError, RetryableError

    with db()() as s:
        # ponytail: one global provider-call fence; use a durable reservation table if throughput exceeds this low-volume lane.
        if not _lock(s, "global_send", wait=job_id is None):
            from app.core.jobs import release_lease
            from app.db.models import Job
            release_lease(s, job_id, attempt=attempt)
            job = s.get(Job, job_id)
            if job is not None and job.status == "retry_wait" and job.attempts == attempt:
                if job.cancel_requested:
                    job.status, job.finished_at = "cancelled", utcnow()
                else:
                    job.attempts = max(0, attempt - 1)
                    job.run_after = job.scheduled_for = utcnow() + timedelta(seconds=15)
            s.commit()
            return "deferred"
        person_id = s.scalar(select(EmailMessage.person_id).where(EmailMessage.id == message_id))
        if person_id:
            _lock(s, "person:" + person_id)
        row = s.scalar(select(EmailMessage).where(EmailMessage.id == message_id).with_for_update())
        if row is None:
            raise FatalError("email_message_missing")
        if row.status != "queued":
            return row.status
        if row.template in INTERNAL_TEMPLATES and row.to_email.strip().lower() != internal_recipient():
            row.status, row.error = "suppressed", "internal_recipient_disallowed"
            s.commit()
            return "suppressed"
        person = s.get(Person, row.person_id)
        if person is not None and row.template not in INTERNAL_TEMPLATES and (person.email or "").strip().lower() != row.to_email.strip().lower():
            # The immutable queued address/key cannot be redirected after account changes.
            row.status, row.error = "suppressed", "recipient_changed"
            s.commit()
            return "suppressed"
        transactional = row.category == templates.TRANSACTIONAL
        # Re-check at send time: the person may have opted out or bounced since it was queued.
        if s.get(EmailSuppression, row.to_email.strip().lower()) is not None or (not transactional and not _prefs(s, row.person_id).get(row.category, False)):
            row.status, row.error = "suppressed", "suppressed_at_send"
            s.commit()
            return "suppressed"
        now = utcnow()
        try:
            context = json.loads(row.context_json)
            if not isinstance(context, dict):
                raise ValueError
            attempted = context.get("_email_first_attempt_at")
            inferred_crash = attempted is None and attempt > 1 and context.get("_email_admission_version") != 1
            # Only pre-journal admissions can have dispatched without committed preparation.
            # Preserve their conservative history through config recovery or a new deferred lease.
            if inferred_crash:
                attempted = row.created_at.isoformat()
                context["_email_first_attempt_at"] = attempted
                row.context_json = json.dumps(context, sort_keys=True)
                _reserve_uncertain(s, row, now)
            attempted_at = datetime.fromisoformat(attempted) if attempted else None
            expired = attempted_at is not None and now - attempted_at >= timedelta(hours=24)
        except (TypeError, ValueError):
            _fail_delivery(s, row, "email_context_invalid", now)
            s.commit()
            raise FatalError("email_context_invalid") from None
        if expired:
            # Resend's idempotency keys expire after 24 hours. Never redial an
            # uncertain accepted send after that boundary, even after a long pause.
            _fail_delivery(s, row, "email_retry_window_expired", now)
            s.commit()
            raise FatalError("email_retry_window_expired")
        if not template_enabled(row.template, person.id if person is not None else None):
            return _defer(s, row, "lifecycle_disabled", now + timedelta(hours=1))
        if row.template == "app_release":
            from app.release_notices.publication import ReleaseUnavailable
            from app.release_notices.service import refresh_context as refresh_release
            try:
                context = refresh_release(s, row.person_id, context)
            except ReleaseUnavailable:
                return _defer(s, row, "release_source_unavailable", now + timedelta(hours=1))
        else:
            from app.email.triggers import refresh_context
            context = refresh_context(s, row.person_id, row.template, context)
        if context is None:
            row.status, row.error = "suppressed", "source_no_longer_eligible"
            s.commit()
            return "suppressed"
        active = last_active(s, row.person_id, recipient_email=row.to_email)
        if active is not None and now - active < QUIET_AFTER_ACTIVITY:
            return _defer(s, row, "quiet_after_activity", active + QUIET_AFTER_ACTIVITY + timedelta(seconds=1))
        cap = DAILY_TRANSACTIONAL_CAP if transactional else 1
        if s.scalar(_daily_query(row.person_id, row.category, now - timedelta(hours=24), include_queued=False)
                    .where(EmailMessage.id != row.id)) >= cap:
            return _defer(s, row, "daily_cap", now + timedelta(hours=1))
        if row.template not in CAP_EXEMPT_TEMPLATES and s.scalar(
                _total_query(row.person_id, now - timedelta(hours=24), include_queued=False).where(EmailMessage.id != row.id)) >= FOUNDER_DAILY_CAP:
            return _defer(s, row, "daily_cap", now + timedelta(hours=1))
        try:
            settings = _rate_settings()
            sent_hour = s.scalar(select(func.count()).select_from(EmailMessage).where(
                _counted_sends(now - timedelta(hours=1)), EmailMessage.id != row.id)) or 0
            if sent_hour >= settings["hourly_cap"]:
                return _defer(s, row, "global_hourly_cap", now + timedelta(hours=1))
            if not transactional:
                circuit = _circuit_state(s, now, settings)
                if circuit["paused"]:
                    return _defer(s, row, "email_circuit_open", now + timedelta(hours=1), circuit=circuit)
            provider = current_provider()
            provider_name = configured_provider_name()
            if provider_name == "resend" and not os.environ.get("RESEND_API_KEY"):
                raise ProviderError("resend_not_configured", False)
            if context.get("_email_dispatch_provider", provider_name) != provider_name:
                raise ProviderError("email_dispatch_provider_changed", False)
            if provider_name not in {"resend", "log"} and attempted_at is not None and not _prepared:
                # SES has no idempotency key. A prepared attempt can already have
                # been accepted, even when no response or DB outcome survived.
                raise ProviderError("email_non_idempotent_outcome_unconfirmed", False)
            if ((not transactional or row.template in templates.ADDRESS_REQUIRED)
                    and provider_name != "log" and not os.environ.get("EMAIL_PHYSICAL_ADDRESS")):
                raise ProviderError("email_physical_address_missing", False)
            payload = context.get("_email_delivery_payload")
            if payload is None:
                sender = _sender()
                if provider_name != "log" and not sender:
                    raise ProviderError("email_from_missing", False)
                headers, unsub, prefs = {}, "", ""
                if not transactional:
                    unsub = unsubscribe_url(row.person_id, row.category)
                    prefs = preferences_url(row.person_id)
                    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
                ctx = {"first_name": first_name(s, row.person_id), **context}
                subject, html, plain = templates.render(row.template, ctx, unsubscribe_url=unsub, prefs_url=prefs)
                payload = {"subject": subject, "html": html, "text": plain, "sender": sender,
                           "reply_to": os.environ.get("EMAIL_REPLY_TO", ""), "headers": headers}
                context["_email_delivery_payload"] = payload
                context.setdefault("_email_first_attempt_at", attempted or now.isoformat())
            context.setdefault("_email_dispatch_provider", provider_name)
            row.context_json = json.dumps(context, sort_keys=True)
            if s.get(AnalyticsEvent, _uncertain_id(row.id)) is None:
                _reserve_uncertain(s, row, now)
                s.commit()
                # Re-read everything under fresh fences after the durable prepare.
                # A concurrent worker can complete this row during the brief gap.
                s.close()
                return deliver(message_id, attempt, job_id=job_id, _prepared=True)
            sending_job_id = None
            if job_id:
                from app.core.jobs import LEASED
                from app.db.models import Job
                job = s.get(Job, job_id)
                try:
                    sending_job = (job is not None and job.kind == "email.send" and job.status in LEASED
                                   and job.attempts == attempt and job.locked_by is not None
                                   and json.loads(job.payload_json).get("message_id") == row.id)
                except (TypeError, ValueError, AttributeError):
                    sending_job = False
                if sending_job:
                    sending_job_id = job.id
            try:
                provider_id = provider(Outgoing(to=row.to_email, **payload, idempotency_key=row.idempotency_key))
            except ProviderError:
                raise
            except Exception:
                # An unknown provider exception may follow acceptance; do not redial or expose its body.
                raise ProviderError("email_provider_outcome_unconfirmed", False) from None
        except ProviderError as exc:
            row.error = exc.code
            # Timeout/5xx/unknown acceptance can already have reached the recipient.
            # Reserve caps without inventing a sent timestamp or delivery receipt.
            if (exc.code in {"email_provider_outcome_unconfirmed", "email_non_idempotent_outcome_unconfirmed",
                            "email_dispatch_provider_changed", "resend_response_unconfirmed", "resend_http_408", "resend_http_409"}
                    or exc.code.startswith("resend_unreachable:") or exc.code.startswith("resend_http_5")):
                _reserve_uncertain(s, row, now)
            if exc.retryable and attempt < MAX_ATTEMPTS:
                s.commit()
                raise RetryableError(exc.code) from exc
            _fail_delivery(s, row, exc.code, now)
            s.commit()
            raise FatalError(exc.code) from exc
        row.status, row.provider_id, row.sent_at, row.error = "sent", provider_id, utcnow(), None
        if sending_job_id:
            # Commit the actual sending lease with acceptance, never a prior successful deferral.
            context["_email_sent_job_id"] = sending_job_id
            context["_email_sent_job_attempt"] = attempt
            row.context_json = json.dumps(context, sort_keys=True)
        try:
            s.commit()
        except SQLAlchemyError:
            # The provider may have accepted and the commit outcome is unknown.
            # Only providers with a safe frozen-key retry can retry automatically.
            with ignore_errors(SQLAlchemyError):
                s.rollback()
            with ignore_errors(SQLAlchemyError):
                s.close()
            if provider_name in {"resend", "log"} and attempt < MAX_ATTEMPTS:
                raise RetryableError("email_delivery_commit_unconfirmed") from None
            raise FatalError("email_delivery_commit_unconfirmed") from None
        person_id, template, category = row.person_id, row.template, row.category
    _analytics("email.sent", person_id, template, category)
    return "sent"


# ---------------------------------------------------------- admin state
CONFIG_ERRORS = ("resend_not_configured", "email_from_missing", "email_physical_address_missing", "email_token_secret_missing", "email_provider_unknown", "email_rate_config_invalid")


def status() -> dict:
    """Content-free readiness, outbox aggregates and signed delivery evidence."""
    from app.worker.catalog import enabled_kinds, tick_kinds
    provider = os.environ.get("EMAIL_PROVIDER", "log").strip().lower()
    problems = []
    if provider == "log":
        problems.append("provider_is_log: mail is only logged, never sent (set EMAIL_PROVIDER=resend)")
    elif provider not in ("resend", "ses"):
        problems.append("email_provider_unknown")
    if provider == "resend" and not os.environ.get("RESEND_API_KEY"):
        problems.append("resend_api_key_missing")
    if provider in {"resend", "ses"} and not _sender():
        problems.append("email_from_missing: Set EMAIL_FROM to your verified sender, then requeue failed email.")
    if provider != "log" and not os.environ.get("EMAIL_PHYSICAL_ADDRESS"):
        problems.append("physical_address_missing: marketing and invite mail is refused without it")
    webhook_ok = bool(os.environ.get("RESEND_WEBHOOK_SECRET"))
    if provider == "resend" and not webhook_ok:
        problems.append("resend_webhook_secret_missing: bounces and complaints will not suppress")
    elif provider == "resend":
        try:
            webhook_ok = len(base64.b64decode(os.environ["RESEND_WEBHOOK_SECRET"].removeprefix("whsec_"), validate=True)) >= 16
        except ValueError:
            webhook_ok = False
        if not webhook_ok:
            problems.append("resend_webhook_secret_invalid")
    token_ok = len(os.environ.get("EMAIL_TOKEN_SECRET") or os.environ.get("COS_INTERNAL_ASSERTION_SECRET") or "") >= 16
    if not token_ok:
        problems.append("email_token_secret_missing")
    hourly_ok = {"email.send", "email.schedule"} <= enabled_kinds() and "email.schedule" in tick_kinds("hourly")
    if not hourly_ok:
        problems.append("email_worker_kinds_missing")
    flag = os.environ.get("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "").strip().lower()
    if flag and flag not in {"1", "true", "on", "yes", "0", "false", "off", "no"}:
        problems.append("email_lifecycle_flag_invalid")
    allowlist = os.environ.get("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST")
    if allowlist is not None and any(name.strip() and name.strip() not in templates.TEMPLATES for name in allowlist.split(",")):
        problems.append("email_template_allowlist_unknown")
    try:
        settings = _rate_settings()
    except ProviderError:
        settings = None
        problems.append("email_rate_config_invalid")
    now, since = utcnow(), utcnow() - timedelta(days=7)
    summary_kinds = ("queued", "sent", "delivered", "bounced", "complained", "suppressed", "failed")
    by_template = {name: dict.fromkeys(summary_kinds, 0) for name in templates.TEMPLATES}
    with db()() as session:
        grouped = session.execute(select(EmailMessage.template, EmailMessage.status, func.count()).where(
            EmailMessage.created_at > since).group_by(EmailMessage.template, EmailMessage.status)).all()
        counts = {}
        for name, kind, count in grouped:
            counts[kind] = counts.get(kind, 0) + count
            if name in by_template and kind in summary_kinds:
                by_template[name][kind] = count
        errors = dict(session.execute(select(EmailMessage.error, func.count()).where(
            EmailMessage.created_at > since, EmailMessage.status == "failed").group_by(EmailMessage.error)).all())
        delivery = {kind: set() for kind in ("delivered", "bounced", "complained")}
        per_receipt = {}
        for kind, name, mid in _delivery_receipts(session, since):
            delivery[kind].add(mid)
            per_receipt.setdefault((name, kind), set()).add(mid)
        for (name, kind), ids in per_receipt.items():
            if name in by_template:
                by_template[name][kind] = len(ids)
        # A delayed queue's accepted timestamp, rather than its creation date, defines "sent lately".
        for name, count in session.execute(select(EmailMessage.template, func.count()).where(
                EmailMessage.status.in_(SEND_STATUSES), EmailMessage.sent_at > since).group_by(EmailMessage.template)):
            if name in by_template:
                by_template[name]["sent"] = count
        circuit = _circuit_state(session, now, settings) if settings else {
            "paused": True, "reason": "email_rate_config_invalid", "window_hours": 24}
        failed_hour = _recent_failures(session, now - timedelta(hours=1))
    return {"provider": provider, "ready": not problems, "problems": problems,
            "config_health": {"provider_configured": provider in {"resend", "ses"},
                "address_configured": bool(os.environ.get("EMAIL_PHYSICAL_ADDRESS")),
                "token_configured": token_ok, "webhook_configured": webhook_ok,
                "hourly_scheduler_and_send_enabled": hourly_ok},
            "rollout": {"enabled_templates": [name for name in templates.TEMPLATES if template_enabled(name)],
                "disabled_templates": [name for name in templates.TEMPLATES if not template_enabled(name)],
                "proof_templates": sorted(PROOF_TEMPLATES),
                "proof_person_allowlist_configured": any(value.strip() for value in
                    os.environ.get("SUPYMEM_EMAIL_PROOF_PERSON_ALLOWLIST", "").split(","))},
            "rate_limits": settings, "circuit_breaker": circuit, "failed_last_hour": failed_hour,
            "last_7_days": counts, "by_template_last_7_days": by_template, "failed_by_error": errors,
            "delivery_reporting": {"basis": "signed_resend_callbacks",
                "counts_are": "distinct_messages_per_event_not_exclusive",
                "sent_means": "provider_accepted_or_log_recorded",
                "last_7_days": {kind: len(ids) for kind, ids in delivery.items()}},
            "requeueable": sum(n for error, n in errors.items() if error in CONFIG_ERRORS)}


def run_health_check() -> bool:
    """Hourly health warning, deduped by day and sent only through the normal outbox."""
    state = status()
    if state["provider"] == "log":
        return False
    circuit = state["circuit_breaker"]
    reason = (circuit["reason"] if circuit["paused"] else
              "config_errors" if not state["ready"] else
              "delivery_failures" if state["failed_last_hour"] >= 5 else "")
    if not reason:
        return False
    with db()() as session:
        _lock(session, "global_send")
        queued = _health_alert(session, reason, {"failed": state["failed_last_hour"],
            **{key: value for key, value in circuit.items() if isinstance(value, int)}}, utcnow())
        session.commit()
    if queued:
        from app.core.jobs import publish_wake
        publish_wake()
    return queued


def requeue_failed() -> int:
    """After fixing config: put messages that failed only for missing config back on the worker queue."""
    from app.core.jobs import enqueue, publish_wake

    with db()() as s:
        _lock(s, "global_send")
        rows = s.scalars(select(EmailMessage).where(EmailMessage.status == "failed", EmailMessage.error.in_(CONFIG_ERRORS))
                         .with_for_update()).all()
        for row in rows:
            row.status, row.error = "queued", None
            if s.get(AnalyticsEvent, _uncertain_id(row.id)) is None:
                context = json.loads(row.context_json)
                row.context_json = json.dumps({key: value for key, value in context.items() if not key.startswith("_email_") or key == "_email_admission_version"}, sort_keys=True)
            enqueue(s, "email.send", {"message_id": row.id}, dedupe_key=f"email.send:{row.id}:requeue:{int(utcnow().timestamp())}", priority=10)
        s.commit()
        n = len(rows)
    if n:
        publish_wake()
    return n
