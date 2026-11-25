"""OT-46: narrow paid-conversion pilots."""

from __future__ import annotations

import json
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import GtmContact, GtmPilot
from app.gtm.relationships.scope import gtm_get
from app.state import db

PILOT_STATUSES = frozenset(
    {"proposed", "running", "succeeded", "failed", "converted", "dropped"}
)


class PilotError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _row(p: GtmPilot) -> dict[str, Any]:
    return {
        "id": p.id,
        "contact_id": p.contact_id,
        "company_id": p.company_id,
        "use_case": p.use_case,
        "team": p.team,
        "baseline": json.loads(p.baseline_json or "{}"),
        "success_criterion": p.success_criterion,
        "duration_days": p.duration_days,
        "status": p.status,
        "paid_decision_at": p.paid_decision_at.isoformat() if p.paid_decision_at else None,
        "permission_to_demo_at": p.permission_to_demo_at.isoformat()
        if p.permission_to_demo_at
        else None,
    }


def create_pilot(
    workspace_id: str,
    *,
    contact_id: str,
    use_case: str,
    success_criterion: str,
    team: str | None = None,
    baseline: dict[str, Any] | None = None,
    duration_days: int = 14,
) -> dict[str, Any]:
    if len((use_case or "").strip()) < 4:
        raise PilotError("use_case_required")
    if len((success_criterion or "").strip()) < 8:
        raise PilotError("success_criterion_required")
    now = utcnow()
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise PilotError("contact_not_found", 404)
        row = GtmPilot(
            id=new_id("gtmpl"),
            workspace_id=workspace_id,
            company_id=contact.company_id,
            contact_id=contact.id,
            use_case=use_case.strip(),
            team=team,
            baseline_json=json.dumps(baseline or {}),
            success_criterion=success_criterion.strip(),
            duration_days=duration_days,
            status="proposed",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        return _row(row)


def list_pilots(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmPilot)
                .where(GtmPilot.workspace_id == workspace_id)
                .order_by(GtmPilot.created_at.desc())
            )
        )
        return [_row(p) for p in rows]


def patch_pilot(workspace_id: str, pilot_id: str, body: dict[str, Any]) -> dict[str, Any]:
    with db()() as session:
        row = gtm_get(session, GtmPilot, pilot_id, workspace_id)
        if row is None:
            raise PilotError("pilot_not_found", 404)
        if "status" in body:
            status = str(body["status"])
            if status not in PILOT_STATUSES:
                raise PilotError("unknown_status")
            row.status = status
        if body.get("permission_to_demo"):
            row.permission_to_demo_at = utcnow()
        if body.get("paid_decision"):
            row.paid_decision_at = utcnow()
            row.status = "converted"
        if "success_criterion" in body:
            row.success_criterion = str(body["success_criterion"])
        row.updated_at = utcnow()
        session.commit()
        return _row(row)
