"""Compatibility shim for OT-40. Canonical write path is ``edges.record_edge``."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import GtmNetworkEdge
from app.gtm.relationships.edges import ALLOWED_SOURCES, NetworkEdgeError, record_edge
from app.gtm.relationships.service import OutreachError
from app.state import db

__all__ = ["ALLOWED_SOURCES", "add_network_edge", "list_edges"]


def add_network_edge(
    workspace_id: str,
    *,
    from_contact_id: str,
    to_contact_id: str,
    source: str,
    domain: str | None = None,
) -> dict[str, Any]:
    try:
        return record_edge(
            workspace_id,
            from_contact_id=from_contact_id,
            to_contact_id=to_contact_id,
            source=source,
            domain=domain,
        )
    except NetworkEdgeError as exc:
        status = 404 if exc.code == "mixed_workspace_edge" else 409
        raise OutreachError(exc.code, status) from exc


def list_edges(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = session.scalars(
            select(GtmNetworkEdge).where(GtmNetworkEdge.workspace_id == workspace_id)
        ).all()
        return [
            {
                "id": r.id,
                "from_contact_id": r.from_contact_id,
                "to_contact_id": r.to_contact_id,
                "source": r.source,
                "domain": r.domain,
            }
            for r in rows
        ]
