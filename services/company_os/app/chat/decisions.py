"""Approve / reject from chat: only this venture's pending items, and always say which one.

``Approval`` has no venture column, so an approval belongs to this venture when its detail or
subject names it. One with no owner is used only when the workspace has a single venture.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard

_REJECT = re.compile(r"\breject", re.I)
MAX_LISTED = 5


def decision_for(query: str) -> str:
    return "rejected" if _REJECT.search(query or "") else "approved"


def _owner(row: Any) -> str | None:
    owner = (row.detail or {}).get("venture_id")
    if owner:
        return str(owner)
    ref = str(row.subject_ref or "")
    return ref[len("venture:"):] if ref.startswith("venture:") else None


def pending_for_venture(venture_id: str) -> list:
    from sqlalchemy import func, select

    from app.approvals.service import list_approvals
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        ven = session.get(Venture, venture_id)
        wid = ven.workspace_id if ven else None
        if not wid:
            return []
        ventures = session.scalar(select(func.count()).select_from(Venture).where(Venture.workspace_id == wid)) or 0
    return [row for row in list_approvals(wid, status="pending")
            if _owner(row) == venture_id or (_owner(row) is None and ventures == 1)]


def _card(row: Any, query: str) -> dict:
    verb = "Reject" if decision_for(query) == "rejected" else "Approve"
    card = ConfirmationCard.for_action(
        get_action("approve"),
        interpretation=f"{verb} this pending item: {row.summary}",
        consequences=["The decision is recorded through the approvals service and cannot be undone from chat."],
        resulting_state={"query": query, "approval_id": row.id})
    return replace(card, confirm_label=f"{verb}: {row.summary}"[:80]).as_action()


def route(venture_id: str, query: str) -> dict:
    """Card(s) naming exactly what would be decided; nothing is decided here."""
    rows = pending_for_venture(venture_id)
    cockpit = {"label": "Open cockpit", "href": f"/v/{venture_id}"}
    verb = "reject" if decision_for(query) == "rejected" else "approve"
    if not rows:
        return {"kind": "text", "body_md": "No pending approvals for this venture, so there is nothing to "
                f"{verb}.", "actions": [cockpit], "citations": []}
    if len(rows) == 1:
        return {"kind": "confirmation", "body_md": f"One item is waiting: **{rows[0].summary}**. "
                f"Confirm to {verb} exactly that.", "actions": [_card(rows[0], query)], "citations": []}
    shown = rows[:MAX_LISTED]
    lines = "\n".join(f"- {r.summary}" for r in shown)
    more = f"\n\nShowing {len(shown)} of {len(rows)}." if len(rows) > len(shown) else ""
    return {"kind": "confirmation", "body_md": f"{len(rows)} items are waiting, so I did not pick one. "
            f"Which should I {verb}?\n{lines}{more}", "actions": [_card(r, query) for r in shown], "citations": [{"question": True}]}


def decide_one(venture_id: str, query: str, wanted: str, actor: str | None = None) -> dict:
    """Execute after confirm. The target must be one of this venture's pending items."""
    from app.approvals.service import ApprovalError, decide

    cockpit = [{"label": "Open cockpit", "href": f"/v/{venture_id}"}]
    rows = pending_for_venture(venture_id)
    target = next((r for r in rows if r.id == wanted), None) if wanted else (rows[0] if len(rows) == 1 else None)
    if target is None:
        body = ("No pending approval for this venture matches that, so nothing was decided."
                if not rows or wanted else
                f"{len(rows)} items are waiting, so I did not pick one. Tell me which to decide.")
        return {"kind": "text", "body_md": body, "actions": cockpit, "citations": []}
    decision = decision_for(query)
    try:
        view = decide(target.id, decision=decision, decided_by=actor or "founder", note="via chat", surface="chat")
    except ApprovalError as exc:
        return {"kind": "text", "body_md": f"Could not {decision.rstrip('d')} that: {exc.message}",
                "actions": cockpit, "citations": []}
    return {"kind": "text", "body_md": f"Recorded **{view.status}**: {view.summary}.",
            "actions": [{**cockpit[0], "style": "primary"}], "citations": []}
