"""Venture-scoped consent and storage with a real public-route probe.

No generated page owns credentials or persistence. Publication depends on a
receipt from the public Gateway route, read back from durable storage. Test
submissions are permanently labelled and excluded from demand counts.
"""
from __future__ import annotations

from datetime import timedelta
from math import ceil
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from app.db.models import Venture
from app.db.supymem_models import AppEvent, VentureWaitlist, VentureWaitlistEntry
from app.db.session import serialize_writes
from app.product_analytics.target_app import select_venture_app
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

VERSION = "early-access-v1"
log = logging.getLogger("app.conversion.waitlist")


class WaitlistError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def public_url(venture_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", venture_id):
        raise WaitlistError("waitlist_scope_invalid", 404)
    from app.core.local_mode import is_local
    if is_local():
        from app.firstlook.waitlist import public_gateway_base
        try:
            return f"{public_gateway_base()}/public/waitlists/{venture_id}"
        except (ValueError, OSError):
            raise WaitlistError("waitlist_local_setup_required", 503) from None
    base = os.environ.get("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com").rstrip("/")
    url = urlsplit(base)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
        raise WaitlistError("waitlist_gateway_unavailable", 503)
    return f"{base}/public/waitlists/{venture_id}"


def consent_text(name):
    from app.core.local_mode import is_local
    if is_local():
        return f"I want early-access updates about {name}. Its founder will store my email and contact me about early access."
    return f"I want early-access updates about {name}. Supymem will store my email for this venture's founder to contact me about early access."


def _venture(session, venture_id, workspace_id=None):
    venture = session.get(Venture, venture_id)
    if (not venture or venture.archived_at or venture.stage == "purged"
            or not venture.workspace_id or (workspace_id and venture.workspace_id != workspace_id)):
        raise WaitlistError("waitlist_not_found", 404)
    return venture


def configure(venture_id, workspace_id, *, enabled, actor):
    with db()() as session:
        serialize_writes(session, key=f"waitlist:{venture_id}")
        _venture(session, venture_id, workspace_id)
        row = session.get(VentureWaitlist, venture_id)
        if row and row.workspace_id != workspace_id:
            raise WaitlistError("waitlist_not_found", 404)
        if row is None:
            row = VentureWaitlist(venture_id=venture_id, workspace_id=workspace_id)
            session.add(row)
        if row.enabled != enabled or row.consent_version != VERSION:
            row.verified_entry_id = row.verified_at = row.probe_hash = row.probe_expires_at = None
        changed = row.enabled != enabled
        row.enabled, row.enabled_by, row.consent_version, row.updated_at = enabled, actor, VERSION, utcnow()
        session.commit()
    from app.autopilot import receipts
    receipts.persisted_record(VentureWaitlist, venture_id, {"enabled": enabled})
    if changed:
        from app.analytics.error_events import actor_person_id
        from app.analytics.funnel import emit_for_venture
        emit_for_venture("waitlist.enabled" if enabled else "waitlist.disabled", venture_id, workspace_id=workspace_id,
                         person_id=actor_person_id(actor))
    return status(venture_id, workspace_id)


def public_details(venture_id):
    with db()() as session:
        venture = _venture(session, venture_id)
        row = session.get(VentureWaitlist, venture_id)
        if not row or row.workspace_id != venture.workspace_id or not row.enabled or row.consent_version != VERSION:
            raise WaitlistError("waitlist_not_found", 404)
        return {"name": venture.name, "url": public_url(venture_id), "consent": consent_text(venture.name), "consent_version": VERSION}


def beacon_key(venture_id: str) -> str | None:
    from app.apps.keys import ensure_ingest_key
    from app.product_analytics.target_app import select_venture_app
    from app.state import db

    with db()() as session:
        app = select_venture_app(session, venture_id)
        if app is None:
            return None
        app_id = app.id
    return ensure_ingest_key(app_id)["secret"]


def clean_email(value, *, is_test=False):
    if not isinstance(value, str):
        raise WaitlistError("waitlist_email_invalid", 422)
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", email):
        raise WaitlistError("waitlist_email_invalid", 422)
    local = email.split("@")[0]
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise WaitlistError("waitlist_email_invalid", 422)
    if email.endswith((".invalid", ".test", ".localhost", "@example.com", "@example.org", "@example.net")) and not is_test:
        raise WaitlistError("waitlist_email_invalid", 422)
    return email


def capture(venture_id, payload, *, with_new=False, founder_import=False):
    from app.core.local_mode import is_local
    if founder_import and not is_local():
        raise WaitlistError("waitlist_input_invalid", 422)
    if not isinstance(payload, dict) or set(payload) - {"email", "consent", "consent_version", "website", "test_token", "consent_digest", "variant", "utm_campaign", "utm_source", "utm_medium", "experiment_id", "ref"}:
        raise WaitlistError("waitlist_input_invalid", 422)
    attribution = {key: payload[key] for key in ("variant", "utm_campaign", "utm_source", "utm_medium", "experiment_id") if payload.get(key)}
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) for value in attribution.values()):
        raise WaitlistError("waitlist_attribution_invalid", 422)
    if "ref" in payload and (not isinstance(payload["ref"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,80}", payload["ref"])):
        raise WaitlistError("waitlist_attribution_invalid", 422)
    if (payload.get("consent") is not True and payload.get("consent") != "yes") or payload.get("consent_version") != VERSION or payload.get("website"):
        raise WaitlistError("waitlist_consent_required", 422)
    if founder_import:
        attribution["source"] = "founder_import"
    first_signup_app_id = None
    with db()() as session:
        serialize_writes(session, key=f"waitlist:{venture_id}")
        venture = _venture(session, venture_id)
        row = session.get(VentureWaitlist, venture_id)
        if not row or row.workspace_id != venture.workspace_id or (not row.enabled and not founder_import) or row.consent_version != VERSION:
            raise WaitlistError("waitlist_not_found", 404)
        if payload.get("consent_digest") != hashlib.sha256(consent_text(venture.name).encode()).hexdigest():
            raise WaitlistError("waitlist_consent_changed", 422)
        token = payload.get("test_token")
        is_test = token is not None
        if is_test:
            if (not isinstance(token, str) or not row.probe_hash or not row.probe_expires_at
                    or as_utc(row.probe_expires_at) < as_utc(utcnow())
                    or not hmac.compare_digest(row.probe_hash, hashlib.sha256(token.encode()).hexdigest())):
                raise WaitlistError("waitlist_test_invalid", 422)
        email = clean_email(payload.get("email"), is_test=is_test)
        if is_test and not email.endswith("@qa.invalid"):
            raise WaitlistError("waitlist_test_invalid", 422)
        entry = session.scalar(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id,
            VentureWaitlistEntry.workspace_id == row.workspace_id, VentureWaitlistEntry.email == email))
        newly_created = entry is None
        if entry is None:
            if not is_test and not founder_import:
                from app.experiments.channel_tests import referred_by
                attribution.update(referred_by(session, venture_id, row.workspace_id, email, payload.get("ref")))
            entry = VentureWaitlistEntry(id=new_id("wle"), venture_id=venture_id, workspace_id=row.workspace_id,
                email=email, consent_version=VERSION, consent_text=consent_text(venture.name), is_test=is_test,
                props=json.dumps(attribution), created_at=utcnow())
            session.add(entry)
            if not is_test and not founder_import:
                from app.experiments.channel_tests import referral_code
                referral_code(entry)
                from app.tasks.service import emit_domain_event
                emit_domain_event(session, workspace_id=row.workspace_id, venture_id=venture_id,
                                  event_type="waitlist.signup", event_id=entry.id, data={"source": "managed_waitlist"})
                # A consented signup and its product-analytics receipt must commit
                # together. A later best-effort emit could lose the event while the
                # waitlist still claims the submission was recorded.
                app = select_venture_app(session, venture_id, row.workspace_id)
                if app is not None:
                    prior = session.scalar(select(func.count()).select_from(AppEvent).where(
                        AppEvent.app_id == app.id,
                        AppEvent.name == "signup",
                    )) or 0
                    session.add(AppEvent(
                        app_id=app.id,
                        workspace_id=row.workspace_id,
                        ts=entry.created_at,
                        name="signup",
                        props=json.dumps({"source": "managed_waitlist", "consented": True, **attribution}),
                    ))
                    if not prior:
                        first_signup_app_id = app.id
        if is_test:
            row.probe_hash = None
        evidence_event_key = f"waitlist.signup:{entry.id}" if newly_created and not is_test and not founder_import else None
        session.commit()
    if evidence_event_key:
        try:
            from app.ventures.stage import after_evidence
            after_evidence(venture_id, evidence_event_key)
        except Exception:
            log.warning("founder_stage_waitlist_hook_failed")
    if first_signup_app_id:
        # Notification is a convenience after the durable pair commits. Its
        # failure cannot turn a saved submission into an ambiguous HTTP result.
        try:
            from app.desktop_notices import enqueue as notice
            notice(title="First browser-reported signup", body=f"First browser-reported signup on app {first_signup_app_id[-8:]}",
                   venture_id=venture_id, kind="analytics")
        except Exception:
            log.exception("first signup notice failed")
        from app.email.triggers import on_first_signup
        on_first_signup(venture_id)  # queues only; never raises
    # No email, existing/new distinction or entry identifier in public response.
    result = {"received": True, "email_verified": False}
    return (result, newly_created and not is_test) if with_new else result


def verified_receipt(session, venture_id, workspace_id):
    venture = _venture(session, venture_id, workspace_id)
    row = session.get(VentureWaitlist, venture_id)
    proof = session.get(VentureWaitlistEntry, row.verified_entry_id) if row and row.verified_entry_id else None
    if not (row and row.workspace_id == workspace_id and row.enabled and row.consent_version == VERSION
            and row.verified_at and proof and proof.is_test
            and as_utc(utcnow()) - timedelta(hours=24) <= as_utc(row.verified_at) <= as_utc(utcnow())
            and proof.consent_text == consent_text(venture.name)
            and (proof.venture_id, proof.workspace_id, proof.consent_version) == (venture_id, workspace_id, VERSION)):
        raise WaitlistError("waitlist_verification_required")
    return {"entry_id": proof.id, "verified_at": as_utc(row.verified_at).isoformat(),
            "consent_version": VERSION, "url": public_url(venture_id)}


def status(venture_id, workspace_id, *, include_leads=False):
    with db()() as session:
        _venture(session, venture_id, workspace_id)
        from app.planning.publication_evidence import public_site_evidence
        published_site = public_site_evidence(session, workspace_id=workspace_id, venture_id=venture_id)
        row = session.get(VentureWaitlist, venture_id)
        if row and row.workspace_id != workspace_id:
            raise WaitlistError("waitlist_not_found", 404)
        entries = select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id,
            VentureWaitlistEntry.workspace_id == workspace_id, VentureWaitlistEntry.is_test.is_(False))
        count = session.scalar(select(func.count()).select_from(entries.subquery())) or 0
        try:
            verified_receipt(session, venture_id, workspace_id)
            verified = True
        except WaitlistError:
            verified = False
        result = {"venture_id": venture_id, "enabled": bool(row and row.enabled), "url": public_url(venture_id),
            "consent_version": VERSION, "submission_verified": verified,
            "verified_at": as_utc(row.verified_at).isoformat() if verified else None,
            "probe_retry_after_seconds": max(0, ceil((as_utc(row.probe_expires_at) - as_utc(utcnow())).total_seconds()))
                if row and row.probe_expires_at else 0,
            "total": count, "email_verified": False, "published_site": published_site,
            "note": "Consented interest, not verified email ownership, active users or payments."}
        if include_leads:
            result["leads"] = [{"id": e.id, "email": e.email, "created_at": as_utc(e.created_at).isoformat(),
                "consent_text": e.consent_text, **json.loads(e.props or "{}")} for e in session.scalars(entries.order_by(VentureWaitlistEntry.created_at.desc()).limit(100))]
        return result


def probe(venture_id, workspace_id):
    """Founder-run waitlist test. The outcome is recorded once per attempt; test rows never count as signups."""
    from app.analytics.funnel import emit_for_venture
    try:
        result = _probe(venture_id, workspace_id)
    except WaitlistError as exc:
        if exc.code not in {"waitlist_enable_required", "waitlist_probe_recent"}:  # not an attempt
            emit_for_venture("waitlist.test_failed", venture_id, workspace_id=workspace_id, props={"code": exc.code})
        raise
    emit_for_venture("waitlist.tested", venture_id, workspace_id=workspace_id)
    return result


def _probe(venture_id, workspace_id):
    """One bounded public POST; never retry an ambiguous submission."""
    from app.core.local_mode import is_local
    if is_local():
        from app.pagebuilder.local_publish import waitlist_destination, _publication, _https_url
        from core.research_web.guard import get_checked
        with db()() as session:
            venture = _venture(session, venture_id, workspace_id)
            configured = session.get(VentureWaitlist, venture_id)
            if not configured or not configured.enabled or configured.workspace_id != workspace_id:
                raise WaitlistError("waitlist_enable_required")
            name = venture.name
        destination = waitlist_destination(venture_id, name)
        if destination["mode"] == "setup":
            raise WaitlistError("waitlist_local_setup_required")
        if destination["mode"] == "mailto":
            return {**status(venture_id, workspace_id), "url": destination["url"],
                "destination_tested": True, "submission_verified": False,
                "note": "Your waitlist email is valid, but delivery is not verified. Open your waitlist link, send a test with your email app, then check your inbox. Import consented signups in Website > Collect signups."}
        if destination["mode"] == "netlify":
            publication = _publication(venture_id, workspace_id)
            if not publication or publication.get("state") != "live" or publication.get("provider") != "netlify":
                raise WaitlistError("waitlist_local_publish_required")
            try:
                url = _https_url(publication["url"]) + "/waitlist.html"
            except ValueError:
                raise WaitlistError("waitlist_local_form_unconfirmed", 503) from None
            with db()() as session:
                serialize_writes(session, key=f"waitlist:{venture_id}")
                configured = session.get(VentureWaitlist, venture_id)
                if not configured or configured.workspace_id != workspace_id or not configured.enabled or configured.consent_version != VERSION:
                    raise WaitlistError("waitlist_enable_required")
                if configured.probe_expires_at and as_utc(configured.probe_expires_at) > as_utc(utcnow()):
                    raise WaitlistError("waitlist_probe_recent", 429)
                configured.probe_hash = configured.verified_entry_id = configured.verified_at = None
                configured.probe_expires_at = utcnow() + timedelta(seconds=60)
                session.commit()
            try:
                with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
                    response = get_checked(client, url, max_redirects=0, https_only=True)
                if response.status_code != 200 or 'data-netlify="true"' not in response.text or f'name="waitlist-{venture_id}"' not in response.text:
                    raise ValueError("form_missing")
                from app.credits import metered
                def send_test():
                    with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
                        return client.post(publication["url"], data={"form-name": "waitlist-" + venture_id,
                            "email": secrets.token_hex(16) + "@qa.invalid", "consent": "yes", "consent_version": VERSION,
                            "consent_digest": hashlib.sha256(consent_text(name).encode()).hexdigest(), "is_test": "true"})
                submitted = metered.call_sync(send_test, purpose="site.publish", estimate_micro=0,
                    workspace_id=workspace_id, venture_id=venture_id)
                if submitted.status_code not in {200, 201, 303}:
                    raise ValueError("submission_unconfirmed")
            except (httpx.HTTPError, ValueError, OSError):
                raise WaitlistError("waitlist_local_form_unconfirmed", 503) from None
            return {**status(venture_id, workspace_id), "url": url, "destination_tested": True,
                "submission_verified": False, "note": "Your synthetic test was sent to your published Netlify form. It is not counted as a signup here. Open Netlify > Forms to confirm delivery, then import only real consented signups in Website > Collect signups."}
    token = secrets.token_urlsafe(32)
    email = f"{secrets.token_hex(16)}@qa.invalid"
    with db()() as session:
        serialize_writes(session, key=f"waitlist:{venture_id}")
        _venture(session, venture_id, workspace_id)
        row = session.get(VentureWaitlist, venture_id)
        if not row or row.workspace_id != workspace_id or not row.enabled:
            raise WaitlistError("waitlist_enable_required")
        if row.probe_expires_at and as_utc(row.probe_expires_at) > as_utc(utcnow()):
            raise WaitlistError("waitlist_probe_recent", 429)
        config_updated_at = row.updated_at
        row.probe_hash = hashlib.sha256(token.encode()).hexdigest()
        row.probe_expires_at = utcnow() + timedelta(seconds=60)
        row.verified_entry_id = row.verified_at = None
        session.commit()
    try:
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False) as client:
            response = client.post(public_url(venture_id), json={"email": email, "consent": "yes",
                "consent_version": VERSION, "test_token": token,
                "consent_digest": hashlib.sha256(public_details(venture_id)["consent"].encode()).hexdigest()}, headers={"Accept": "application/json"})
        if response.status_code != 201 or response.json().get("received") is not True:
            raise WaitlistError("waitlist_public_submission_unconfirmed", 503)
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, WaitlistError):
            raise
        raise WaitlistError("waitlist_public_submission_unconfirmed", 503) from None
    with db()() as session:
        serialize_writes(session, key=f"waitlist:{venture_id}")
        row = session.get(VentureWaitlist, venture_id)
        entry = session.scalar(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id,
            VentureWaitlistEntry.workspace_id == workspace_id, VentureWaitlistEntry.email == email,
            VentureWaitlistEntry.is_test.is_(True), VentureWaitlistEntry.consent_version == VERSION))
        if not row or not row.enabled or row.workspace_id != workspace_id or not entry or row.probe_hash or row.updated_at != config_updated_at:
            raise WaitlistError("waitlist_storage_receipt_missing", 503)
        row.verified_entry_id, row.verified_at = entry.id, utcnow()
        session.commit()
        from app.autopilot import receipts
        receipts.persisted_record(VentureWaitlist, venture_id, {"enabled": True, "verified_entry_id": entry.id})
    return status(venture_id, workspace_id)
