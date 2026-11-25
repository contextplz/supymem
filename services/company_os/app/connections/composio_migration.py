"""Durable reconnect state without moving queues or mailbox history."""

from __future__ import annotations

import json
import os
import re

from sqlalchemy import select

from app.db.models import ChatMessage, ContentAccount, GtmEmailAccount, GtmEmailEvent, GtmEmailSendQueue, Job, Venture, VentureDocument, Workspace
from app.db.session import serialize_writes
from app.state import db
from core.clock import utcnow
from core.ids import new_id

GMAIL_RECONNECT_REASON = "gmail_reconnect_required"
GMAIL_RECONNECT_TITLE = "Reconnect your Gmail to keep your emails going"


def missing_connection(exc: Exception) -> bool:
    text = str(exc).replace("_", " ").replace("-", " ")
    if re.search(r"(?:connected[_ ]?account|connection)\b.*(?:not[_ ]found|invalid|does not exist)|invalid.*connected[_ ]?account", text, re.I):
        return True
    return getattr(exc, "status_code", None) == 404 and not re.search(r"history|message|thread|tool.*not found", text, re.I)


def gmail_notice(session, account, *, reconnected: bool = False) -> None:
    from app.connections.mailboxes import enabled
    multi = enabled()
    owner = session.scalar(select(Workspace.owner_id).where(Workspace.id == account.workspace_id))
    if not owner:
        return
    project = os.environ.get("COMPOSIO_PROJECT", "v1").strip() or "v1"
    key = f"gmail-reconnect:{owner}:{project}:{account.connected_account_id}:{account.updated_at.isoformat()}"
    if session.get_bind().dialect.name != "sqlite" or not session.connection().connection.driver_connection.in_transaction:
        serialize_writes(session, key=key)
    if reconnected:
        for card in session.scalars(select(ChatMessage).where(ChatMessage.venture_id.in_(
                select(Venture.id).where(Venture.workspace_id.in_(select(Workspace.id).where(Workspace.owner_id == owner)))),
                ChatMessage.kind == "needs_you", ChatMessage.body_md.contains(GMAIL_RECONNECT_TITLE))):
            if multi and not any((action.get("payload") or {}).get("mailbox_id") == account.id
                                 for action in json.loads(card.actions_json or "[]")):
                continue
            card.kind, card.body_md, card.actions_json = "gmail_reconnect_resolved", "Your Gmail is reconnected. Your saved emails can resume.", "[]"
        notice = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == account.id,
            GtmEmailEvent.event_type == "gmail_reconnect_notice", ~GtmEmailEvent.idempotency_key.endswith(":resumed"))
            .order_by(GtmEmailEvent.created_at.desc(), GtmEmailEvent.id.desc()).limit(1))
        if notice is None:
            return
        key = notice.idempotency_key + ":resumed"
    if session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == key)):
        return
    venture = session.scalar(select(Venture).where(Venture.workspace_id.in_(
        select(Workspace.id).where(Workspace.owner_id == owner)), Venture.archived_at.is_(None))
        .order_by(Venture.created_at, Venture.id).limit(1))
    if venture is None:
        return
    from app.chat.milestones import post_milestone
    body = ("Your Gmail is reconnected. Your queued emails and follow-ups resume automatically under your existing approvals and limits."
            if reconnected else GMAIL_RECONNECT_TITLE + ". Your queued emails and follow-ups stay saved while sending and reply checks are paused.")
    if multi:
        body = (f"Your Gmail inbox {account.email or ''} is reconnected. Your saved emails for this inbox can resume."
                if reconnected else GMAIL_RECONNECT_TITLE + f" ({account.email or ''}). Your saved emails for this inbox stay paused.")
    actions = None if reconnected else [{"label": "Reconnect", "action": "connections.connect",
        "payload": {"query": f"reconnect gmail {account.email or ''} inbox {account.id}" if multi else "connect gmail",
                    **({"mailbox_id": account.id} if multi else {})}, "style": "primary"}]
    if post_milestone(venture.id, key=key, kind="gmail_reconnected" if reconnected else "needs_you",
                      body_md=body, actions=actions, requested=True, session=session):
        session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=account.workspace_id,
            email_account_id=account.id, event_type="gmail_reconnect_notice", idempotency_key=key, created_at=utcnow()))


def mark_gmail_reconnect(connected_account_id: str) -> None:
    from app.connections.mailboxes import enabled
    with db()() as session:
        serialize_writes(session, key=f"gmail-reconnect:{connected_account_id}")
        account = session.scalar(select(GtmEmailAccount).where(
            GtmEmailAccount.connected_account_id == connected_account_id).with_for_update())
        if account is None or (enabled() and account.status == "revoked"):
            return
        first_notice = account.status != "needs_reconnect"
        account.status, account.updated_at = "needs_reconnect", utcnow()
        for job in session.scalars(select(GtmEmailSendQueue).where(
                GtmEmailSendQueue.email_account_id == account.id, GtmEmailSendQueue.status == "queued")):
            job.last_error = GMAIL_RECONNECT_REASON
        workspaces = select(Workspace.id).where(Workspace.owner_id == session.scalar(
            select(Workspace.owner_id).where(Workspace.id == account.workspace_id)))
        for document in session.scalars(select(VentureDocument).where(VentureDocument.kind == "outreach_draft",
                VentureDocument.venture_id.in_(select(Venture.id).where(Venture.workspace_id.in_(workspaces))))):
            draft = json.loads(document.provenance_json or "{}")
            if enabled() and _draft_mailbox_id(draft) != account.id:
                continue
            if draft.get("channel") == "email" and draft.get("state") == "draft":
                draft["error"] = GMAIL_RECONNECT_REASON
                document.provenance_json = json.dumps(draft)
                scheduled = draft.get("scheduled_send") or {}
                job = session.get(Job, scheduled.get("job_id")) if scheduled.get("job_id") else None
                if job and job.venture_id == document.venture_id and job.status in {"pending", "scheduled", "retry_wait"}:
                    job.status, job.last_error = "paused", GMAIL_RECONNECT_REASON
        if first_notice:
            gmail_notice(session, account)
        session.commit()


def resume_gmail_work(session, account) -> None:
    from app.connections.mailboxes import enabled
    multi = enabled()
    workspaces = select(Workspace.id).where(Workspace.owner_id == session.scalar(
        select(Workspace.owner_id).where(Workspace.id == account.workspace_id)))
    for job in ([] if multi else session.scalars(select(Job).where(Job.workspace_id.in_(workspaces),
            Job.status == "paused", Job.last_error == GMAIL_RECONNECT_REASON))):
        job.status, job.last_error = "scheduled" if job.run_after else "pending", None
        job.locked_at, job.locked_by = None, None
    for document in session.scalars(select(VentureDocument).where(VentureDocument.kind == "outreach_draft",
            VentureDocument.venture_id.in_(select(Venture.id).where(Venture.workspace_id.in_(workspaces))))):
        draft = json.loads(document.provenance_json or "{}")
        if multi and _draft_mailbox_id(draft) != account.id:
            continue
        if multi:
            scheduled = draft.get("scheduled_send") or {}
            job = session.get(Job, scheduled.get("job_id")) if scheduled.get("job_id") else None
            if job and job.venture_id == document.venture_id and job.status == "paused" and job.last_error == GMAIL_RECONNECT_REASON:
                job.status, job.last_error = "scheduled" if job.run_after else "pending", None
                job.locked_at, job.locked_by = None, None
        if draft.get("error") == GMAIL_RECONNECT_REASON and draft.get("state") == "draft":
            draft.pop("error")
            document.provenance_json = json.dumps(draft)


def _draft_mailbox_id(draft: dict) -> str | None:
    return draft.get("mailbox_id") or draft.get("email_account_id") or (draft.get("sender") or {}).get("mailbox_id")


def reconcile_gmail_project(workspace_id: str) -> None:
    project = os.environ.get("COMPOSIO_PROJECT", "").strip()
    if not project:
        return
    from app.gtm.supymem.pipeline import _mailboxes_query
    with db()() as session:
        stale = []
        for account in session.scalars(_mailboxes_query(session, workspace_id).where(GtmEmailAccount.status.in_(["active", "expired"]))):
            if str(account.connected_account_id or "").startswith("sim_"):
                continue
            marker = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == account.id,
                GtmEmailEvent.event_type == "composio_project").order_by(GtmEmailEvent.created_at.desc(), GtmEmailEvent.id.desc()).limit(1))
            saved = json.loads(marker.metadata_json or "{}").get("project") if marker else "v1"
            if saved != project or account.status == "expired":
                stale.append(account.connected_account_id)
    for account_id in stale:
        mark_gmail_reconnect(account_id)


def mark_social_reconnect(connected_account_id: str) -> None:
    with db()() as session:
        for account in session.scalars(select(ContentAccount).where(ContentAccount.status != "revoked")):
            if json.loads(account.extra_json or "{}").get("connected_account_id") == connected_account_id:
                account.status, account.health = "needs_reconnect", "down"
                account.paused_reason, account.last_error = "composio_reconnect_required", None
                account.updated_at = utcnow()
        session.commit()
