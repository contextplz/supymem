"""Venture watches — Huginn event graph (websites, RSS, webhooks, peaks)."""

from __future__ import annotations

from app.watch import bridge as _service

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


router = APIRouter(tags=["watch"])


class WatchRunBody(BaseModel):
    force: bool = False


@router.get("/ventures/{venture_id}/watch")
def get_watch(venture_id: str):
    return _service.watch_get_watch(venture_id)


@router.post("/ventures/{venture_id}/watch/run")
def run_watch(venture_id: str, payload: WatchRunBody | None = None):
    return _service.watch_run_watch(venture_id, payload)


@router.post("/ventures/{venture_id}/watch/webhooks/{secret}")
async def inbound_watch_webhook(venture_id: str, secret: str, request: Request):
    return await _service.watch_inbound_watch_webhook(venture_id, secret, request)
