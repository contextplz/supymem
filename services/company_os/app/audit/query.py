"""Read side of the audit log (42 §5): filterable, paginated, plain language."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import EventLog
from app.state import db

MAX_LIMIT = 200

# Topic -> a sentence a founder can read without knowing the schema.
PHRASES: dict[str, str] = {
    "workspace.created": "created this workspace",
    "workspace.renamed": "renamed this workspace",
    "governance.autonomy.changed": "changed how much this system may do on its own",
    "governance.observe.changed": "changed how much of your systems this can see",
    "governance.denied": "was refused an action",
    "approval.requested": "asked for a decision",
    "approval.approved": "approved a request",
    "approval.rejected": "rejected a request",
    "approval.expired": "let a request expire without deciding",
    "invite.created": "invited someone",
    "invite.accepted": "joined this workspace",
    "member.role_changed": "changed someone's role",
    "member.removed": "removed someone",
}


def describe(row: EventLog) -> str:
    who = row.actor_id or row.actor or "someone"
    what = PHRASES.get(row.topic, row.topic.replace(".", " "))
    line = f"{who} {what}"
    if row.outcome == "denied" and row.denied_reason:
        line += f", refused: {row.denied_reason}"
    return line


def list_audit(
    workspace_id: str,
    *,
    actor_id: str | None = None,
    topic: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    before_seq: int | None = None,
) -> dict[str, Any]:
    limit = max(1, min(limit, MAX_LIMIT))
    factory = db()
    with factory() as session:
        stmt = select(EventLog).where(EventLog.workspace_id == workspace_id)
        if actor_id:
            stmt = stmt.where(EventLog.actor_id == actor_id)
        if topic:
            stmt = stmt.where(EventLog.topic == topic)
        if outcome:
            stmt = stmt.where(EventLog.outcome == outcome)
        if before_seq is not None:
            stmt = stmt.where(EventLog.seq < before_seq)
        rows = session.scalars(
            stmt.order_by(EventLog.seq.desc()).limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "seq": r.seq,
                "topic": r.topic,
                "line": describe(r),
                "actor_id": r.actor_id or r.actor,
                "actor_kind": r.actor_kind,
                "on_behalf_of": r.on_behalf_of,
                "surface": r.surface,
                "target_ref": r.target_ref,
                "approval_id": r.approval_id,
                "outcome": r.outcome,
                "denied_reason": r.denied_reason,
                "payload": r.payload_json,
                "ts": r.ts.isoformat() if r.ts else None,
            }
            for r in rows
        ]
    return {
        "items": items,
        "has_more": has_more,
        "next_before_seq": items[-1]["seq"] if items and has_more else None,
    }
