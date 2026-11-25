"""Workspace-scoped GTM loads. Outreach never reads by bare primary key."""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy.orm import Session

from app.db.models import GtmContact

T = TypeVar("T")


def gtm_get(session: Session, model: type[T], id: str, workspace_id: str) -> T | None:
    """Return the row only when it belongs to ``workspace_id``. Else ``None``."""

    if not id or not workspace_id:
        return None
    row = session.get(model, id)
    if row is None:
        return None
    ws = getattr(row, "workspace_id", None)
    if ws == workspace_id:
        return row
    if ws is None:
        contact_id = getattr(row, "contact_id", None)
        if not contact_id:
            return None
        parent = session.get(GtmContact, contact_id)
        if parent is None or parent.workspace_id != workspace_id:
            return None
        return row
    return None
