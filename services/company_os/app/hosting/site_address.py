"""The founder's choice of the prefix in <prefix>.supymem.com, checked as they type and applied race-safely.

Hosting cannot redirect (a host maps to one immutable deployment in the CloudFront KeyValueStore, with no redirect
entry), so a retired address becomes an *alias*: an ``AppDomain`` row of kind ``supymem_alias`` that every later
publication re-points at the same deployment as the new primary host, and that unpublish takes offline together with it.
A live URL is therefore never broken, and a takedown never leaves an old host serving.
"""

from __future__ import annotations

import re
from typing import Any

from core.hosting_contracts import (
    RESERVED_PREFIXES,
    RESERVED_SLUGS,
    HostingRejected,
    public_hostname,
    valid_slug,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.writer import audit
from app.credits._txn import naive_utc
from app.db.session import serialize_writes
from app.db.supymem_models import AppDomain, Deployment, FounderApp
from app.hosting.firstlook_publish import (
    _BUSY,
    BUILD_MODE,
    _ensure_app,
    slug_for,
)
from app.hosting.service import HostingConfig, HostingError
from app.state import db

ALIAS = "supymem_alias"
MIN_LEN, MAX_LEN = 3, 48
REASONS = {
    "taken": "That address is already taken.",
    "reserved": "That address is kept for Supymem itself.",
    "invalid": "Use only letters, numbers and single dashes, and start and end with a letter or number.",
    "too_short": f"Use at least {MIN_LEN} characters.",
    "too_long": f"Use at most {MAX_LEN} characters.",
}


def normalize(raw: str) -> str:
    return re.sub(r"[\s_]+", "-", (raw or "").strip().lower())


def classify(slug: str) -> str | None:
    """Reason a normalized address can never be used, else None (availability is checked separately)."""
    if len(slug) < MIN_LEN:
        return "too_short"
    if len(slug) > MAX_LEN:
        return "too_long"
    if slug in RESERVED_SLUGS or slug.startswith(RESERVED_PREFIXES):
        return "reserved"
    try:
        valid_slug(slug)
    except HostingRejected:
        return "invalid"
    return "invalid" if "--" in slug else None


def _taken(session: Session, slug: str, app_id: str | None) -> bool:
    other = session.scalar(select(FounderApp.id).where(FounderApp.slug == slug))
    if other is not None and other != app_id:
        return True
    alias = session.scalar(select(AppDomain.app_id).where(AppDomain.hostname.like(slug + ".%")))
    return alias is not None and alias != app_id


def _own_app(session: Session, venture_id: str, target: str = "firstlook") -> FounderApp | None:
    if target == "pagebuilder":  # the page builder publishes the venture's one app (same pick as its analytics binding)
        from app.product_analytics.target_app import select_venture_app

        return select_venture_app(session, venture_id)
    return session.scalar(select(FounderApp).where(
        FounderApp.venture_id == venture_id, FounderApp.build_mode == BUILD_MODE, FounderApp.deleted_at.is_(None)))


def _suggest(session: Session, venture_id: str, slug: str, reason: str | None) -> str:
    from app.db.models import Venture
    from app.infra.apps import free_slug

    if reason in (None, "taken"):
        for candidate in (slug, f"{slug}-site"):
            candidate = candidate[:MAX_LEN].strip("-")
            if not classify(candidate):
                found = free_slug(candidate, session=session)
                if not classify(found):
                    return found
    venture = session.get(Venture, venture_id)
    return slug_for(venture.name if venture else "", session=session)


def check(venture_id: str, raw: str, target: str = "firstlook") -> dict[str, Any]:
    slug = normalize(raw)
    reason = classify(slug)
    with db()() as session:
        app = _own_app(session, venture_id, target)
        if reason is None and _taken(session, slug, app.id if app else None):
            reason = "taken"
        out: dict[str, Any] = {"available": reason is None, "normalized": slug, "reason": reason,
                               "current": app.slug if app else None, "message": REASONS.get(reason) if reason else None, "suggestion": None}
        if reason:
            out["suggestion"] = _suggest(session, venture_id, slug, reason)
        return out


def set_address(venture_id: str, workspace_id: str, actor: str, raw: str, target: str = "firstlook") -> dict[str, Any]:
    """Set the venture's page address if it is free. Raises HostingError(422/409) carrying a suggestion."""
    slug = normalize(raw)
    reason = classify(slug)
    if reason:
        raise _rejected(venture_id, reason, slug, 422)
    config = HostingConfig.from_environment()
    app_id = _app_id(venture_id, workspace_id, actor, target)
    new_host = public_hostname(slug, config.public_domain)
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app = session.get(FounderApp, app_id)
        old_slug = app.slug
        if old_slug == slug:
            return {"slug": slug, "hostname": new_host, "changed": False, "live": False}
        rows = session.scalars(select(Deployment).where(Deployment.app_id == app_id)).all()
        if any(r.state in _BUSY or r.error == "hosting_public_state_uncertain" for r in rows):
            raise HostingError("hosting_publication_in_progress", status=409)
        live = any(r.state == "live" for r in rows)
        if _taken(session, slug, app_id):
            raise _rejected(venture_id, "taken", slug, 409, session=session)
        try:
            with session.begin_nested():
                primary = session.scalar(select(AppDomain).where(AppDomain.app_id == app_id, AppDomain.kind == "supymem"))
                if primary is not None:
                    if primary.active_at is None:
                        session.delete(primary)  # never served: nothing to keep
                    else:
                        primary.kind = ALIAS  # served once: keeps serving the current page
                    session.flush()
                back = session.scalar(select(AppDomain).where(AppDomain.app_id == app_id, AppDomain.hostname == new_host))
                if back is not None:
                    back.kind = "supymem"  # going back to a retired address
                app.slug, app.updated_at = slug, naive_utc()
                session.flush()
        except IntegrityError as exc:  # the unique constraint on apps.slug decides a race
            session.rollback()
            raise _rejected(venture_id, "taken", slug, 409, session=session) from exc
        session.commit()
        old_host = public_hostname(old_slug, config.public_domain)
        workspace = app.workspace_id
    audit("hosting.site_address_changed", workspace_id=workspace, venture_id=venture_id, actor_id=actor,
          actor_kind="person", surface="api", target_ref=app_id,
          payload={"from": old_host, "to": new_host, "live": live, "old_address_keeps_working": live})
    return {"slug": slug, "hostname": new_host, "changed": True, "live": live, "previous_hostname": old_host}


def _app_id(venture_id: str, workspace_id: str, actor: str, target: str) -> str:
    """Authorize owner/admin and find (first-look: create) the app whose address is being chosen."""
    if target != "pagebuilder":
        return _ensure_app(venture_id, workspace_id, actor)
    from app.builder.access import authorize

    with db()() as session:
        try:
            authorize(session, workspace_id=workspace_id, venture_id=venture_id, actor=actor, mutate=True)
        except KeyError as exc:
            raise HostingError("hosting_app_not_found", status=404) from exc
        except PermissionError as exc:
            raise HostingError(str(exc), status=403) from exc
        app = _own_app(session, venture_id, target)
        if app is None:
            raise HostingError("site_address_page_required", status=409)
        return app.id


def _rejected(venture_id: str, reason: str, slug: str, status: int, *, session: Session | None = None) -> HostingError:
    err = HostingError(f"site_address_{reason}", status=status)
    if session is not None:
        err.suggestion = _suggest(session, venture_id, slug, reason)
    else:
        with db()() as fresh:
            err.suggestion = _suggest(fresh, venture_id, slug, reason)
    return err


def aliases(app_id: str, session: Session | None = None) -> list[str]:
    def read(s: Session) -> list[str]:
        return list(s.scalars(select(AppDomain.hostname).where(AppDomain.app_id == app_id, AppDomain.kind == ALIAS)))
    if session is not None:
        return read(session)
    with db()() as s:
        return read(s)
