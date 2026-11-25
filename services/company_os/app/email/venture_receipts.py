"""Receipts for prepared venture exports and the existing recoverable archive.

Only successful server audit rows are sources. The existing hourly email pass
recovers temporary admission failures; canonical delivery owns all mail policy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.models import EventLog, Venture, Workspace, WorkspaceMember
from app.email import service
from app.state import db

log = logging.getLogger("app.email.venture_receipts")
TOPICS = {"venture.export_prepared": "venture_export_prepared", "venture.soft_deleted": "venture_archived"}


def _naive(value):
    from datetime import timezone

    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _context(session, event, person_id, template):
    if (event is None or TOPICS.get(event.topic) != template or event.outcome != "ok"
            or event.actor_kind != "person" or event.actor_id != person_id
            or event.actor != person_id or event.surface != "api"
            or not event.venture_id or event.target_ref != event.venture_id):
        return None
    venture = session.get(Venture, event.venture_id)
    workspace = session.get(Workspace, event.workspace_id) if event.workspace_id else None
    if (not venture or not workspace or venture.workspace_id != workspace.id or venture.stage == "purged"
            or not (workspace.owner_id == person_id or session.scalar(select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.person_id == person_id)))):
        return None
    if json.loads(workspace.settings_json or "{}").get("blocked"):
        return None
    now = service.utcnow()
    if not timedelta(0) <= now - _naive(event.ts) < timedelta(days=7):
        return None
    payload = json.loads(event.payload_json)
    if not isinstance(payload, dict) or payload.get("schema") != "venture_lifecycle_receipt_v1":
        return None
    context = {"_venture_receipt_id": event.id, "_venture_id": venture.id, "venture_name": venture.name}
    if template == "venture_export_prepared":
        if (venture.archived_at is not None or workspace.archived_at is not None
                or not isinstance(payload.get("export_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", payload["export_sha256"])):
            return None
        context.update(path=f"/v/{venture.id}/settings")
        ref = f"{venture.id}:{_naive(event.ts).date().isoformat()}:{payload['export_sha256']}"
    else:
        from app.ventures.lifecycle import RETENTION_DAYS

        archived = venture.archived_at
        if (archived is None or workspace.archived_at is None or _naive(workspace.archived_at) != _naive(archived)
                or payload.get("retention_days") != RETENTION_DAYS
                or _naive(datetime.fromisoformat(payload["archived_at"])) != _naive(archived)):
            return None
        deadline = _naive(archived) + timedelta(days=RETENTION_DAYS)
        if now >= deadline or _naive(datetime.fromisoformat(payload["purge_after"])) != deadline:
            return None
        context.update(path="/", restore_until=deadline.strftime("%d %b %Y at %H:%M UTC"))
        ref = f"{venture.id}:{_naive(archived).isoformat()}"
    return context, ref


def _queue(event_id):
    with db()() as session:
        event = session.scalar(select(EventLog).where(EventLog.id == event_id))
        template = TOPICS.get(event.topic) if event else None
        if not template or not service.template_enabled(template):
            return None
        found = _context(session, event, event.actor_id, template)
        person_id = event.actor_id
    if found:
        context, ref = found
        return service.send_email(person_id, template, context, ref=ref)
    return None


def on_receipt(event_id):
    """Mail failure never changes a completed export/archive response."""
    if event_id:
        try:
            return _queue(event_id)
        except Exception:
            log.warning("venture_receipt_queue_failed")
    return None


def on_export_prepared(venture_id, actor_id, pack):
    """Called only by the real download endpoint, after redacted pack preparation."""
    from app.audit.writer import audit
    from app.authz.workspace import subject_person_id

    try:
        event_id = audit("venture.export_prepared", venture_id=venture_id,
            workspace_id=pack["venture"]["workspace_id"], actor_id=subject_person_id(actor_id),
            actor_kind="person", surface="api", target_ref=venture_id,
            payload={"schema": "venture_lifecycle_receipt_v1",
                     "export_sha256": hashlib.sha256(json.dumps(pack, sort_keys=True).encode()).hexdigest()})
        return on_receipt(event_id)
    except Exception:
        log.warning("venture_export_receipt_failed")
        return None


def reconcile():
    """Recover source receipts using the existing outbox's admission/dedupe fences."""
    if not any(service.template_enabled(template) for template in TOPICS.values()):
        return
    now = service.utcnow()
    with db()() as session:
        ids = session.scalars(select(EventLog.id).where(EventLog.topic.in_(TOPICS),
            EventLog.ts > now - timedelta(days=7), EventLog.ts <= now).order_by(EventLog.seq)).all()
    for event_id in ids:
        on_receipt(event_id)


def refresh_context(session, person_id, template, context):
    event_id = context.get("_venture_receipt_id")
    if not isinstance(event_id, str):
        return None
    event = session.scalar(select(EventLog).where(EventLog.id == event_id))
    found = _context(session, event, person_id, template)
    if not found or context.get("_venture_id") != event.venture_id:
        return None
    fresh, _ = found
    if "_email_delivery_payload" in context:
        fresh.pop("venture_name")  # The canonical retry keeps its prepared copy.
    return {**context, **fresh}
