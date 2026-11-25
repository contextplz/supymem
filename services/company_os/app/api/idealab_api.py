"""Idea Lab API (37 §9) — slate, reroll, reject, promote."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.idealab import promote as promote_mod
from app.idealab import slate as slate_mod
from app.workspaces.resolve import LOCAL_PERSON_ID

router = APIRouter(tags=["idealab"])


class SlateBody(BaseModel):
    seed: dict | None = None


class RerollBody(BaseModel):
    previous_slate_id: str | None = None
    reject_reasons: list[dict] = Field(default_factory=list)


class RejectBody(BaseModel):
    opportunity_id: str
    reason_code: str = "other"
    note: str | None = None


class PromoteBody(BaseModel):
    opportunity_id: str
    start: bool = False


def _fail(exc: slate_mod.IdeaLabError) -> HTTPException:
    status = {
        "slate_not_found": 404,
        "opportunity_not_found": 404,
        "workspace_not_found": 404,
        "already_promoted": 409,
        "rejected": 409,
        "too_thin": 422,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


@router.post("/workspaces/{workspace_id}/idealab/slate", status_code=201)
async def create_slate(workspace_id: str, body: SlateBody | None = None):
    body = body or SlateBody()
    try:
        view = await slate_mod.create_slate(
            workspace_id=workspace_id,
            seed_input=body.seed,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except slate_mod.IdeaLabError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.get("/workspaces/{workspace_id}/idealab/slate")
def get_latest_slate(workspace_id: str):
    view = slate_mod.latest_slate(workspace_id)
    if view is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "slate_not_found",
                    "message": "No slate on this bench yet.",
                }
            },
        )
    return view.to_dict()


@router.get("/idealab/slates/{slate_id}")
def get_slate(slate_id: str):
    try:
        return slate_mod.get_slate(slate_id).to_dict()
    except slate_mod.IdeaLabError as exc:
        raise _fail(exc) from exc


@router.post("/workspaces/{workspace_id}/idealab/reroll", status_code=201)
async def reroll(workspace_id: str, body: RerollBody):
    previous = body.previous_slate_id
    if not previous:
        latest = slate_mod.latest_slate(workspace_id)
        if latest is None:
            raise HTTPException(
                404,
                detail={
                    "error": {
                        "code": "slate_not_found",
                        "message": "Nothing to reroll, generate a slate first.",
                    }
                },
            )
        previous = latest.id
    try:
        view = await slate_mod.reroll(
            workspace_id=workspace_id,
            previous_slate_id=previous,
            reject_reasons=body.reject_reasons,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except slate_mod.IdeaLabError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/workspaces/{workspace_id}/idealab/reject")
def reject(workspace_id: str, body: RejectBody):
    try:
        view = slate_mod.reject_opportunity(
            body.opportunity_id,
            workspace_id=workspace_id,
            reason_code=body.reason_code,
            note=body.note,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except slate_mod.IdeaLabError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/workspaces/{workspace_id}/idealab/promote", status_code=201)
def promote(workspace_id: str, body: PromoteBody):
    try:
        slate_mod.get_opportunity(body.opportunity_id, workspace_id=workspace_id)
        result = promote_mod.promote(
            body.opportunity_id,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
            start=body.start,
        )
    except slate_mod.IdeaLabError as exc:
        raise _fail(exc) from exc
    return result.to_dict()
