"""Workspace routes (35 §9). Argument parsing plus a service call, nothing more."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.audit.writer import audit
from app.authz.roles import role_allows
from app.authz.workspace import assert_workspace_access, ensure_subject_person, subject_person_id
from app.workspaces import resolve, service

router = APIRouter(tags=["workspaces"])


class CreateWorkspace(BaseModel):
    kind: str = Field(default="venture")
    name: str | None = None
    entry_mode: str = Field(default="idea")
    tenancy: str = Field(default="local")


class RenameWorkspace(BaseModel):
    name: str


class AutonomyBody(BaseModel):
    level: str


class TenancyBody(BaseModel):
    tenancy: str
    # When flipping to managed, gateway should have authenticated a real usr_* subject.
    account_subject: str | None = None


class ObserveBody(BaseModel):
    level: str
    scope: str | None = None
    granted_by: str | None = None


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"error": {"code": code, "message": message}})


@router.get("/me")
def me(x_subject: str | None = Header(default=None, alias="X-Subject")):
    person_id = ensure_subject_person(x_subject)
    person = resolve.local_person() if person_id == resolve.LOCAL_PERSON_ID else None
    desk = resolve.ensure_desk(person_id)
    return {
        "person": {
            "id": person_id,
            "display_name": person.display_name if person else "Founder",
            "email": person.email if person else None,
        },
        "desk_id": desk.id,
        "workspaces": [w.to_dict() for w in service.list_workspaces(person_id)],
    }


@router.get("/workspaces")
def list_workspaces(
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    person_id = ensure_subject_person(x_subject)
    resolve.ensure_desk(person_id)
    items = [w.to_dict() for w in service.list_workspaces(person_id)]
    grouped: dict[str, list[dict]] = {"desk": [], "venture": [], "company": []}
    for item in items:
        grouped.setdefault(item["kind"], []).append(item)
    return {"items": items, "grouped": grouped}


@router.post("/workspaces", status_code=201)
def create_workspace(
    body: CreateWorkspace,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    person_id = ensure_subject_person(x_subject)
    try:
        ws = service.create_workspace(
            kind=body.kind,
            name=body.name,
            entry_mode=body.entry_mode,
            owner_id=person_id,
            tenancy=body.tenancy,
        )
    except service.WorkspaceError as exc:
        raise _error(422, exc.code, exc.message) from exc
    audit(
        "workspace.created",
        workspace_id=ws.id,
        actor_id=person_id,
        actor_kind="person",
        surface="api",
        target_ref=ws.id,
        payload={"kind": ws.kind, "entry_mode": ws.entry_mode},
    )
    return ws.to_dict()


@router.get("/workspaces/{workspace_id}")
def get_workspace(workspace_id: str):
    ws = service.get_workspace(workspace_id)
    if ws is None:
        raise _error(404, "workspace_not_found", "That workspace doesn't exist.")
    return {**ws.to_dict(), "members": service.members(workspace_id)}


@router.patch("/workspaces/{workspace_id}")
def rename_workspace(workspace_id: str, body: RenameWorkspace):
    ws = service.rename_workspace(workspace_id, body.name)
    if ws is None:
        raise _error(404, "workspace_not_found", "That workspace doesn't exist.")
    audit(
        "workspace.renamed",
        workspace_id=workspace_id,
        surface="api",
        target_ref=workspace_id,
        payload={"name": ws.name},
    )
    return ws.to_dict()


@router.post("/workspaces/{workspace_id}/tenancy")
def set_tenancy(workspace_id: str, body: TenancyBody):
    before = service.get_workspace(workspace_id)
    if before is None:
        raise _error(404, "workspace_not_found", "That workspace doesn't exist.")
    if body.tenancy == "managed":
        sub = (body.account_subject or "").strip()
        if not sub.startswith("usr_"):
            raise _error(
                403,
                "signin_required",
                "Sign in with an account before enabling managed sync.",
            )
    try:
        ws = service.set_tenancy(workspace_id, body.tenancy)
    except service.WorkspaceError as exc:
        raise _error(422, exc.code, exc.message) from exc
    audit(
        "workspace.tenancy_changed",
        workspace_id=workspace_id,
        surface="api",
        target_ref=workspace_id,
        payload={"tenancy": body.tenancy, "from": before.tenancy},
    )
    return ws.to_dict() if ws else before.to_dict()


@router.post("/workspaces/{workspace_id}/autonomy")
def set_autonomy(workspace_id: str, body: AutonomyBody):
    before = service.get_workspace(workspace_id)
    if before is None:
        raise _error(404, "workspace_not_found", "That workspace doesn't exist.")
    try:
        ws = service.set_autonomy(workspace_id, body.level)
    except service.WorkspaceError as exc:
        raise _error(422, exc.code, exc.message) from exc
    assert ws is not None
    # Audited with old and new values even when it lowers risk (42 §2).
    audit(
        "governance.autonomy.changed",
        workspace_id=workspace_id,
        surface="api",
        target_ref=workspace_id,
        payload={"from": before.autonomy_level, "to": ws.autonomy_level},
    )
    return ws.to_dict()


@router.post("/workspaces/{workspace_id}/observe-level")
def set_observe_level(workspace_id: str, body: ObserveBody,
                      x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_workspace_access(workspace_id, x_subject)
    if not role_allows(service.role_of(workspace_id, subject_person_id(x_subject)), "change_autonomy"):
        raise HTTPException(403, detail={"code": "workspace_admin_required"})
    before = service.get_workspace(workspace_id)
    if before is None:
        raise _error(404, "workspace_not_found", "That workspace doesn't exist.")
    try:
        ws = service.set_observe_level(workspace_id, body.level)
    except service.WorkspaceError as exc:
        raise _error(422, exc.code, exc.message) from exc
    assert ws is not None
    audit(
        "governance.observe.changed",
        workspace_id=workspace_id,
        surface="api",
        target_ref=body.scope or workspace_id,
        payload={
            "from": before.observe_level,
            "to": ws.observe_level,
            "scope": body.scope,
            "granted_by": body.granted_by,
        },
    )
    from app.workspaces.observe import cancel_work_above_level

    cancelled = cancel_work_above_level(workspace_id, ws.observe_level)
    return {**ws.to_dict(), "cancelled_work": cancelled}
