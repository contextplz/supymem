"""World model simulate API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import SimulateBody
from app.db.models import Venture
from app.state import MEM, db
from app.worldmodel.actions import Action
from app.worldmodel.simulate import simulate

router = APIRouter(tags=["worldmodel"])


@router.post("/ventures/{venture_id}/worldmodel/simulate")
async def sim(venture_id: str, payload: SimulateBody):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    run = await simulate(venture_id, Action(kind=payload.kind, cost_usd=payload.cost_usd))
    out = {
        "verdict": run.verdict,
        "predictions": [
            {
                "quantity": p.quantity,
                "point": p.point,
                "lo": p.lo,
                "hi": p.hi,
                "method": p.method,
                "confidence": p.confidence,
            }
            for p in run.predictions
        ],
        "method_mix": run.method_mix,
        "llm_calls": run.llm_calls,
        "elapsed_ms": run.elapsed_ms,
        "blocking_reason": run.blocking_reason,
    }
    MEM[f"sim:{venture_id}"] = out
    return out


@router.get("/ventures/{venture_id}/worldmodel")
def get_sim(venture_id: str):
    return MEM.get(f"sim:{venture_id}") or {}
