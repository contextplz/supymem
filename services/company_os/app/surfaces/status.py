"""Workspace status summaries for the public /api/v1 surface (doc 41).

Thin aggregation over engagements, activity, and cost — no business rules.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import ActivityLine, TokenLedger, Venture
from app.state import db


def workspace_status(workspace_id: str) -> dict[str, Any]:
    from app.engagements import service as eng
    from app.workspaces.service import get_workspace

    ws = get_workspace(workspace_id)
    if ws is None:
        return {"workspace_id": workspace_id, "found": False}

    open_items = eng.list_engagements(workspace_id, status="open")
    blockers = [e.to_dict() for e in open_items if e.status == "blocked"]
    next_up = [
        e.to_dict()
        for e in open_items
        if e.status not in {"blocked", "delivered", "failed", "abandoned"}
    ][:5]

    return {
        "workspace_id": workspace_id,
        "found": True,
        "kind": ws.kind,
        "name": ws.name,
        "autonomy_level": ws.autonomy_level,
        "observe_level": ws.observe_level,
        "open_engagements": len(open_items),
        "blockers": blockers,
        "next_up": next_up,
        "cost_usd": _cost_usd(workspace_id),
    }


def workspace_activity(
    workspace_id: str, *, limit: int = 50, cursor: int | None = None
) -> dict[str, Any]:
    """Cursor is a simple offset for now (stable enough for v1 clients)."""
    factory = db()
    with factory() as session:
        q = (
            select(ActivityLine)
            .where(ActivityLine.workspace_id == workspace_id)
            .order_by(ActivityLine.ts.desc())
        )
        rows = session.scalars(q).all()
        start = cursor or 0
        page = rows[start : start + limit]
        items = [
            {
                "id": r.seq,
                "text": r.text,
                "kind": r.kind,
                "at": r.ts.isoformat() if r.ts else None,
                "phase": r.phase,
                "status": r.status,
            }
            for r in page
        ]
        next_cursor = start + len(page) if start + len(page) < len(rows) else None
        return {"items": items, "cursor": next_cursor}


def _cost_usd(workspace_id: str) -> float:
    """Sum token ledger spend for ventures attached to this workspace."""
    factory = db()
    with factory() as session:
        venture_ids = [
            v.id
            for v in session.scalars(
                select(Venture).where(Venture.workspace_id == workspace_id)
            ).all()
        ]
        if not venture_ids:
            return 0.0
        rows = session.scalars(
            select(TokenLedger).where(TokenLedger.venture_id.in_(venture_ids))
        ).all()
        return round(sum(float(r.cost_usd or 0) for r in rows), 6)
