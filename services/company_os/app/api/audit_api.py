"""Audit log and provenance trace (42 §8)."""

from __future__ import annotations

from fastapi import APIRouter

from app.audit import query
from app.audit.trace import trace

router = APIRouter(tags=["audit"])


@router.get("/workspaces/{workspace_id}/audit")
def list_audit(
    workspace_id: str,
    actor_id: str | None = None,
    topic: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    before_seq: int | None = None,
):
    return query.list_audit(
        workspace_id,
        actor_id=actor_id,
        topic=topic,
        outcome=outcome,
        limit=limit,
        before_seq=before_seq,
    )


@router.get("/trace")
def get_trace(ref: str):
    return trace(ref).to_dict()
