"""Real bounded source intake. Legacy host paths/fixtures/presigns are not mounted."""
import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import Field
from starlette.requests import ClientDisconnect

from app.authz.workspace import assert_venture_access
from app.imports import intake, cleanup
from app.imports.contracts import ImportFailure, Source, Strict
from core.bounded_body import BodyRejected, zip_body

router = APIRouter(prefix="/ventures/{venture_id}/apps/{app_id}/imports", tags=["imports"])


def scope(venture_id: str, x_subject: str = Header(alias="X-Subject"),
          x_workspace_id: str = Header(alias="X-Workspace-ID")):
    _, wid = assert_venture_access(venture_id, x_subject)
    if wid != x_workspace_id:
        raise HTTPException(404, detail={"code": "import_not_found"})
    return wid, x_subject


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ImportFailure as exc:
        raise HTTPException(exc.status, detail={"code": exc.code},
                            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None) from None


class Start(Strict):
    source: Source


class Revision(Strict):
    state_version: int = Field(ge=0)


@router.get("/capabilities")
def capabilities(venture_id: str, app_id: str, bound=Depends(scope)):
    return call(intake.capabilities, bound[0], venture_id, app_id, actor=bound[1])


@router.get("")
def recent(venture_id: str, app_id: str, before: str | None = None, bound=Depends(scope)):
    return call(intake.recent, bound[0], venture_id, app_id, actor=bound[1], before=before)


@router.post("", status_code=202)
def start(venture_id: str, app_id: str, body: Start,
          idempotency_key: Annotated[str, Header(alias="Idempotency-Key")], bound=Depends(scope)):
    return call(intake.start, bound[0], venture_id, app_id, body.source.model_dump(mode="json"), actor=bound[1], idempotency_key=idempotency_key)


@router.get("/{import_id}")
def read(venture_id: str, app_id: str, import_id: str, bound=Depends(scope)):
    return call(intake.read, bound[0], venture_id, app_id, import_id, actor=bound[1])


@router.post("/{import_id}/cancel")
def cancel(venture_id: str, app_id: str, import_id: str, body: Revision, bound=Depends(scope)):
    return call(intake.cancel, bound[0], venture_id, app_id, import_id, actor=bound[1], state_version=body.state_version)


@router.post("/{import_id}/retry")
def retry(venture_id: str, app_id: str, import_id: str, body: Revision, bound=Depends(scope)):
    return call(intake.retry, bound[0], venture_id, app_id, import_id, actor=bound[1], state_version=body.state_version)


@router.put("/{import_id}/archive", status_code=202)
async def upload(venture_id: str, app_id: str, import_id: str, request: Request, bound=Depends(scope)):
    args, auth = (bound[0], venture_id, app_id, import_id), {"actor": bound[1]}
    claim = await asyncio.to_thread(call, intake.prepare_upload, *args, **auth)
    if claim["complete"]:
        return claim["view"]
    try:
        blob = await zip_body(request, expected=claim["size_bytes"])
        return await asyncio.to_thread(call, intake.finish_upload, *args, blob, **auth, claim_id=claim["claim_id"])
    except (BodyRejected, ClientDisconnect, HTTPException) as exc:
        await asyncio.to_thread(intake.abort_upload, *args, **auth, claim_id=claim["claim_id"])
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(getattr(exc, "status", 400), detail={"code": getattr(exc, "code", "import_upload_interrupted")}) from None


@router.post("/{import_id}/delete")
def delete(venture_id: str, app_id: str, import_id: str, body: Revision, bound=Depends(scope)):
    return call(cleanup.remove, bound[0], venture_id, app_id, import_id, actor=bound[1], state_version=body.state_version)


@router.get("/{import_id}/cleanup")
def cleanup_status(venture_id: str, app_id: str, import_id: str, bound=Depends(scope)):
    return {"items": call(cleanup.read_summary, bound[0], venture_id, app_id, import_id, actor=bound[1])}


@router.post("/{import_id}/cleanup/{cleanup_id}/retry")
def cleanup_retry(venture_id: str, app_id: str, import_id: str, cleanup_id: str, body: Revision, bound=Depends(scope)):
    return {"items": call(cleanup.recover, bound[0], venture_id, app_id, import_id, cleanup_id,
                          actor=bound[1], state_version=body.state_version)}
