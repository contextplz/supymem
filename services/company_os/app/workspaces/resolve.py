"""Venture <-> Workspace resolution and the local-person/Desk guarantees.

Every ``/api/ventures/{id}/...`` route keeps working forever (35 §4.1); this is
the single helper that bridges the old key to the new one. Do not inline this
lookup anywhere else.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.local_mode import is_local
from app.db.models import Person, Venture, Workspace, WorkspaceMember
from app.db.session import serialize_writes
from app.state import db
from core.clock import utcnow
from core.ids import new_id

# Stable ids created by migration c7d1e2f3a4b5 for the single local founder.
LOCAL_PERSON_ID = "per_local000000"
LOCAL_DESK_ID = "wsp_desklocal00"


def local_person(session: Session | None = None) -> Person:
    """The single local founder. Created on demand for fresh databases."""
    if session is not None:
        return _local_person(session)
    factory = db()
    with factory() as s:
        person = _local_person(s)
        s.commit()
        s.refresh(person)
        s.expunge(person)
        return person


def _local_person(session: Session) -> Person:
    person = session.get(Person, LOCAL_PERSON_ID)
    if person is None:
        person = Person(
            id=LOCAL_PERSON_ID,
            display_name="Founder",
            email=None,
            created_at=utcnow(),
        )
        session.add(person)
        session.flush()
    return person


def ensure_desk(person_id: str = LOCAL_PERSON_ID) -> Workspace:
    """Every account has exactly one Desk (35 §3.1). Created lazily, never deleted."""
    factory = db()
    with factory() as session:
        if is_local():
            desk = session.scalars(
                select(Workspace).where(Workspace.owner_id == person_id, Workspace.kind == "desk")
            ).first()
            if desk is not None and (person_id != LOCAL_PERSON_ID or session.get(Person, person_id) is not None):
                session.expunge(desk)
                return desk
            session.rollback()
        serialize_writes(session, key=f"desk:{person_id}")
        _local_person(session) if person_id == LOCAL_PERSON_ID else None
        desk = session.scalars(
            select(Workspace).where(
                Workspace.owner_id == person_id, Workspace.kind == "desk"
            )
        ).first()
        if desk is None:
            now = utcnow()
            desk_id = LOCAL_DESK_ID if person_id == LOCAL_PERSON_ID else new_id("wsp")
            desk = Workspace(
                id=desk_id,
                kind="desk",
                name="Desk",
                entry_mode="engagement",
                owner_id=person_id,
                tenancy="local",
                autonomy_level="L1",
                observe_level="O0",
                settings_json="{}",
                created_at=now,
                updated_at=now,
            )
            session.add(desk)
            session.flush()
            session.add(
                WorkspaceMember(
                    id=new_id("wsm"),
                    workspace_id=desk.id,
                    person_id=person_id,
                    role="owner",
                    can_approve=1,
                    created_at=now,
                )
            )
        session.commit()
        session.refresh(desk)
        session.expunge(desk)
        return desk


def workspace_for_venture(venture_id: str) -> Workspace | None:
    """Resolve a legacy venture id to its workspace, healing older rows."""
    factory = db()
    with factory() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            return None
        if venture.workspace_id:
            ws = session.get(Workspace, venture.workspace_id)
            if ws is not None:
                session.expunge(ws)
                return ws
        # A venture created before Phase 10 wiring, or by a raw insert in a test.
        ws = _adopt_venture(session, venture)
        session.commit()
        session.refresh(ws)
        session.expunge(ws)
        return ws


def _adopt_venture(session: Session, venture: Venture) -> Workspace:
    person = _local_person(session)
    now = utcnow()
    ws = Workspace(
        id="wsp" + venture.id[3:] if venture.id.startswith("ven_") else new_id("wsp"),
        kind="venture",
        name=venture.name,
        entry_mode="idea",
        owner_id=person.id,
        tenancy="local",
        autonomy_level=venture.autonomy_level or "L1",
        observe_level="O0",
        settings_json="{}",
        created_at=venture.created_at or now,
        updated_at=now,
    )
    existing = session.get(Workspace, ws.id)
    if existing is not None:
        venture.workspace_id = existing.id
        return existing
    session.add(ws)
    session.flush()
    session.add(
        WorkspaceMember(
            id=new_id("wsm"),
            workspace_id=ws.id,
            person_id=person.id,
            role="owner",
            can_approve=1,
            created_at=now,
        )
    )
    venture.workspace_id = ws.id
    return ws


def venture_for_workspace(workspace_id: str) -> Venture | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(Venture).where(Venture.workspace_id == workspace_id)
        ).first()
        if row:
            session.expunge(row)
        return row


def workspace_or_404(workspace_id: str) -> Workspace:
    from fastapi import HTTPException

    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            raise HTTPException(
                404,
                detail={
                    "error": {
                        "code": "workspace_not_found",
                        "message": "That workspace doesn't exist.",
                    }
                },
            )
        session.expunge(ws)
        return ws
