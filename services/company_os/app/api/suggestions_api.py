"""Suggestions: cards Supymem offers to take over, and the founder's decision on each."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.authz.workspace import accessible_workspace_ids, assert_venture_access
from app.suggestions import service

router = APIRouter(tags=["suggestions"])


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "save", "dismiss"]
    feedback: str = Field(default="", max_length=20)


class ShownBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(max_length=10)


@router.get("/suggestions")
def list_suggestions(status: str = "open", venture_id: str | None = None,
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                     x_subject: str | None = Header(default=None, alias="X-Subject")):
    """`open` = new + saved (the badge and overlay); `history` = everything decided."""
    states = ("new", "saved") if status == "open" else ("accepted", "dismissed", "executing", "done", "failed", "needs_you", "running", "noop")
    workspaces = accessible_workspace_ids(x_subject)
    if venture_id is not None:
        _vid, workspace_id = assert_venture_access(venture_id, x_subject)
        workspaces &= {workspace_id}
    return service.list_for(workspaces, states, venture_id=venture_id, offset=offset, limit=limit)


@router.post("/suggestions/shown")
def shown(body: ShownBody, x_subject: str | None = Header(default=None, alias="X-Subject")):
    allowed = {i["id"] for i in service.list_for(accessible_workspace_ids(x_subject))["items"]}
    service.mark_shown([i for i in body.ids if i in allowed])
    return {"ok": True}


@router.post("/ventures/{venture_id}/suggestions/refresh")
def refresh(venture_id: str, x_subject: str | None = Header(default=None, alias="X-Subject")):
    assert_venture_access(venture_id, x_subject)
    return {"created": len(service.generate(venture_id))}


@router.post("/suggestions/{suggestion_id}/decision")
async def decide(suggestion_id: str, body: DecideBody, request: Request, x_subject: str | None = Header(default=None, alias="X-Subject")):
    row = service.get(suggestion_id)
    if row is None:
        raise HTTPException(404, "not_found")
    assert_venture_access(row.venture_id, x_subject)  # cross-tenant ids look missing
    try:
        if body.decision == "accept":
            if row.action_kind == "browser.task.prepare":
                from app.admin.authn import verify_caller
                return await service.accept(suggestion_id, x_subject, caller=verify_caller(request, required=True))
            return await service.accept(suggestion_id, x_subject)
        ok = service.save(suggestion_id) if body.decision == "save" else service.dismiss(suggestion_id, body.feedback)
    except PermissionError:
        raise HTTPException(403, detail={"error": {"code": "authenticated_device_required",
            "message": "Browser tasks run in the Supymem Mac app. Open this venture there and prepare the suggestion for review."}}) from None
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(409, detail={"error": {"code": "already_decided", "message": "This was already decided."}})
    return service.public(service.get(suggestion_id))
