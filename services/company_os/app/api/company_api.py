"""Company map / discovery routes (38 §9)."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.audit.writer import audit
from app.authz.roles import role_allows
from app.authz.workspace import assert_workspace_access, subject_person_id
from app.discovery import map as map_mod
from app.workspaces import observe, service as workspaces

router = APIRouter(tags=["company"])


class DiscoverBody(BaseModel):
    """Optional observe-ladder bump after a successful discover (38 §5)."""

    bump_observe_to: str | None = None
    scope: str | None = None


@router.get("/workspaces/{workspace_id}/map")
def get_map(workspace_id: str):
    view = map_mod.latest_map(workspace_id)
    if view is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "map_not_found",
                    "message": "No company map yet, run discover first.",
                }
            },
        )
    return view.to_dict()


@router.post("/workspaces/{workspace_id}/discover")
async def discover(workspace_id: str, body: DiscoverBody | None = None,
                   x_subject: str | None = Header(default=None, alias="X-Subject")):
    body = body or DiscoverBody()
    assert_workspace_access(workspace_id, x_subject)
    actor_id = subject_person_id(x_subject)
    if body.bump_observe_to and not role_allows(workspaces.role_of(workspace_id, actor_id), "change_autonomy"):
        raise HTTPException(403, detail={"code": "workspace_admin_required"})
    ws = workspaces.get_workspace(workspace_id)
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
    view = await map_mod.discover_and_map(
        workspace_id, actor_id=actor_id, surface="api"
    )
    result = view.to_dict()
    if body.bump_observe_to:
        before = ws.observe_level
        try:
            updated = workspaces.set_observe_level(workspace_id, body.bump_observe_to)
        except workspaces.WorkspaceError as exc:
            raise HTTPException(
                422, detail={"error": {"code": exc.code, "message": exc.message}}
            ) from exc
        assert updated is not None
        audit(
            "governance.observe.changed",
            workspace_id=workspace_id,
            surface="api",
            target_ref=body.scope or workspace_id,
            payload={
                "from": before,
                "to": updated.observe_level,
                "scope": body.scope,
                "granted_by": actor_id,
                "via": "discover",
            },
        )
        cancelled = observe.cancel_work_above_level(
            workspace_id, updated.observe_level
        )
        result["observe_level"] = updated.observe_level
        result["cancelled_work"] = cancelled
    return result
