"""Engagement API (36 §9)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.engagements import service
from app.workspaces.resolve import LOCAL_PERSON_ID

router = APIRouter(tags=["engagements"])


class CreateBody(BaseModel):
    text: str
    origin: str = "composer"
    origin_ref: str | None = None
    requested_by: str | None = None
    advance: bool = True


class AnswerBody(BaseModel):
    answer: str


class AbandonBody(BaseModel):
    reason: str = ""


def _fail(exc: service.EngagementError | service.LifecycleError) -> HTTPException:
    status = {
        "not_found": 404,
        "workspace_not_found": 404,
        "request_too_short": 422,
        "request_too_long": 422,
        "invalid_origin": 422,
        "unknown_status": 422,
        "not_waiting": 409,
        "answer_first": 409,
        "no_question": 409,
        "empty_answer": 422,
        "already_closed": 409,
        "illegal_transition": 409,
        "missing_engagement": 422,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


@router.get("/workspaces/{workspace_id}/engagements")
def list_engagements(workspace_id: str, status: str | None = "open"):
    try:
        items = service.list_engagements(workspace_id, status=status)
    except service.EngagementError as exc:
        raise _fail(exc) from exc
    return {"items": [i.to_dict() for i in items]}


@router.post("/workspaces/{workspace_id}/engagements", status_code=201)
async def create_engagement(workspace_id: str, body: CreateBody):
    try:
        view = await service.create_engagement(
            workspace_id=workspace_id,
            text=body.text,
            origin=body.origin,
            origin_ref=body.origin_ref,
            requested_by=body.requested_by or LOCAL_PERSON_ID,
            surface="api",
        )
        if body.advance and view.status != "awaiting_goahead":
            view = await service.advance(view.id)
    except (service.EngagementError, service.LifecycleError) as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.get("/engagements/{engagement_id}")
def get_engagement(engagement_id: str):
    view = service.get_engagement(engagement_id)
    if view is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": "That piece of work doesn't exist.",
                }
            },
        )
    detail = service.triage_detail(engagement_id)
    payload = view.to_dict()
    if detail:
        payload["triage"] = detail
    return payload


@router.post("/engagements/{engagement_id}/goahead")
async def goahead(engagement_id: str):
    try:
        view = await service.goahead(
            engagement_id, actor_id=LOCAL_PERSON_ID, surface="api"
        )
    except (service.EngagementError, service.LifecycleError) as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/engagements/{engagement_id}/answer")
async def answer(engagement_id: str, body: AnswerBody):
    try:
        view = await service.answer_clarification(
            engagement_id,
            body.answer,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except (service.EngagementError, service.LifecycleError) as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/engagements/{engagement_id}/abandon")
def abandon(engagement_id: str, body: AbandonBody):
    try:
        view = service.abandon(
            engagement_id,
            body.reason,
            actor_id=LOCAL_PERSON_ID,
            surface="api",
        )
    except (service.EngagementError, service.LifecycleError) as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/engagements/{engagement_id}/revert")
def revert(engagement_id: str):
    try:
        result = service.revert(
            engagement_id, actor_id=LOCAL_PERSON_ID, surface="api"
        )
    except (service.EngagementError, service.LifecycleError) as exc:
        raise _fail(exc) from exc
    return result
