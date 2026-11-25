"""Idempotent memory so the agent does not double-reply to the same signal."""

from __future__ import annotations

import json
import logging
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import ContentEvent
from app.state import db

log = logging.getLogger(__name__)

SEEN_KIND = "agent.signal_seen"
ACTION_KIND = "agent.action"


def already_handled(workspace_id: str, external_id: str) -> bool:
    if not external_id:
        return False
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentEvent)
            .where(
                ContentEvent.workspace_id == workspace_id,
                ContentEvent.kind.in_((SEEN_KIND, ACTION_KIND)),
            )
            .order_by(ContentEvent.created_at.desc())
            .limit(400)
        ).all()
        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}")
            except Exception:
                continue
            if payload.get("external_id") == external_id:
                return True
    return False


def mark_seen(workspace_id: str, signal: dict[str, Any]) -> None:
    _write(
        workspace_id,
        SEEN_KIND,
        platform=str(signal.get("platform") or "") or None,
        payload={
            "external_id": signal.get("external_id"),
            "kind": signal.get("kind"),
            "summary": str(signal.get("summary") or "")[:240],
        },
    )


def mark_action(
    workspace_id: str, action: dict[str, Any], result: dict[str, Any]
) -> None:
    _write(
        workspace_id,
        ACTION_KIND,
        platform=str(action.get("platform") or result.get("platform") or "") or None,
        post_id=action.get("parent_post_id"),
        payload={
            "external_id": action.get("signal_external_id"),
            "action": action,
            "result": {
                k: result.get(k)
                for k in ("ok", "kind", "mode", "error", "external_url", "external_id")
            },
        },
    )


def _write(
    workspace_id: str,
    kind: str,
    *,
    platform: str | None = None,
    post_id: str | None = None,
    payload: dict[str, Any],
) -> None:
    factory = db()
    with factory() as session:
        session.add(
            ContentEvent(
                id=new_id("cev"),
                workspace_id=workspace_id,
                kind=kind,
                platform=platform,
                post_id=post_id if isinstance(post_id, str) else None,
                payload_json=json.dumps(payload),
                created_at=utcnow(),
            )
        )
        session.commit()
