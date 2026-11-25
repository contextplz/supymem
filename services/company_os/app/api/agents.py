"""Agent listing."""

from __future__ import annotations

from fastapi import APIRouter

from app.org.registry import registry

router = APIRouter(tags=["agents"])


@router.get("/ventures/{venture_id}/agents")
def agents_list(venture_id: str):
    return {
        "items": [
            {
                "id": a.id,
                "status": a.status,
                "spec_id": a.spec_id,
                "spec_version": a.spec_version,
            }
            for a in registry.list_agents(venture_id)
        ]
    }
