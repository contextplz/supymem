"""Approval queue routes (42 §8)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.approvals import service

router = APIRouter(tags=["approvals"])


class RequestBody(BaseModel):
    kind: str
    subject_ref: str
    summary: str
    requested_by: str
    risk: str = "low"
    detail: dict = Field(default_factory=dict)


class DecideBody(BaseModel):
    decision: str
    decided_by: str
    note: str | None = None


class BatchBody(BaseModel):
    ids: list[str]
    decision: str
    decided_by: str
    note: str | None = None


def _fail(exc: service.ApprovalError) -> HTTPException:
    status = {
        "not_found": 404,
        "already_decided": 409,
        "expired": 409,
        "self_approval": 403,
        "cannot_approve": 403,
    }.get(exc.code, 422)
    return HTTPException(
        status, detail={"error": {"code": exc.code, "message": exc.message}}
    )


@router.get("/workspaces/{workspace_id}/approvals")
def list_approvals(workspace_id: str, status: str | None = "pending"):
    items = service.list_approvals(workspace_id, status=status)
    return {"items": [i.to_dict() for i in items]}


@router.post("/workspaces/{workspace_id}/approvals", status_code=201)
def request_approval(workspace_id: str, body: RequestBody):
    try:
        view = service.request_approval(
            workspace_id=workspace_id,
            kind=body.kind,
            subject_ref=body.subject_ref,
            summary=body.summary,
            requested_by=body.requested_by,
            risk=body.risk,
            detail=body.detail,
        )
    except service.ApprovalError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.get("/approvals/{approval_id}")
def get_approval(approval_id: str):
    view = service.get_approval(approval_id)
    if view is None:
        raise HTTPException(
            404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": "That approval doesn't exist.",
                }
            },
        )
    return view.to_dict()


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: str, body: DecideBody):
    try:
        view = service.decide(
            approval_id,
            decision=body.decision,
            decided_by=body.decided_by,
            note=body.note,
        )
    except service.ApprovalError as exc:
        raise _fail(exc) from exc
    return view.to_dict()


@router.post("/approvals/batch-decide")
def batch_decide(body: BatchBody):
    try:
        views = service.batch_decide(
            body.ids,
            decision=body.decision,
            decided_by=body.decided_by,
            note=body.note,
        )
    except service.ApprovalError as exc:
        raise _fail(exc) from exc
    return {"items": [v.to_dict() for v in views]}
