"""Connection routes (39 §8)."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.authz.gate import Denied
from app.authz.workspace import subject_person_id
from app.connectors import service
from app.core.local_mode import is_local

router = APIRouter(tags=["connections"])


class CreateBody(BaseModel):
    provider: str = "github"
    auth_kind: str = "pat"
    external_account: str | None = None
    token: str | None = None
    scopes: list[str] | None = None
    resource_grants: list[str] | None = None


def _fail(exc: service.ConnectionError) -> HTTPException:
    status = {
        "not_found": 404,
        "already_connected": 409,
        "unknown_provider": 422,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


@router.get("/connectors")
def connector_catalog():
    from app.connectors.registry import catalog, connectors_mode

    return {"items": catalog(), "mode": connectors_mode()}


@router.get("/workspaces/{workspace_id}/connections")
def list_connections(workspace_id: str):
    items = service.list_connections(workspace_id)
    return {"items": [c.to_dict() for c in items]}


@router.post("/workspaces/{workspace_id}/connections", status_code=201)
async def create_connection(workspace_id: str, body: CreateBody,
                            x_subject: str | None = Header(default=None, alias="X-Subject")):
    try:
        view = await service.create_connection(
            workspace_id=workspace_id,
            provider=body.provider,
            auth_kind=body.auth_kind,
            external_account=body.external_account,
            token=body.token,
            scopes=body.scopes,
            resource_grants=body.resource_grants,
            actor_id=subject_person_id(x_subject),
            surface="api",
        )
    except Denied as exc:
        raise HTTPException(
            403, detail={"error": {"code": "denied", "message": str(exc)}}
        ) from exc
    except service.ConnectionError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/connections/{connection_id}/revoke")
async def revoke_connection(connection_id: str,
                            x_subject: str | None = Header(default=None, alias="X-Subject")):
    try:
        view = await service.revoke_connection(connection_id, actor_id=subject_person_id(x_subject), surface="api")
    except Denied as exc:
        raise HTTPException(
            403, detail={"error": {"code": "denied", "message": str(exc)}}
        ) from exc
    except service.ConnectionError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.get("/connections/{connection_id}/events")
def connection_events(connection_id: str, limit: int = 50):
    from app.connectors import events

    view = service.get_connection(connection_id)
    if view is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": "Open Connections, refresh your accounts, then select an available connection." if is_local() else "That connection doesn't exist.",
                }
            },
        )
    return {"items": events.recent(connection_id, limit=limit)}
