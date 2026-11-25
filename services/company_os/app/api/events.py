"""Recent in-process events (gateway owns SSE fan-out)."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.eventbus import bus
from app.state import MEM

router = APIRouter(tags=["events"])


@router.get("/events/recent")
async def recent_events(topic: str = "venture.created", limit: int = 50):
    # Best-effort: return anything buffered in MEM
    return {"items": (MEM.get(f"events:{topic}") or [])[-limit:], "seq_hint": getattr(bus, "seq", 0)}
