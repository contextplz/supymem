"""Members and invites (42 §8)."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from app.authz.workspace import assert_workspace_access, subject_person_id
from app.billing.entitlements import current_plan
from app.core.local_mode import is_local
from app.workspaces import members

router = APIRouter(tags=["members"])


class InviteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    role: str


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str


def _fail(exc: members.MemberError) -> HTTPException:
    status = {
        "not_found": 404,
        "forbidden": 403,
        "workspace_not_found": 404,
        "invalid_invite": 404,
        "last_owner": 409,
        "already_invited": 409,
        "invite_used": 409,
        "invite_expired": 410,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


@router.get("/workspaces/{workspace_id}/members")
def list_members(
    workspace_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    return {"items": members.list_members(workspace_id)}


@router.post("/workspaces/{workspace_id}/invites", status_code=201)
def create_invite(
    workspace_id: str,
    body: InviteBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    if not is_local():
        plan = current_plan(
            workspace_id=workspace_id, person_id=subject_person_id(x_subject)
        )
        current = members.list_members(workspace_id)
        if len(current) >= plan.seats:
            raise HTTPException(
                402,
                detail={
                    "error": {
                        "code": "seat_limit",
                        "message": f"This plan allows {plan.seats} people. Upgrade to invite more.",
                    }
                },
            )
    try:
        view = members.create_invite(
            workspace_id,
            email=body.email,
            role=body.role,
            invited_by=subject_person_id(x_subject),
        )
    except members.MemberError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.get("/invites/{token}")
def peek_invite(token: str):
    try:
        return members.peek_invite(token)
    except members.MemberError as exc:
        raise _fail(exc) from exc


@router.post("/invites/{token}/accept")
def accept_invite(token: str, body: AcceptBody):
    try:
        result = members.accept_invite(token, display_name=body.display_name)
    except members.MemberError as exc:
        raise _fail(exc) from exc
    try:
        from app.analytics.events import emit

        emit(
            "invite.accepted",
            person_id=result.get("person_id"),
            workspace_id=result.get("workspace_id"),
        )
    except Exception:
        pass
    return result


@router.patch("/members/{member_id}")
def change_role(
    member_id: str,
    body: RoleBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    try:
        return members.change_role(
            member_id, role=body.role, actor_id=subject_person_id(x_subject)
        )
    except members.MemberError as exc:
        raise _fail(exc) from exc


@router.delete("/members/{member_id}")
def remove_member(
    member_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    try:
        return members.remove_member(member_id, actor_id=subject_person_id(x_subject))
    except members.MemberError as exc:
        raise _fail(exc) from exc
