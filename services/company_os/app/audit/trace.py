"""Walk any artifact back to a human intent (42 §5).

"Why does this file exist?" is the first question anyone asks of an AI-written
codebase. One query answers it:

    deliverable -> engagement -> request_verbatim -> person
                -> approval(s) -> decided_by
                -> agent runs -> model calls (cost, tokens, purpose)
                -> connection_events (what we touched in their systems)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db.models import (
    AgentRun,
    Approval,
    ConnectionEvent,
    Engagement,
    EventLog,
    Person,
    TokenLedger,
)
from app.state import db


@dataclass
class Trace:
    ref: str
    found: bool = False
    engagement: dict[str, Any] | None = None
    requested_by: dict[str, Any] | None = None
    approvals: list[dict[str, Any]] = field(default_factory=list)
    agent_runs: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    connection_events: list[dict[str, Any]] = field(default_factory=list)
    audit_rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "found": self.found,
            "engagement": self.engagement,
            "requested_by": self.requested_by,
            "approvals": self.approvals,
            "agent_runs": self.agent_runs,
            "model_calls": self.model_calls,
            "connection_events": self.connection_events,
            "audit": self.audit_rows,
            "cost_usd": round(
                sum(c.get("cost_usd") or 0.0 for c in self.model_calls), 6
            ),
        }


def trace(ref: str) -> Trace:
    """Resolve ``ref`` — an engagement id, or a deliverable path inside one."""
    result = Trace(ref=ref)
    factory = db()
    with factory() as session:
        engagement = _find_engagement(session, ref)
        if engagement is None:
            # Still return whatever the audit log knows, so the answer is never
            # an empty page for a real artifact.
            result.audit_rows = _audit_for(session, ref)
            result.found = bool(result.audit_rows)
            return result

        result.found = True
        result.engagement = {
            "id": engagement.id,
            "workspace_id": engagement.workspace_id,
            "title": engagement.title,
            "size": engagement.size,
            "status": engagement.status,
            "request_verbatim": engagement.request_verbatim,
            "definition_of_done": engagement.definition_of_done,
            "deliverables": _loads(engagement.deliverables_json, []),
            "created_at": _iso(engagement.created_at),
            "delivered_at": _iso(engagement.delivered_at),
        }

        person = session.get(Person, engagement.requested_by)
        result.requested_by = {
            "id": engagement.requested_by,
            "display_name": person.display_name if person else engagement.requested_by,
        }

        result.approvals = [
            {
                "id": a.id,
                "kind": a.kind,
                "risk": a.risk,
                "status": a.status,
                "decided_by": a.decided_by,
                "decided_at": _iso(a.decided_at),
                "summary": a.summary,
            }
            for a in session.scalars(
                select(Approval)
                .where(Approval.subject_ref == engagement.id)
                .order_by(Approval.created_at)
            ).all()
        ]

        result.audit_rows = _audit_for(session, engagement.id)

        result.agent_runs = [
            {
                "id": r.id,
                "agent_id": r.agent_id,
                "spec_version": r.spec_version,
                "goal": r.goal,
                "status": r.status,
                "cost_usd": r.cost_usd,
                "started_at": _iso(r.started_at),
            }
            for r in session.scalars(
                select(AgentRun)
                .where(AgentRun.venture_id == engagement.workspace_id)
                .order_by(AgentRun.started_at)
            ).all()
        ]

        known_runs = {r["id"] for r in result.agent_runs}
        result.model_calls = [
            {
                "id": t.id,
                "run_id": t.run_id,
                "purpose": t.purpose,
                "tier": t.tier,
                "tokens_in": t.tokens_in,
                "tokens_out": t.tokens_out,
                "cost_usd": t.cost_usd,
                "created_at": _iso(t.created_at),
            }
            for t in session.scalars(
                select(TokenLedger).where(
                    TokenLedger.venture_id == engagement.workspace_id
                )
            ).all()
            if t.run_id is None or t.run_id in known_runs
        ]

        result.connection_events = [
            {
                "id": e.id,
                "connection_id": e.connection_id,
                "direction": e.direction,
                "operation": e.operation,
                "target": e.target,
                "outcome": e.outcome,
                "simulated": bool(e.simulated),
                "at": _iso(e.at),
            }
            for e in session.scalars(
                select(ConnectionEvent)
                .where(ConnectionEvent.engagement_id == engagement.id)
                .order_by(ConnectionEvent.at)
            ).all()
        ]
    return result


def _find_engagement(session: Any, ref: str) -> Engagement | None:
    if ref.startswith("eng_"):
        return session.get(Engagement, ref)
    # A deliverable path: find the engagement that claims it.
    for row in session.scalars(select(Engagement)).all():
        for item in _loads(row.deliverables_json, []):
            path = item.get("path") if isinstance(item, dict) else item
            if path == ref:
                return row
    return None


def _audit_for(session: Any, ref: str) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(EventLog).where(EventLog.target_ref == ref).order_by(EventLog.seq)
    ).all()
    return [
        {
            "seq": r.seq,
            "topic": r.topic,
            "actor_id": r.actor_id or r.actor,
            "actor_kind": r.actor_kind,
            "on_behalf_of": r.on_behalf_of,
            "surface": r.surface,
            "approval_id": r.approval_id,
            "outcome": r.outcome,
            "denied_reason": r.denied_reason,
            "ts": _iso(r.ts),
        }
        for r in rows
    ]


def _loads(blob: str | None, default: Any) -> Any:
    try:
        return json.loads(blob or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None
