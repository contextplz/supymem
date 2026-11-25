"""Record interest signals from the generated page (45 §4).

``email`` is third_party_pii: stored locally, never sent to a model, never
included in a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.db.models import InterestSignal, SiteVersion
from app.state import db
from core.clock import utcnow
from core.ids import new_id

VALID_SOURCES = frozenset({"direct", "shared_link", "ad"})


@dataclass(frozen=True)
class InterestView:
    id: str
    workspace_id: str
    site_version: int
    # email intentionally omitted from default serialisation helpers used near models
    has_email: bool
    note: str | None
    referrer: str | None
    source: str
    created_at: str

    def to_dict(self, *, include_email: bool = False, email: str | None = None) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "site_version": self.site_version,
            "has_email": self.has_email,
            "note": self.note,
            "referrer": self.referrer,
            "source": self.source,
            "created_at": self.created_at,
        }
        if include_email:
            payload["email"] = email
        return payload


def record(
    *,
    workspace_id: str,
    site_version: int,
    email: str | None = None,
    note: str | None = None,
    referrer: str | None = None,
    source: str = "direct",
) -> InterestView:
    src = source if source in VALID_SOURCES else "direct"
    clean_email = (email or "").strip() or None
    row = InterestSignal(
        id=new_id("int"),
        workspace_id=workspace_id,
        site_version=int(site_version),
        email=clean_email,
        note=(note or "").strip() or None,
        referrer=(referrer or "").strip() or None,
        source=src,
        created_at=utcnow(),
    )
    factory = db()
    with factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    return InterestView(
        id=row.id,
        workspace_id=row.workspace_id,
        site_version=row.site_version,
        has_email=bool(row.email),
        note=row.note,
        referrer=row.referrer,
        source=row.source,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def count_for(workspace_id: str) -> int:
    factory = db()
    with factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(InterestSignal)
                .where(InterestSignal.workspace_id == workspace_id)
            )
            or 0
        )


def aggregate(workspace_id: str, *, venture_id: str | None = None) -> dict[str, Any]:
    """Signal counts; an explicit venture includes only its bound site versions."""
    predicate = InterestSignal.workspace_id == workspace_id
    if venture_id is not None:
        bound_versions = select(SiteVersion.version).where(
            SiteVersion.workspace_id == workspace_id,
            SiteVersion.venture_id == venture_id,
        )
        predicate = predicate & InterestSignal.site_version.in_(bound_versions)
    factory = db()
    with factory() as session:
        total = int(session.scalar(
            select(func.count()).select_from(InterestSignal).where(predicate)
        ) or 0)
        by_version = {
            int(v): int(n)
            for v, n in session.execute(
                select(InterestSignal.site_version, func.count())
                .where(predicate)
                .group_by(InterestSignal.site_version)
            ).all()
        }
    return {"total": total, "by_version": by_version}


def list_leads(workspace_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Founder-facing lead list (includes email). Never pass this into an LLM prompt."""
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(InterestSignal)
            .where(InterestSignal.workspace_id == workspace_id)
            .order_by(InterestSignal.created_at.desc())
            .limit(max(1, min(limit, 500)))
        ).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": row.id,
                    "email": row.email,
                    "has_email": bool(row.email),
                    "note": row.note,
                    "referrer": row.referrer,
                    "source": row.source,
                    "site_version": row.site_version,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
            )
        return out


__all__ = [
    "InterestView",
    "VALID_SOURCES",
    "aggregate",
    "count_for",
    "list_leads",
    "record",
]
