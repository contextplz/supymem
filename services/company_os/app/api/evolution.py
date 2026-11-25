"""Durable, tenant-scoped evolution history and readiness API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.schemas import CycleBody
from app.db.models import EvolutionCycle, Venture
from app.state import db
from app.tenancy import current_or_none

router = APIRouter(tags=["evolution"])


@router.post("/ventures/{venture_id}/evolution/cycle")
async def evolution_cycle(venture_id: str, payload: CycleBody | None = None):
    factory = db()
    with factory() as session:
        venture = session.get(Venture, venture_id)
        scope = current_or_none()
        if not venture or scope and scope.workspace_id not in {"none", venture.workspace_id}:
            raise HTTPException(404, "not_found")
    raise HTTPException(
        409,
        detail={
            "code": "evolution_not_ready",
            "message": "Evolution requires real product metrics, feedback, a target metric and an approved rollback path.",
        },
    )


@router.get("/ventures/{venture_id}/evolution")
def get_evolution(venture_id: str):
    factory = db()
    with factory() as session:
        venture = session.get(Venture, venture_id)
        scope = current_or_none()
        if not venture or scope and scope.workspace_id not in {"none", venture.workspace_id}:
            raise HTTPException(404, "not_found")
        rows = session.scalars(
            select(EvolutionCycle).where(EvolutionCycle.venture_id == venture_id)
            .order_by(EvolutionCycle.started_at.desc(), EvolutionCycle.id.desc()).limit(100)
        ).all()
        return {"cycles": [{
            "id": row.id,
            "trigger": row.trigger,
            "status": row.status,
            "tokens_used": row.tokens_used,
            "proposals_generated": row.proposals_generated,
            "proposals_promoted": row.proposals_promoted,
            "started_at": row.started_at.isoformat(),
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        } for row in rows]}
