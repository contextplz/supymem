"""Activity list + SSE stream (29 §2)."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.authz.workspace import assert_venture_access
from app.db.models import ActivityLine, Venture
from app.onboarding.state import active_run, latest_run
from app.state import MEM, db
from app.tenancy import current_or_none

router = APIRouter(tags=["activity"])
logger = logging.getLogger(__name__)

# Per-subject stream counter (gateway forwards X-Subject / X-Session-Id)
_STREAM_COUNTS: dict[str, int] = {}
_MAX_STREAMS = 16


def _line_dict(row: ActivityLine) -> dict:
    detail = None
    if row.detail_json:
        try:
            detail = json.loads(row.detail_json)
        except json.JSONDecodeError:
            detail = None
    undo = None
    if row.undo_json:
        try:
            undo = json.loads(row.undo_json)
        except json.JSONDecodeError:
            undo = None
    return {
        "seq": row.seq,
        "phase": row.phase,
        "kind": row.kind,
        "status": row.status,
        "text": row.text,
        "query": row.query,
        "detail": detail,
        "evidence_url": row.evidence_url,
        "undo": undo,
        "cost_usd": row.cost_usd,
        "ts": row.ts.isoformat() + "Z" if row.ts else None,
        "step_id": row.step_id,
    }


@router.get("/ventures/{venture_id}/activity")
def get_activity(
    venture_id: str,
    after_seq: int = Query(0),
    limit: int = Query(100, le=500),
    phase: str | None = None,
    kind: str | None = None,
):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
        q = select(ActivityLine).where(
            ActivityLine.venture_id == venture_id,
            ActivityLine.seq > after_seq,
        )
        if phase:
            q = q.where(ActivityLine.phase == phase)
        if kind:
            q = q.where(ActivityLine.kind == kind)
        q = q.order_by(ActivityLine.seq.asc()).limit(limit)
        rows = session.scalars(q).all()
        items = [_line_dict(r) for r in rows]
        next_cursor = str(items[-1]["seq"]) if items else None
    run = active_run(venture_id) or latest_run(venture_id)
    run_block = None
    if run:
        run_block = {
            "status": run.status,
            "phase": run.phase,
            "phases_done": run.phases_done,
            "phases_total": run.phases_total,
        }
    return {"items": items, "next_cursor": next_cursor, "run": run_block}


def _stream_snapshot(venture_id: str, subject: str | None, cursor: int):
    assert_venture_access(venture_id, subject)
    run = active_run(venture_id) or latest_run(venture_id)
    with db()() as session:
        rows = session.scalars(
            select(ActivityLine)
            .where(ActivityLine.venture_id == venture_id, ActivityLine.seq > cursor)
            .order_by(ActivityLine.seq.asc())
            .limit(50)
        ).all()
        items = [_line_dict(row) for row in rows]
    return run, items


@router.get("/ventures/{venture_id}/activity/stream")
async def stream_activity(venture_id: str, request: Request):
    # Prefer subject from gateway; never key solely by shared proxy IP.
    key = (
        request.headers.get("x-subject")
        or request.headers.get("x-session-id")
        or (request.client.host if request.client else "anon")
    )
    count = _STREAM_COUNTS.get(key, 0)
    if count >= _MAX_STREAMS:
        raise HTTPException(
            429,
            detail={
                "error": {
                    "code": "too_many_streams",
                    "message": "Too many live streams. Close other venture tabs and retry.",
                }
            },
        )
    _STREAM_COUNTS[key] = count + 1

    last = request.headers.get("last-event-id")
    after = int(last) if last and last.isdigit() else 0
    scope = current_or_none()
    subject = request.headers.get("x-subject") or (scope.actor if scope else None)

    async def gen():
        try:
            cursor = after
            last_phase: str | None = None
            last_status: str | None = None
            while True:
                if await request.is_disconnected():
                    break
                run, items = await asyncio.to_thread(_stream_snapshot, venture_id, subject, cursor)
                if run:
                    if run.phase != last_phase:
                        last_phase = run.phase
                        phase_payload = json.dumps(
                            {
                                "phase": run.phase,
                                "phases_done": run.phases_done,
                                "phases_total": run.phases_total,
                                "status": run.status,
                            }
                        )
                        yield f"event: phase\ndata: {phase_payload}\n\n"
                    if run.status != last_status:
                        last_status = run.status
                        if run.status == "paused":
                            yield f"event: paused\ndata: {json.dumps({'reason': run.paused_reason})}\n\n"
                        elif run.status == "done":
                            yield f"event: done\ndata: {json.dumps({'run_id': run.id})}\n\n"
                for item in items:
                    cursor = item["seq"]
                    data = json.dumps(item)
                    yield f"id: {cursor}\nevent: activity\ndata: {data}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(1.0)
        except Exception as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {404, 423}:
                message = "Not found. Return to your workspaces."
            else:
                logger.exception("Activity stream failed")
                message = "Activity is unavailable. Reopen your company to try again."
            yield f"event: unavailable\ndata: {json.dumps({'message': message})}\n\n"
        finally:
            _STREAM_COUNTS[key] = max(0, _STREAM_COUNTS.get(key, 1) - 1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
