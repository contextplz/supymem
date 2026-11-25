"""Local owners choose background work; hosted early access uses admin requests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.analytics.events import emit
from app.chat.actions import beta_scope_enforced
from app.core.local_mode import is_local
from app.core.local_capabilities import enabled, blocked_message
from app.db.models import Person
from app.db.supymem_models import GodModeRequest
from app.email.triggers import on_god_mode_requested
from app.state import db
from core.clock import utcnow
from core.ids import new_id

ACTIVE = ("requested", "approved")
NOTE_MAX = 500


def venture_approved(venture_id: str) -> bool:
    if is_local():
        return enabled("background_work")
    with db()() as s:
        return s.scalar(select(GodModeRequest.id).where(
            GodModeRequest.venture_id == venture_id, GodModeRequest.status == "approved").limit(1)) is not None


def beta_gate_open(venture_id: str) -> bool:
    """True when God mode may be turned on for this venture: gate lifted, or an admin approved it."""
    if is_local():
        return enabled("background_work")
    return not beta_scope_enforced() or venture_approved(venture_id)


def _view(row: GodModeRequest | None) -> dict[str, Any]:
    if row is None:
        return {"status": "none", "requested_at": None, "decided_at": None, "note": ""}
    return {"status": row.status, "requested_at": row.created_at.isoformat(), "note": row.note,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None}


def _latest(s, venture_id: str, person_id: str) -> GodModeRequest | None:
    return s.scalars(select(GodModeRequest).where(
        GodModeRequest.venture_id == venture_id, GodModeRequest.person_id == person_id)
        .order_by(GodModeRequest.created_at.desc()).limit(1)).first()


def _local_status():
    return {**_view(None), "status": "approved" if enabled("background_work") else "none",
            "next_step": blocked_message("background_work")}


def my_status(venture_id: str, person_id: str, workspace_id: str, *, viewed: bool = False) -> dict[str, Any]:
    if is_local():
        return _local_status()
    with db()() as s:
        out = _view(_latest(s, venture_id, person_id))
    if viewed:
        emit("god_mode.request_viewed", person_id=person_id, workspace_id=workspace_id, venture_id=venture_id,
             props={"status": out["status"]})
    return out


def request_access(venture_id: str, workspace_id: str, person_id: str, note: str = "") -> dict[str, Any]:
    """Idempotent: a second call while a request is active returns it unchanged."""
    if is_local():
        return {**_local_status(), "created": False}
    note = (note or "").strip()[:NOTE_MAX]
    with db()() as s:
        row = _latest(s, venture_id, person_id)
        if row is not None and row.status in ACTIVE:
            return {**_view(row), "created": False}
        person = s.get(Person, person_id)
        row = GodModeRequest(id=new_id("gmr"), person_id=person_id, workspace_id=workspace_id, venture_id=venture_id,
                             email=(person.email if person else None) or None, note=note, status="requested",
                             created_at=utcnow())
        s.add(row)
        try:
            s.commit()
        except IntegrityError:  # a concurrent request won the unique index
            s.rollback()
            return {**_view(_latest(s, venture_id, person_id)), "created": False}
        out = _view(row)
    emit("god_mode.requested", person_id=person_id, workspace_id=workspace_id, venture_id=venture_id,
         props={"has_note": bool(note)})
    on_god_mode_requested(venture_id, note)  # founder confirmation + admin heads-up; never raises
    return {**out, "created": True}


def _admin_row(row: GodModeRequest) -> dict[str, Any]:
    return {"id": row.id, "person_id": row.person_id, "workspace_id": row.workspace_id, "venture_id": row.venture_id,
            "email": row.email, "note": row.note, "status": row.status, "created_at": row.created_at.isoformat(),
            "decided_at": row.decided_at.isoformat() if row.decided_at else None, "decided_by": row.decided_by}


def admin_list(status: str | None = None) -> dict[str, Any]:
    with db()() as s:
        counts = dict(s.execute(select(GodModeRequest.status, func.count()).group_by(GodModeRequest.status)).all())
        q = select(GodModeRequest).order_by(GodModeRequest.created_at.desc()).limit(500)
        if status:
            q = q.where(GodModeRequest.status == status)
        return {"counts": {k: counts.get(k, 0) for k in ("requested", "approved", "declined")},
                "items": [_admin_row(r) for r in s.scalars(q).all()]}


def approve(request_id: str, admin_id: str) -> dict[str, Any] | None:
    """Allowlist the venture. Returns None when unknown. Re-approving is a no-op."""
    with db()() as s:
        row = s.get(GodModeRequest, request_id)
        if row is None:
            return None
        if row.status == "approved":
            return _admin_row(row)
        row.status, row.decided_at, row.decided_by = "approved", utcnow(), admin_id
        try:
            s.commit()
        except IntegrityError:  # a declined row cannot revive next to a newer active one
            s.rollback()
            return None
        out = _admin_row(row)
    emit("god_mode.approved", person_id=out["person_id"], workspace_id=out["workspace_id"],
         venture_id=out["venture_id"], props={"decided_by": admin_id})
    return out


def explain_reply(venture_id: str) -> dict[str, Any]:
    """Chat reply for 'turn on god mode': honest early-access note plus the request card. Never enables it."""
    if is_local():
        return {"kind": "text", "citations": [], "body_md": blocked_message("background_work"),
                "actions": [{"label": "Choose your capabilities", "href": "/settings/local/capabilities", "style": "primary"}]}
    return {"kind": "text", "citations": [], "body_md": (
        "God mode is in early access, so I have not turned it on. It lets Supymem choose and run the next steps "
        "for your venture on its own while credits are available, with no daily or lifetime spending cap, and it never sends to real people without your "
        "approval settings. You can ask for access and we will enable it for you."),
        "actions": [{"label": "Request God mode access", "action": "god_mode.request", "style": "primary"}]}


def request_reply(venture_id: str, actor_subject: str | None) -> dict[str, Any]:
    """The chat button for explain_reply: the same request the Settings card sends."""
    from app.authz.workspace import ensure_subject_person
    from app.chat.execute import _workspace_id

    out = request_access(venture_id, _workspace_id(venture_id) or "", ensure_subject_person(actor_subject))
    if is_local():
        return {"kind": "text", "body_md": out["next_step"], "actions": [], "citations": []}
    body = ("God mode is already approved for this venture. Say \"turn on god mode\" to start it." if out["status"] == "approved"
            else "Request sent. The team will review it and let you know; nothing was turned on."
            if out.get("created") else "You already asked for God mode access. The team has your request; nothing was turned on.")
    return {"kind": "text", "body_md": body, "actions": [], "citations": []}
