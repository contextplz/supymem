"""OT-40: first-party warm edges from that tenant's connectors only."""

from __future__ import annotations

from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import event, text

from app.db.models import GtmContact, GtmNetworkEdge
from app.gtm.relationships.scope import gtm_get
from app.state import db

ALLOWED_SOURCES = frozenset(
    {
        "tenant_gmail",
        "tenant_slack",
        "tenant_github",
        "tenant_paste",
        "tenant_asserted",
    }
)


class NetworkEdgeError(ValueError):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _assert_same_workspace(
    *,
    workspace_id: str,
    from_ws: str | None,
    to_ws: str | None,
    source: str,
) -> None:
    if source not in ALLOWED_SOURCES:
        raise NetworkEdgeError("linkedin_source_forbidden")
    if not workspace_id or from_ws != workspace_id or to_ws != workspace_id:
        raise NetworkEdgeError("mixed_workspace_edge")


def record_edge(
    workspace_id: str,
    *,
    from_contact_id: str,
    to_contact_id: str,
    source: str,
    domain: str | None = None,
) -> dict[str, Any]:
    source = (source or "").strip()
    if source not in ALLOWED_SOURCES:
        raise NetworkEdgeError("linkedin_source_forbidden")
    with db()() as session:
        left = gtm_get(session, GtmContact, from_contact_id, workspace_id)
        right = gtm_get(session, GtmContact, to_contact_id, workspace_id)
        if left is None or right is None:
            raise NetworkEdgeError("mixed_workspace_edge")
        _assert_same_workspace(
            workspace_id=workspace_id,
            from_ws=left.workspace_id,
            to_ws=right.workspace_id,
            source=source,
        )
        now = utcnow()
        row = GtmNetworkEdge(
            id=new_id("gtmed"),
            workspace_id=workspace_id,
            from_contact_id=left.id,
            to_contact_id=right.id,
            source=source,
            domain=domain,
            created_at=now,
        )
        session.add(row)
        session.commit()
        return {
            "id": row.id,
            "workspace_id": workspace_id,
            "from_contact_id": left.id,
            "to_contact_id": right.id,
            "source": source,
        }


@event.listens_for(GtmNetworkEdge, "before_insert")
def _forbid_cross_tenant_edge(mapper, connection, target: GtmNetworkEdge) -> None:
    if target.source not in ALLOWED_SOURCES:
        raise NetworkEdgeError("linkedin_source_forbidden")
    rows = connection.execute(
        text("SELECT id, workspace_id FROM gtm_contacts WHERE id IN (:a, :b)"),
        {"a": target.from_contact_id, "b": target.to_contact_id},
    ).mappings()
    found = {r["id"]: r["workspace_id"] for r in rows}
    from_ws = found.get(target.from_contact_id)
    to_ws = found.get(target.to_contact_id)
    _assert_same_workspace(
        workspace_id=target.workspace_id,
        from_ws=from_ws,
        to_ws=to_ws,
        source=target.source,
    )
