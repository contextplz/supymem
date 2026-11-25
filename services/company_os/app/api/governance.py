"""Governance status (core-side view; authoritative state is warden)."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.warden_client import warden

router = APIRouter(tags=["governance"])


@router.get("/governance/kill-switch")
async def kill_status():
    active = await warden.kill_switch()
    return {"kill_switch": active}


@router.get("/governance/status")
async def gov_status():
    active = await warden.kill_switch()
    return {"kill_switch": active, "note": "authoritative controls via gateway → warden"}
