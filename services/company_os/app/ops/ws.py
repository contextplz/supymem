"""WebSocket /ws/activity — Polsia live activity feed."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.core.local_mode import is_local
from app.ops.services import activity as activity_svc

router = APIRouter()


@router.websocket("/ws/activity")
async def activity_ws(
    websocket: WebSocket,
    workspace_id: str | None = Query(default=None),
) -> None:
    if is_local():
        from app.core.local_owner import check_request, current_owner

        try:
            check_request(websocket)
            current_owner()
        except HTTPException:
            await websocket.close(code=1008, reason="Open your local app directly and try again.")
            return
    await websocket.accept()
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def _on_event(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    unsub = activity_svc.subscribe(_on_event, workspace_id)
    try:
        # Seed with recent history so a fresh tab isn't empty.
        if workspace_id:
            for item in reversed(activity_svc.get_recent_activity(workspace_id, limit=20)):
                await websocket.send_text(json.dumps(item, default=str))
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25.0)
                await websocket.send_text(json.dumps(event, default=str))
            except asyncio.TimeoutError:
                # Keepalive so proxies don't drop idle sockets.
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        unsub()
