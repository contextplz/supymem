"""Durable, workspace-scoped receipt for a read-only Gmail profile check."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.audit.writer import audit
from app.db.models import EventLog, Venture
from app.gtm.supymem.pipeline import verify_gmail_connection
from app.state import db

TOPIC = "connections.gmail.profile_checked"
REQUEST = "GMAIL_GET_PROFILE"
STATUSES = frozenset({"verified", "not_connected", "provider_error", "unconfirmed", "account_mismatch"})


def _validated_venture(workspace_id: str, venture_id: str | None) -> str | None:
    if venture_id is None:
        return None
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None or venture.workspace_id != workspace_id or venture.archived_at is not None:
            raise ValueError("venture_unavailable")
    return venture_id


def verify(workspace_id: str, *, actor_id: str, venture_id: str | None = None, mailbox_id: str | None = None) -> dict[str, Any]:
    venture_id = _validated_venture(workspace_id, venture_id)
    from app.connections.mailboxes import _owned_account, enabled, resolve_send_mailbox
    multi = enabled()
    if multi:
        with db()() as session:
            if mailbox_id is not None:
                mailbox_id = _owned_account(session, workspace_id, mailbox_id).id
            else:
                mailbox_id = resolve_send_mailbox(session, workspace_id, venture_id=venture_id)["mailbox_id"]
            session.commit()
    proof = verify_gmail_connection(workspace_id, mailbox_id=mailbox_id) if multi else verify_gmail_connection(workspace_id)
    status = proof["status"] if proof.get("status") in STATUSES else "unconfirmed"
    payload = {
        "request": REQUEST,
        "status": status,
        "account_id": proof.get("account_id"),
        "log_id": proof.get("log_id"),
        **({"mailbox_id": mailbox_id} if multi else {}),
    }
    receipt_id = audit(
        TOPIC,
        workspace_id=workspace_id,
        venture_id=venture_id,
        actor_id=actor_id,
        actor_kind="person",
        surface="api",
        target_ref=payload["account_id"],
        outcome="ok" if status == "verified" else "error",
        payload=payload,
    )
    if multi:
        proof = {**proof, "account_id": mailbox_id}
    return {**proof, "receipt_id": receipt_id, "receipt_saved": receipt_id is not None}


def latest(workspace_id: str, *, venture_id: str | None = None, mailbox_id: str | None = None) -> dict[str, Any]:
    venture_id = _validated_venture(workspace_id, venture_id)
    with db()() as session:
        from app.connections.mailboxes import enabled
        multi = enabled()
        if multi and mailbox_id is not None:
            from app.gtm.supymem.pipeline import _mailboxes_query
            from app.db.models import GtmEmailAccount
            if session.scalar(_mailboxes_query(session, workspace_id).where(GtmEmailAccount.id == mailbox_id)) is None:
                raise ValueError("mailbox_unavailable")
        rows = session.scalars(
            select(EventLog)
            .where(EventLog.workspace_id == workspace_id, EventLog.venture_id == venture_id, EventLog.topic == TOPIC)
            .order_by(EventLog.seq.desc())
            .limit(None if multi else 1)
        )
        row = next((row for row in rows if not multi or mailbox_id is None
                    or json.loads(row.payload_json or "{}").get("mailbox_id") == mailbox_id), None)
        if row is None:
            return {"receipt": None}
        payload = json.loads(row.payload_json or "{}")
        status = payload.get("status")
        return {"receipt": {
            "receipt_id": row.id,
            "checked_at": row.ts.isoformat() if row.ts else None,
            "request": REQUEST,
            "venture_id": venture_id,
            "status": status if status in STATUSES else "unconfirmed",
            "account_id": payload.get("mailbox_id") if multi else payload.get("account_id"),
            "log_id": payload.get("log_id"),
            "mail_sent": False,
        }}
