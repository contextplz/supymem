"""Observe-ladder descent (38 §5).

Dropping the level must cancel in-flight work that needed the higher one. A
ladder you cannot descend under load is not a control.
"""

from __future__ import annotations

from sqlalchemy import select

from app.authz.effective import TARGET_MIN_OBSERVE, observe_rank
from app.db.models import Engagement, Workflow
from app.state import db
from core.clock import utcnow

# Engagement target classes, by how much access carrying them out needs.
ENGAGEMENT_TARGET_CLASS = "connection:write"
IN_FLIGHT = ("triaging", "awaiting_goahead", "planned", "working", "in_review")


def cancel_work_above_level(workspace_id: str, level: str) -> list[str]:
    """Block engagements and pause workflows that the new level no longer allows."""
    needed = TARGET_MIN_OBSERVE.get(ENGAGEMENT_TARGET_CLASS, "O4")
    if observe_rank(level) >= observe_rank(needed):
        return []

    cancelled: list[str] = []
    factory = db()
    reason = f"observe level dropped to {level}"
    with factory() as session:
        rows = session.scalars(
            select(Engagement).where(
                Engagement.workspace_id == workspace_id,
                Engagement.status.in_(IN_FLIGHT),
                Engagement.target_ref.is_not(None),
            )
        ).all()
        for row in rows:
            row.status = "blocked"
            row.blocked_reason = reason
            row.updated_at = utcnow()
            cancelled.append(row.id)

        flows = session.scalars(
            select(Workflow).where(
                Workflow.workspace_id == workspace_id, Workflow.status == "active"
            )
        ).all()
        for flow in flows:
            if _writes_externally(flow):
                flow.status = "paused"
                flow.paused_reason = reason
                flow.updated_at = utcnow()
                cancelled.append(flow.id)
        session.commit()
    return cancelled


def _writes_externally(flow: Workflow) -> bool:
    import json

    try:
        steps = json.loads(flow.steps_json or "[]")
    except json.JSONDecodeError:
        return False
    return any(
        (step.get("action") or {}).get("kind") == "connector_write" for step in steps
    )
