"""Assert session subject owns the workspace (or is a member). Cross-tenant → 404."""

from __future__ import annotations

from contextlib import nullcontext

from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ConsumerUser, Person, PersonIdentity, Venture, Workspace, WorkspaceMember
from app.db.session import serialize_writes
from app.state import db
from app.workspaces.resolve import LOCAL_PERSON_ID
from core.clock import utcnow


def subject_person_id(subject: str | None) -> str:
    """Map gateway X-Subject to a Person / Workspace.owner_id."""
    sub = (subject or "").strip()
    if not sub or sub in {"founder", "local", LOCAL_PERSON_ID}:
        return LOCAL_PERSON_ID
    return sub


def ensure_subject_person(subject: str | None, *, email: str | None = None) -> str:
    """Ensure an authenticated account subject has a local Person row."""
    person_id = subject_person_id(subject)
    factory = db()
    with factory() as session:
        serialize_writes(session, key=f"person:{person_id}")
        person = session.get(Person, person_id)
        if person is None:
            person = Person(
                id=person_id,
                display_name="Founder",
                email=None,
                created_at=utcnow(),
            )
            session.add(person)
        if email and email.strip():
            account_email = email.strip().lower()
            if not person.email:
                person.email = account_email
            identity = session.scalar(select(PersonIdentity).where(PersonIdentity.subject == person_id))
            if identity is not None:
                if not identity.email:
                    identity.email = account_email
                linked_person = session.get(Person, identity.person_id)
                if not linked_person.email:
                    linked_person.email = identity.email
        session.commit()
    return person_id


def _is_member(session, workspace_id: str, person_id: str) -> bool:
    row = session.scalars(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.person_id == person_id,
        )
    ).first()
    return row is not None


def person_for_principal(principal) -> str | None:
    with db()() as session:
        user = session.get(ConsumerUser, principal.user_id)
        if user is not None and (user.person_id or "").strip():
            return user.person_id
        ident = session.scalars(
            select(PersonIdentity).where(
                PersonIdentity.subject.in_(
                    [principal.cognito_sub, principal.user_id, principal.email]
                )
            )
        ).first()
        if ident is not None:
            return ident.person_id
        by_email = session.scalars(
            select(ConsumerUser).where(ConsumerUser.cognito_sub == principal.cognito_sub)
        ).first()
        if by_email is not None and (by_email.person_id or "").strip():
            return by_email.person_id
    return None


def workspace_for_person(workspace_id: str, person_id: str) -> Workspace | None:
    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is not None and (workspace.owner_id == person_id or _is_member(session, workspace_id, person_id)):
            session.expunge(workspace)
            return workspace
    return None


def accessible_workspace_ids(subject: str | None) -> set[str]:
    """Workspace ids the current subject may see in portfolio/list endpoints."""
    person = subject_person_id(subject)
    factory = db()
    with factory() as session:
        owned = session.scalars(
            select(Workspace.id).where(Workspace.owner_id == person)
        ).all()
        member = session.scalars(
            select(WorkspaceMember.workspace_id).where(
                WorkspaceMember.person_id == person
            )
        ).all()
    return set(owned) | set(member)


def assert_workspace_access(
    workspace_id: str,
    subject: str | None,
    *,
    hide: bool = True,
    session: Session | None = None,
) -> str:
    """Raise 404 if subject cannot access workspace. Returns workspace_id."""
    person = subject_person_id(subject)
    with (db()() if session is None else nullcontext(session)) as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            raise HTTPException(
                404, detail={"error": {"code": "not_found", "message": "Not found."}}
            )
        if ws.owner_id == person or _is_member(session, workspace_id, person):
            try:
                import json

                settings = json.loads(ws.settings_json or "{}")
            except json.JSONDecodeError:
                settings = {}
            if settings.get("blocked"):
                raise HTTPException(
                    423,
                    detail={
                        "error": {
                            "code": "workspace_blocked",
                            "message": "This workspace is blocked.",
                        }
                    },
                )
            return workspace_id
    if hide:
        raise HTTPException(
            404, detail={"error": {"code": "not_found", "message": "Not found."}}
        )
    raise HTTPException(
        403, detail={"error": {"code": "forbidden", "message": "Not allowed."}}
    )


def assert_venture_access(
    venture_id: str,
    subject: str | None,
    *,
    session: Session | None = None,
) -> tuple[str, str]:
    """Return (venture_id, workspace_id) or 404."""
    with (db()() if session is None else nullcontext(session)) as session:
        ven = session.get(Venture, venture_id)
        if ven is None or not ven.workspace_id:
            raise HTTPException(
                404, detail={"error": {"code": "not_found", "message": "Not found."}}
            )
        workspace_id = ven.workspace_id
        assert_workspace_access(workspace_id, subject, session=session)
    return venture_id, workspace_id


def subject_header(
    x_subject: str | None = Header(default=None, alias="X-Subject"),
) -> str | None:
    return x_subject


__all__ = [
    "accessible_workspace_ids",
    "ensure_subject_person",
    "assert_venture_access",
    "assert_workspace_access",
    "subject_header",
    "subject_person_id",
]
