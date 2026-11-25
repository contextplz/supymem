"""Approval lifecycle: routing, decision, expiry, escalation (42 §3).

Expiry is an outcome, not a timeout bug: work blocks rather than proceeding.
Silent approval by inaction is how autonomous systems do damage with a paper
trail that blames the human.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import or_, select

from app.audit.writer import audit
from app.authz.roles import ROLE_RANK, roles_with
from app.db.models import Approval, Engagement, Workflow, WorkspaceMember
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

DEFAULT_WINDOW_HOURS = 72
MAX_NOTIFICATIONS = 2
BATCH_LIMIT = 10
STANDING_KIND = "standing_policy"
STANDING_DECISION_KIND = "standing_policy_decision"

VALID_KINDS = frozenset(
    {
        "engagement_brief",
        "proposal",
        "workflow_write",
        "scope_grant",
        "autonomy_change",
        "external_comms",
        "spend_increase",
        "evolution_promotion",
        "credit_reserve_large",
        "production_deploy",
    }
)
VALID_RISKS = ("low", "medium", "high")
OPEN_STATUSES = ("pending",)


class ApprovalError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ApprovalView:
    id: str
    workspace_id: str
    kind: str
    subject_ref: str
    summary: str
    detail: dict[str, Any]
    risk: str
    requested_by: str
    assigned_to: str | None
    status: str
    decided_by: str | None
    decision_note: str | None
    notified_count: int
    expires_at: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "subject_ref": self.subject_ref,
            "summary": self.summary,
            "detail": self.detail,
            "risk": self.risk,
            "requested_by": self.requested_by,
            "assigned_to": self.assigned_to,
            "status": self.status,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
            "notified_count": self.notified_count,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


def _view(row: Approval) -> ApprovalView:
    try:
        detail = json.loads(row.detail_json or "{}")
    except json.JSONDecodeError:
        detail = {}
    expires = as_utc(row.expires_at) if row.kind in {STANDING_KIND, STANDING_DECISION_KIND} else row.expires_at
    return ApprovalView(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        subject_ref=row.subject_ref,
        summary=row.summary,
        detail=detail,
        risk=row.risk,
        requested_by=row.requested_by,
        assigned_to=row.assigned_to,
        status=row.status,
        decided_by=row.decided_by,
        decision_note=row.decision_note,
        notified_count=row.notified_count,
        expires_at=expires.isoformat() if expires else "",
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def _can_approve(session: Any, workspace_id: str, person_id: str | None) -> bool:
    if not person_id:
        return False
    row = session.scalars(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.person_id == person_id,
        )
    ).first()
    if row is None:
        return False
    return bool(row.can_approve) and ROLE_RANK.get(row.role, -1) >= ROLE_RANK["maintainer"]


def _route(
    session: Any, workspace_id: str, *, subject_ref: str, requested_by: str
) -> tuple[str | None, str | None]:
    """Return (assignee, escalation_note) per 42 §3."""
    candidate: str | None = None

    if subject_ref.startswith("wfl_"):
        flow = session.get(Workflow, subject_ref)
        if flow is not None:
            candidate = flow.owner_id
    elif subject_ref.startswith("eng_"):
        eng = session.get(Engagement, subject_ref)
        if eng is not None:
            candidate = eng.requested_by

    if candidate and _can_approve(session, workspace_id, candidate):
        return candidate, None

    original = candidate
    owner = session.scalars(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == "owner",
        )
    ).first()
    if owner is not None and _can_approve(session, workspace_id, owner.person_id):
        note = None
        if original and original != owner.person_id:
            # Say so in the summary rather than silently reassigning (42 §3).
            note = "escalated to the workspace owner, who can approve this"
        return owner.person_id, note

    # Nobody obvious: pick the weakest role that holds `approve` and is present.
    for role in roles_with("approve"):
        member = session.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == role,
                WorkspaceMember.can_approve == 1,
            )
        ).first()
        if member is not None:
            return member.person_id, f"escalated to a {role}, who can approve this"
    return None, "nobody in this workspace can approve, add an owner"


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def _stage_notification(session: Any, row: Approval, moment: Any) -> bool:
    """The reminder count and its recipient-scoped delivery commit together."""
    if not _can_approve(session, row.workspace_id, row.assigned_to):
        return False
    from app.db.supymem_models import Notice
    session.add(Notice(id=new_id("not"), workspace_id=row.workspace_id,
        person_id=row.assigned_to, kind="approval:" + row.id,
        title="An action needs your approval", body="Review the exact saved action before deciding.",
        target_path=None, read_at=None, created_at=moment))
    row.notified_count += 1
    return True


def request_approval(
    *,
    workspace_id: str,
    kind: str,
    subject_ref: str,
    summary: str,
    requested_by: str,
    risk: str = "low",
    detail: dict[str, Any] | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    standing_action: Any = None,
    founder_id: str | None = None,
    venture_id: str | None = None,
) -> ApprovalView:
    if kind not in VALID_KINDS:
        raise ApprovalError("invalid_kind", f"Unknown approval kind: {kind}")
    if risk not in VALID_RISKS:
        raise ApprovalError("invalid_risk", f"Unknown risk: {risk}")

    if kind == "scope_grant" and (detail or {}).get("standing_policy_v1"):
        from app.approvals.standing import choose_card

        return _view(choose_card(workspace_id=workspace_id, founder_id=requested_by,
                                 detail=detail["standing_policy_v1"]))
    if standing_action is not None and founder_id and venture_id and risk == "low":
        from app.approvals.standing import auto_approve

        approved = auto_approve(workspace_id=workspace_id, venture_id=venture_id, founder_id=founder_id,
            action=standing_action, kind=kind, subject_ref=subject_ref, summary=summary,
            requested_by=requested_by, detail=detail, window_hours=window_hours)
        if approved is not None:
            return _view(approved)

    now = utcnow()
    factory = db()
    with factory() as session:
        assignee, note = _route(
            session, workspace_id, subject_ref=subject_ref, requested_by=requested_by
        )
        row = Approval(
            id=new_id("apr"),
            workspace_id=workspace_id,
            kind=kind,
            subject_ref=subject_ref,
            summary=f"{summary} ({note})" if note else summary,
            detail_json=json.dumps(detail or {}, default=str),
            risk=risk,
            requested_by=requested_by,
            assigned_to=assignee,
            status="pending",
            notified_count=0,
            expires_at=now + timedelta(hours=window_hours),
            created_at=now,
        )
        session.add(row)
        _stage_notification(session, row, now)
        session.commit()
        session.refresh(row)
        view = _view(row)

    audit(
        "approval.requested",
        workspace_id=workspace_id,
        actor_id=requested_by,
        actor_kind="agent" if requested_by.startswith("agt") else "person",
        target_ref=subject_ref,
        approval_id=view.id,
        payload={"kind": kind, "risk": risk, "assigned_to": assignee},
    )
    return view


def decide(
    approval_id: str,
    *,
    decision: str,
    decided_by: str,
    note: str | None = None,
    surface: str = "api",
) -> ApprovalView:
    if decision not in {"approved", "rejected", "withdrawn"}:
        raise ApprovalError("invalid_decision", f"Unknown decision: {decision}")

    factory = db()
    with factory() as session:
        row = session.get(Approval, approval_id)
        if row is None:
            raise ApprovalError("not_found", "That approval doesn't exist.")
        if row.kind in {STANDING_KIND, STANDING_DECISION_KIND}:
            from app.authz.workspace import subject_person_id
            from app.tenancy import current_or_none

            tenant = current_or_none()
            if subject_person_id(decided_by) != row.decided_by or tenant and (
                tenant.workspace_id != row.workspace_id or subject_person_id(tenant.actor) != row.decided_by
            ):
                raise ApprovalError("not_found", "That approval is not available to you.")
        if row.kind == STANDING_KIND and row.status == "standing":
            if decision != "withdrawn":
                raise ApprovalError("invalid_decision", "You can revoke this standing approval.")
            from app.approvals.standing import revoke

            session.close()
            return _view(revoke(approval_id, decided_by))
        if row.status != "pending":
            raise ApprovalError(
                "already_decided", f"That was already {row.status}."
            )
        expires = as_utc(row.expires_at)
        if expires and expires <= utcnow():
            raise ApprovalError("expired", "That request expired; ask for a new one.")
        # A person cannot approve work they requested themselves (42 §9.2).
        if decision == "approved" and decided_by == row.requested_by:
            raise ApprovalError(
                "self_approval", "You can't approve work you requested yourself."
            )
        # Only the requester may withdraw without approver authority.
        if (decision != "withdrawn" or decided_by != row.requested_by) and not _can_approve(
            session, row.workspace_id, decided_by
        ):
            raise ApprovalError("cannot_approve", "Your role can't approve this.")

        row.status = decision
        row.decided_by = decided_by
        row.decided_at = utcnow()
        row.decision_note = note
        session.commit()
        session.refresh(row)
        view = _view(row)

    audit(
        f"approval.{decision}",
        workspace_id=view.workspace_id,
        actor_id=decided_by,
        actor_kind="person",
        surface=surface,
        target_ref=view.subject_ref,
        approval_id=view.id,
        payload={"kind": view.kind, "note": note},
    )
    return view


def batch_decide(
    approval_ids: Iterable[str],
    *,
    decision: str,
    decided_by: str,
    note: str | None = None,
) -> list[ApprovalView]:
    """Only same-kind, same-risk, low-risk sets of at most ten (42 §3).

    Never let one click approve a mixed bag: that is how a high-risk item rides
    along with nine trivial ones.
    """
    ids = list(dict.fromkeys(approval_ids))
    if not ids:
        raise ApprovalError("empty_batch", "Nothing selected.")
    if len(ids) > BATCH_LIMIT:
        raise ApprovalError(
            "batch_too_large", f"Approve at most {BATCH_LIMIT} at a time."
        )

    factory = db()
    with factory() as session:
        rows = session.scalars(select(Approval).where(Approval.id.in_(ids))).all()
        if len(rows) != len(ids):
            raise ApprovalError("not_found", "Some of those approvals don't exist.")
        kinds = {r.kind for r in rows}
        risks = {r.risk for r in rows}
        if len(kinds) > 1:
            raise ApprovalError(
                "mixed_kind", "These are different kinds of request; decide them separately."
            )
        if len(risks) > 1:
            raise ApprovalError(
                "mixed_risk", "These carry different risk; decide them separately."
            )
        if risks != {"low"}:
            raise ApprovalError(
                "risk_too_high", "Only low-risk requests can be decided in a batch."
            )

    # Each still gets its own decision and its own audit row.
    return [
        decide(aid, decision=decision, decided_by=decided_by, note=note) for aid in ids
    ]


def list_approvals(
    workspace_id: str, *, status: str | None = "pending"
) -> list[ApprovalView]:
    if status == "standing":
        from app.approvals.standing import list_grants

        return [_view(row) for row in list_grants(workspace_id)]
    factory = db()
    with factory() as session:
        stmt = select(Approval).where(Approval.workspace_id == workspace_id)
        from app.authz.workspace import subject_person_id
        from app.tenancy import current_or_none

        tenant = current_or_none()
        founder = subject_person_id(tenant.actor) if tenant and tenant.workspace_id == workspace_id else None
        stmt = stmt.where(or_(~Approval.kind.in_((STANDING_KIND, STANDING_DECISION_KIND)),
                              Approval.decided_by == founder if founder else False))
        if status:
            stmt = stmt.where(Approval.status == status)
        rows = session.scalars(stmt.order_by(Approval.created_at.desc())).all()
        return [_view(r) for r in rows]


def get_approval(approval_id: str) -> ApprovalView | None:
    factory = db()
    with factory() as session:
        row = session.get(Approval, approval_id)
        if row is not None and row.kind in {STANDING_KIND, STANDING_DECISION_KIND}:
            from app.authz.workspace import subject_person_id
            from app.tenancy import current_or_none

            tenant = current_or_none()
            if not tenant or tenant.workspace_id != row.workspace_id or subject_person_id(tenant.actor) != row.decided_by:
                return None
        return _view(row) if row else None


def expire_due(now: Any = None) -> list[str]:
    """Expire overdue approvals and block whatever was waiting on them."""
    moment = now or utcnow()
    expired: list[str] = []
    factory = db()
    with factory() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key="approval-notifications")
        rows = session.scalars(
            select(Approval).where(Approval.status == "pending").with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            expires = as_utc(row.expires_at)
            if expires is None or expires > moment:
                continue
            row.status = "expired"
            row.decided_at = moment
            expired.append(row.id)
            if row.subject_ref.startswith("eng_"):
                eng = session.get(Engagement, row.subject_ref)
                if eng is not None and eng.status not in {"delivered", "cancelled"}:
                    eng.status = "blocked"
                    eng.blocked_reason = "nobody decided in 72h"
                    eng.updated_at = moment
            elif row.subject_ref.startswith("wfl_"):
                flow = session.get(Workflow, row.subject_ref)
                if flow is not None and flow.status == "active":
                    flow.status = "paused"
                    flow.paused_reason = "nobody decided in 72h"
                    flow.updated_at = moment
        session.commit()

    for approval_id in expired:
        view = get_approval(approval_id)
        if view is None:
            continue
        audit(
            "approval.expired",
            workspace_id=view.workspace_id,
            actor_id="scheduler",
            actor_kind="system",
            surface="internal",
            target_ref=view.subject_ref,
            approval_id=approval_id,
            outcome="denied",
            denied_reason="nobody decided in 72h",
            payload={"kind": view.kind},
        )
    return expired


def sweep_notifications(now: Any = None) -> list[tuple[str, str]]:
    """Second and final nudge at the halfway mark. Two notifications, then stop."""
    moment = now or utcnow()
    sent: list[tuple[str, str]] = []
    factory = db()
    with factory() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key="approval-notifications")
        rows = session.scalars(
            select(Approval).where(Approval.status == "pending").with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            if row.notified_count >= MAX_NOTIFICATIONS:
                continue
            created = as_utc(row.created_at)
            expires = as_utc(row.expires_at)
            if created is None or expires is None or expires <= moment:
                continue
            halfway = created + (expires - created) / 2
            if moment >= halfway:
                if _stage_notification(session, row, moment):
                    sent.append((row.id, row.assigned_to or ""))
        session.commit()
    return sent
