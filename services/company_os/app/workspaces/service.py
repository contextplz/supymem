"""Workspace CRUD (35 §9). Service layer: routes call this, never the ORM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from app.db.models import Workspace, WorkspaceMember
from app.db.session import serialize_writes
from app.state import db
from app.workspaces.resolve import LOCAL_PERSON_ID, local_person
from core.clock import utcnow
from core.ids import new_id

Kind = Literal["venture", "company", "desk"]
EntryMode = Literal["idea", "surprise", "engagement", "existing_company", "product"]

VALID_KINDS = frozenset({"venture", "company", "desk"})
VALID_ENTRY_MODES = frozenset(
    {"idea", "surprise", "engagement", "existing_company", "product"}
)
VALID_TENANCY = frozenset({"local", "self_hosted", "managed"})
AUTONOMY_LEVELS = ("L0", "L1", "L2", "L3", "L4")
OBSERVE_LEVELS = ("O0", "O1", "O2", "O3", "O4")


@dataclass(frozen=True)
class WorkspaceView:
    id: str
    kind: str
    name: str
    entry_mode: str
    owner_id: str
    tenancy: str
    autonomy_level: str
    observe_level: str
    created_at: str
    archived_at: str | None
    settings: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "entry_mode": self.entry_mode,
            "owner_id": self.owner_id,
            "tenancy": self.tenancy,
            "autonomy_level": self.autonomy_level,
            "observe_level": self.observe_level,
            "created_at": self.created_at,
            "archived_at": self.archived_at,
            "settings": self.settings,
        }


def view(ws: Workspace) -> WorkspaceView:
    try:
        settings = json.loads(ws.settings_json or "{}")
    except json.JSONDecodeError:
        settings = {}
    return WorkspaceView(
        id=ws.id,
        kind=ws.kind,
        name=ws.name,
        entry_mode=ws.entry_mode,
        owner_id=ws.owner_id,
        tenancy=ws.tenancy,
        autonomy_level=ws.autonomy_level,
        observe_level=ws.observe_level,
        created_at=ws.created_at.isoformat() if ws.created_at else "",
        archived_at=ws.archived_at.isoformat() if ws.archived_at else None,
        settings=settings,
    )


class WorkspaceError(ValueError):
    """Invalid workspace request. Carries a founder-readable message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def create_workspace(
    *,
    kind: str,
    name: str | None = None,
    entry_mode: str = "idea",
    owner_id: str = LOCAL_PERSON_ID,
    tenancy: str = "local",
    autonomy_level: str = "L1",
) -> WorkspaceView:
    if kind not in VALID_KINDS:
        raise WorkspaceError("invalid_kind", f"Unknown workspace kind: {kind}")
    if entry_mode not in VALID_ENTRY_MODES:
        raise WorkspaceError("invalid_entry_mode", f"Unknown entry mode: {entry_mode}")
    if tenancy not in VALID_TENANCY:
        raise WorkspaceError("invalid_tenancy", f"Unknown tenancy: {tenancy}")

    factory = db()
    now = utcnow()
    with factory() as session:
        if kind == "desk":
            serialize_writes(session, key=f"desk:{owner_id}")
        if owner_id == LOCAL_PERSON_ID:
            local_person(session)
        if kind == "desk":
            existing = session.scalars(
                select(Workspace).where(
                    Workspace.owner_id == owner_id, Workspace.kind == "desk"
                )
            ).first()
            if existing is not None:
                raise WorkspaceError(
                    "desk_exists", "You already have a Desk; there is only ever one."
                )
        from app.core.local_mode import is_local
        from app.core.providers_local import _sample_selected
        ws = Workspace(
            id=new_id("wsp"),
            kind=kind,
            name=(name or _default_name(kind)).strip()[:120],
            entry_mode=entry_mode,
            owner_id=owner_id,
            tenancy=tenancy,
            autonomy_level=autonomy_level,
            observe_level="O0",
            settings_json=json.dumps({"sample": _sample_selected()}) if is_local() else "{}",
            created_at=now,
            updated_at=now,
        )
        session.add(ws)
        session.flush()
        session.add(
            WorkspaceMember(
                id=new_id("wsm"),
                workspace_id=ws.id,
                person_id=owner_id,
                role="owner",
                can_approve=1,
                created_at=now,
            )
        )
        session.commit()
        session.refresh(ws)
        return view(ws)


def _default_name(kind: str) -> str:
    return {"desk": "Desk", "company": "Company", "venture": "Untitled"}.get(
        kind, "Untitled"
    )


def get_workspace(workspace_id: str) -> WorkspaceView | None:
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        return view(ws) if ws else None


def list_workspaces(
    owner_id: str = LOCAL_PERSON_ID, *, include_archived: bool = False
) -> list[WorkspaceView]:
    factory = db()
    with factory() as session:
        stmt = select(Workspace).where(Workspace.owner_id == owner_id)
        if not include_archived:
            stmt = stmt.where(Workspace.archived_at.is_(None))
        rows = session.scalars(stmt.order_by(Workspace.created_at.desc())).all()
        return [view(r) for r in rows]


def rename_workspace(workspace_id: str, name: str) -> WorkspaceView | None:
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return None
        ws.name = name.strip()[:120]
        ws.updated_at = utcnow()
        session.commit()
        session.refresh(ws)
        return view(ws)


def set_autonomy(workspace_id: str, level: str) -> WorkspaceView | None:
    if level not in AUTONOMY_LEVELS:
        raise WorkspaceError("invalid_autonomy", f"Unknown autonomy level: {level}")
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return None
        ws.autonomy_level = level
        ws.updated_at = utcnow()
        session.commit()
        session.refresh(ws)
        return view(ws)


def set_tenancy(workspace_id: str, tenancy: str) -> WorkspaceView | None:
    """Opt a workspace into managed/self_hosted/local (doc 43 / 94 / 96 H2.5)."""
    if tenancy not in VALID_TENANCY:
        raise WorkspaceError("invalid_tenancy", f"Unknown tenancy: {tenancy}")
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return None
        # managed requires a real account subject in the gateway session — enforced at API layer.
        ws.tenancy = tenancy
        ws.updated_at = utcnow()
        session.commit()
        session.refresh(ws)
        return view(ws)


def set_observe_level(workspace_id: str, level: str) -> WorkspaceView | None:
    """Observe level is monotonic upward by grant, instant downward (38 §5)."""
    if level not in OBSERVE_LEVELS:
        raise WorkspaceError("invalid_observe_level", f"Unknown observe level: {level}")
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return None
        ws.observe_level = level
        ws.updated_at = utcnow()
        session.commit()
        session.refresh(ws)
        return view(ws)


def merge_settings(workspace_id: str, patch: dict[str, Any]) -> WorkspaceView | None:
    """Shallow-merge keys into workspace.settings_json."""
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return None
        try:
            current = json.loads(ws.settings_json or "{}")
        except json.JSONDecodeError:
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(patch)
        ws.settings_json = json.dumps(current)
        ws.updated_at = utcnow()
        session.commit()
        session.refresh(ws)
        return view(ws)


def members(workspace_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        ).all()
        return [
            {
                "id": r.id,
                "person_id": r.person_id,
                "role": r.role,
                "can_approve": bool(r.can_approve),
            }
            for r in rows
        ]


def role_of(workspace_id: str, person_id: str) -> str | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.person_id == person_id,
            )
        ).first()
        return row.role if row else None

from app.workspaces import resolve

def greeting_state(person_id: str) -> dict:
    desk = resolve.ensure_desk(person_id)
    with db()() as session:
        ws = session.get(Workspace, desk.id)
        try:
            data = json.loads(ws.settings_json or "{}")
        except json.JSONDecodeError:
            data = {}
    return data if isinstance(data, dict) else {}

def greeting_update(person_id: str, **fields: str) -> None:
    desk = resolve.ensure_desk(person_id)
    with db()() as session:
        serialize_writes(session, key=f"greeting:{desk.id}")
        # Sign-in name sync and greeting outcomes arrive together; without the lock one erases the other.
        ws = session.get(Workspace, desk.id, with_for_update=True)
        try:
            data = json.loads(ws.settings_json or "{}")
        except json.JSONDecodeError:
            data = {}
        data = data if isinstance(data, dict) else {}
        if "greeting_outcome" in fields and data.get("greeted_at"):
            return
        data.update(fields)
        ws.settings_json = json.dumps(data, sort_keys=True)
        ws.updated_at = utcnow()
        session.commit()
