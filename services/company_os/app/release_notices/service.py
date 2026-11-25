"""Hourly reconciliation and the canonical delivery eligibility seam.

Root integration: register build_template as product_updates; call reconcile
from existing email.schedule. For app_release only, deliver must call this
refresh_context before rendering. ReleaseUnavailable defers the existing outbox
for an hour; None suppresses confirmed changed/optional/ineligible source.
Do not put that exception inside triggers.refresh_context's broad None wrapper.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import AnalyticsEvent, Person
from app.db.supymem_models import DesktopDevice, EmailMessage
from app.email import service as email, templates
from app.release_notices import publication as source
from app.state import db

TEMPLATE = "app_release"
EVENT = "desktop.release_artifact_verified"
log = logging.getLogger("app.release_notices")


def _enabled():
    return (os.environ.get("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "").strip().lower() in {"1", "true", "on", "yes"}
            and TEMPLATE in {v.strip() for v in os.environ.get("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", "").split(",")}
            and TEMPLATE in templates.TEMPLATES and templates.category_of(TEMPLATE) == "product_updates"
            and email.template_enabled(TEMPLATE))


def _saved(session, publication):
    event = session.get(AnalyticsEvent, publication.witness_id)
    if event is None:
        return None
    if event.event_name != EVENT or any(value is not None for value in (event.person_id, event.workspace_id, event.venture_id)):
        raise source.ReleaseUnavailable("release_witness_scope_invalid")
    try:
        value = json.loads(event.props_json)
    except (ValueError, TypeError):
        raise source.ReleaseUnavailable("release_witness_invalid") from None
    source.verify_witness(publication, value)
    return value


def _current(session, *, verify_new):
    deadline = time.monotonic() + source.FETCH_SECONDS
    with source.client() as client:
        publication = source.fetch_manifest(client, deadline)
        if not publication.mandatory and publication.min_version is None:
            return None  # Ordinary updates stay off, including security-looking prose.
        if _saved(session, publication) is None:
            if not verify_new:
                raise source.ReleaseUnavailable("release_witness_missing")
            value = source.verify_artifact(client, publication, deadline)
            session.add(AnalyticsEvent(id=publication.witness_id, event_name=EVENT, props_json=json.dumps(value, sort_keys=True),
                                       occurred_at=email.utcnow()))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if _saved(session, publication) != value:
                    raise source.ReleaseUnavailable("release_witness_conflict") from None
        return publication


def _eligible(session, person_id, publication):
    person = session.get(Person, person_id)
    if person is None or not person.email:
        return False
    devices = session.scalars(select(DesktopDevice).where(DesktopDevice.person_id == person_id,
        DesktopDevice.platform == "macos", DesktopDevice.revoked_at.is_(None)))
    return any(publication.required_for(device.app_version) for device in devices)


def reconcile():
    """No caller-selected recipient/source. Queue through canonical email only."""
    result = {"status": "disabled", "queued": 0}
    if not _enabled():
        return result
    try:
        with db()() as session:
            publication = _current(session, verify_new=True)
            if publication is None:
                return {**result, "status": "optional"}
            people = session.scalars(select(Person.id).join(DesktopDevice, DesktopDevice.person_id == Person.id)
                .where(Person.email.is_not(None), DesktopDevice.platform == "macos", DesktopDevice.revoked_at.is_(None))
                .distinct().order_by(Person.id)).all()
        result["status"] = "verified"
        for person_id in people:
            with db()() as session:
                if not _enabled():
                    return {**result, "status": "disabled"}
                if not _eligible(session, person_id, publication):
                    continue
                if session.scalar(select(EmailMessage.id).where(
                        EmailMessage.idempotency_key == email._key(person_id, TEMPLATE, publication.version))):
                    continue
            context = {"version": publication.version, "_release_witness": publication.witness_id,
                       "_release_manifest_sha256": publication.manifest_sha256}
            message_id = email.send_email(person_id, TEMPLATE, context, ref=publication.version)
            if message_id:
                with db()() as session:
                    row = session.get(EmailMessage, message_id)
                    result["queued"] += int(row is not None and row.status == "queued")
        return result
    except source.ReleaseUnavailable:
        log.warning("release_notice_source_unavailable")
        return {**result, "status": "unavailable"}


def refresh_context(session, person_id, context):
    """Fresh policy/device custody. Uncertain source raises; changed source returns None.

The server-only witness was generated from the same streamed bytes used for
both hashes and the signature check; public analytics input cannot select its ID
or supply an unscoped event. A cache hit still verifies the current signature.
"""
    if not _enabled():
        raise source.ReleaseUnavailable("release_notice_disabled")
    if (not isinstance(context, dict) or not isinstance(context.get("version"), str)
            or not source.VERSION.fullmatch(context["version"])
            or not isinstance(context.get("_release_witness"), str)
            or not re.fullmatch(r"ane_release_[a-f0-9]{64}", context["_release_witness"])
            or not isinstance(context.get("_release_manifest_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", context["_release_manifest_sha256"])):
        return None
    publication = _current(session, verify_new=False)
    if (publication is None or context.get("version") != publication.version
            or context.get("_release_witness") != publication.witness_id
            or not _eligible(session, person_id, publication)):
        return None
    # No policy prose enters copy. Frozen provider payload stays under canonical custody.
    return {**context, "_release_manifest_sha256": publication.manifest_sha256}


def build_template(context):
    """One canonical layout/CTA; source notes and caller URLs never become copy."""
    source.version_tuple(context.get("version"))
    version = context["version"]
    result = templates._note(context, f"Required Mac update {version}",
                             f"Supymem {version} is a required update for your Mac.", "Update Supymem",
                             note="Use Update now in Supymem, or download the current Mac app.", path="/")
    return {**result, "url": source.DOWNLOAD_URL}
