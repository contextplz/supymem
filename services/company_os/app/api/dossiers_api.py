"""Deep-research dossiers (docs/supymem/25). File markdown path stays on research_api.py."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.authz.workspace import assert_venture_access
from app.research.service import export_markdown, get_dossier, list_dossiers, request_research_stop, rerun, review_quote, set_finding_flag

router = APIRouter(tags=["research-board"])


class FlagBody(BaseModel):
    founder_flag: str | None = None


class RerunBody(BaseModel):
    phase: str | None = None
    source_digest: str | None = None
    request_key: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/ventures/{venture_id}/research/dossiers")
def dossiers(
    venture_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    return {"items": list_dossiers(venture_id)}


@router.get("/ventures/{venture_id}/research/dossiers/{dossier_id}")
def dossier(
    venture_id: str,
    dossier_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        data = get_dossier(dossier_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "not_found", "message": str(exc)}}) from exc
    if data["venture_id"] != venture_id:
        raise HTTPException(404, detail={"error": {"code": "not_found", "message": "dossier"}})
    return data


@router.post("/ventures/{venture_id}/research/jobs/{job_id}/stop")
def stop_research(venture_id: str, job_id: str, request: Request):
    from app.admin.authn import verify_caller
    from app.core.local_mode import is_local

    not_found = {"code": "not_found", "message": "You cannot stop this run here. Open Research for the idea and check its progress."}
    if not is_local():
        raise HTTPException(404, detail=not_found)
    actor = verify_caller(request).sub
    _, workspace_id = assert_venture_access(venture_id, actor)
    try:
        stopped = request_research_stop(venture_id, job_id, workspace_id, actor)
    except KeyError as exc:
        raise HTTPException(404, detail=not_found) from exc
    return {"stop_requested": stopped}


@router.patch("/ventures/{venture_id}/research/findings/{finding_id}")
def patch_finding(
    venture_id: str,
    finding_id: str,
    body: FlagBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        return set_finding_flag(venture_id, finding_id, body.founder_flag)
    except (KeyError, ValueError) as exc:
        code = 404 if isinstance(exc, KeyError) else 422
        raise HTTPException(code, detail={"error": {"code": type(exc).__name__, "message": str(exc)}}) from exc


@router.post("/ventures/{venture_id}/research/dossiers/{dossier_id}/rerun")
def rerun_dossier(
    venture_id: str,
    dossier_id: str,
    body: RerunBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    body = body or RerunBody()
    try:
        if get_dossier(dossier_id)["venture_id"] != venture_id:
            raise KeyError("dossier")
        return rerun(dossier_id, body.phase, body.source_digest, body.request_key)
    except (KeyError, ValueError, RuntimeError) as exc:
        status = 404 if isinstance(exc, KeyError) else 422 if isinstance(exc, ValueError) else 409
        raise HTTPException(status, detail={"error": {"code": str(exc)}}) from exc


@router.get("/ventures/{venture_id}/research/dossiers/{dossier_id}/review-quote")
def quote_saved_review(venture_id: str, dossier_id: str,
                       x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    try:
        if get_dossier(dossier_id)["venture_id"] != venture_id:
            raise KeyError("dossier")
        return review_quote(dossier_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        status = 404 if isinstance(exc, KeyError) else 422 if isinstance(exc, ValueError) else 409
        raise HTTPException(status, detail={"error": {"code": str(exc)}}) from exc


@router.get("/ventures/{venture_id}/research/dossiers/{dossier_id}/export")
def export_dossier(
    venture_id: str,
    dossier_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_venture_access(venture_id, x_subject)
    try:
        exported = export_markdown(venture_id, dossier_id)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": {"code": "not_found", "message": str(exc)}}) from exc
    return PlainTextResponse(exported["markdown"], media_type="text/markdown")
