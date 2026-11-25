"""Members and invites (42 §6).

Local tenancy has no email infrastructure, so an invite is a link the owner
shares by hand. The token is stored hashed so the DB never holds a usable one.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.audit.writer import audit
from app.authz.roles import ROLES, role_allows
from app.db.models import Invite, Person, Workspace, WorkspaceMember
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

INVITE_TTL_DAYS = 7


class MemberError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InviteView:
    id: str
    workspace_id: str
    email: str
    role: str
    status: str
    expires_at: str
    # Returned once, at creation. Never stored or shown again.
    link: str | None = None

    def to_dict(self) -> dict[str, Any]:
        body = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "email": self.email,
            "role": self.role,
            "status": self.status,
            "expires_at": self.expires_at,
        }
        if self.link:
            body["link"] = self.link
        return body


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def list_members(workspace_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(WorkspaceMember, Person)
            .join(Person, Person.id == WorkspaceMember.person_id)
            .where(WorkspaceMember.workspace_id == workspace_id)
        ).all()
        out = []
        for member in rows:
            person = session.get(Person, member.person_id)
            out.append(
                {
                    "id": member.id,
                    "person_id": member.person_id,
                    "display_name": person.display_name if person else member.person_id,
                    "email": person.email if person else None,
                    "role": member.role,
                    "can_approve": bool(member.can_approve),
                }
            )
        return out


def create_invite(
    workspace_id: str, *, email: str, role: str, invited_by: str
) -> InviteView:
    if role not in ROLES:
        raise MemberError("invalid_role", f"Unknown role: {role}")
    if role == "owner":
        raise MemberError(
            "cannot_invite_owner",
            "There is exactly one owner; transfer ownership explicitly instead.",
        )

    token = secrets.token_urlsafe(24)
    now = utcnow()
    factory = db()
    with factory() as session:
        if session.get(Workspace, workspace_id) is None:
            raise MemberError("workspace_not_found", "That workspace doesn't exist.")
        existing = session.scalars(
            select(Invite).where(
                Invite.workspace_id == workspace_id, Invite.email == email
            )
        ).first()
        if existing is not None and existing.status == "pending":
            raise MemberError("already_invited", f"{email} already has an open invite.")
        if existing is not None:
            session.delete(existing)
            session.flush()
        row = Invite(
            id=new_id("inv"),
            workspace_id=workspace_id,
            email=email,
            role=role,
            token_hash=_hash(token),
            invited_by=invited_by,
            status="pending",
            expires_at=now + timedelta(days=INVITE_TTL_DAYS),
            created_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        view = InviteView(
            id=row.id,
            workspace_id=row.workspace_id,
            email=row.email,
            role=row.role,
            status=row.status,
            expires_at=row.expires_at.isoformat(),
            link=f"/invite/{token}",
        )

    audit(
        "invite.created",
        workspace_id=workspace_id,
        actor_id=invited_by,
        actor_kind="person",
        surface="api",
        target_ref=view.id,
        payload={"role": role},
    )
    return view


def peek_invite(token: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(Invite).where(Invite.token_hash == _hash(token))
        ).first()
        if row is None:
            raise MemberError("invalid_invite", "That invite link isn't valid.")
        workspace = session.get(Workspace, row.workspace_id)
        return {
            "email": row.email,
            "role": row.role,
            "status": row.status,
            "workspace_id": row.workspace_id,
            "workspace_name": workspace.name if workspace else row.workspace_id,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }


def accept_invite(token: str, *, display_name: str | None = None) -> dict[str, Any]:
    now = utcnow()
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(Invite).where(Invite.token_hash == _hash(token))
        ).first()
        if row is None:
            raise MemberError("invalid_invite", "That invite link isn't valid.")
        if row.status != "pending":
            raise MemberError("invite_used", f"That invite was already {row.status}.")
        expires = as_utc(row.expires_at)
        if expires is not None and expires <= now:
            row.status = "expired"
            session.commit()
            raise MemberError("invite_expired", "That invite has expired.")

        person = session.scalars(select(Person).where(Person.email == row.email)).first()
        if person is None:
            person = Person(
                id=new_id("per"),
                display_name=display_name or row.email.split("@")[0],
                email=row.email,
                created_at=now,
            )
            session.add(person)
            session.flush()

        already = session.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == row.workspace_id,
                WorkspaceMember.person_id == person.id,
            )
        ).first()
        if already is None:
            session.add(
                WorkspaceMember(
                    id=new_id("wsm"),
                    workspace_id=row.workspace_id,
                    person_id=person.id,
                    role=row.role,
                    can_approve=1 if "approve" in ROLES[row.role] else 0,
                    created_at=now,
                )
            )
        row.status = "accepted"
        row.accepted_at = now
        session.commit()
        result = {
            "workspace_id": row.workspace_id,
            "person_id": person.id,
            "role": row.role,
        }

    audit(
        "invite.accepted",
        workspace_id=result["workspace_id"],
        actor_id=result["person_id"],
        actor_kind="person",
        surface="api",
        target_ref=result["person_id"],
        payload={"role": result["role"]},
    )
    return result


def _require_member_admin(session, row: WorkspaceMember, actor_id: str, *, role: str | None = None) -> None:
    """Changing or removing another member needs owner/admin; only an owner may grant or remove owner."""
    actor = session.scalars(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == row.workspace_id, WorkspaceMember.person_id == actor_id)).first()
    actor_role = actor.role if actor else None
    if role is None and actor is not None and actor.id == row.id:
        return  # anyone may leave a workspace (the last owner is still refused)
    if not role_allows(actor_role, "invite"):
        raise MemberError("forbidden", "Only a workspace owner or admin can change members.")
    if (role == "owner" or row.role == "owner") and actor_role != "owner":
        raise MemberError("forbidden", "Only an owner can grant or remove the owner role.")


def change_role(member_id: str, *, role: str, actor_id: str) -> dict[str, Any]:
    if role not in ROLES:
        raise MemberError("invalid_role", f"Unknown role: {role}")
    factory = db()
    with factory() as session:
        row = session.get(WorkspaceMember, member_id)
        if row is None:
            raise MemberError("not_found", "That member doesn't exist.")
        _require_member_admin(session, row, actor_id, role=role)
        if row.role == "owner" and role != "owner":
            _refuse_if_last_owner(session, row)
        row.role = role
        row.can_approve = 1 if "approve" in ROLES[role] else 0
        session.commit()
        result = {"id": row.id, "person_id": row.person_id, "role": row.role,
                  "workspace_id": row.workspace_id}

    audit(
        "member.role_changed",
        workspace_id=result["workspace_id"],
        actor_id=actor_id,
        actor_kind="person",
        surface="api",
        target_ref=result["person_id"],
        payload={"to": role},
    )
    return result


def remove_member(member_id: str, *, actor_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.get(WorkspaceMember, member_id)
        if row is None:
            raise MemberError("not_found", "That member doesn't exist.")
        _require_member_admin(session, row, actor_id)
        if row.role == "owner":
            _refuse_if_last_owner(session, row)
        result = {"workspace_id": row.workspace_id, "person_id": row.person_id}
        session.delete(row)
        session.commit()

    audit(
        "member.removed",
        workspace_id=result["workspace_id"],
        actor_id=actor_id,
        actor_kind="person",
        surface="api",
        target_ref=result["person_id"],
    )
    return result


def _refuse_if_last_owner(session: Any, row: WorkspaceMember) -> None:
    owners = session.scalars(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == row.workspace_id,
            WorkspaceMember.role == "owner",
        )
    ).all()
    if len({o.id for o in owners}) <= 1:
        raise MemberError(
            "last_owner",
            "This is the only owner; transfer ownership before removing them.",
        )
