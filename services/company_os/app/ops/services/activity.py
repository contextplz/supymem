"""Ops activity log + in-process pub/sub for WebSocket fans-out."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from typing import Any, Callable

from sqlalchemy import select

from app.db.models import OpsActivity
from app.state import db
from core.clock import utcnow
from core.ids import new_id

_lock = threading.Lock()
_subscribers: dict[str | None, list[Callable[[dict[str, Any]], None]]] = defaultdict(list)


def subscribe(callback: Callable[[dict[str, Any]], None], workspace_id: str | None = None) -> Callable[[], None]:
    with _lock:
        _subscribers[workspace_id].append(callback)

    def _unsub() -> None:
        with _lock:
            listeners = _subscribers.get(workspace_id)
            if not listeners:
                return
            try:
                listeners.remove(callback)
            except ValueError:
                pass

    return _unsub


def publish(event: dict[str, Any]) -> None:
    workspace_id = event.get("workspace_id")
    with _lock:
        targets = list(_subscribers.get(workspace_id, []))
        targets.extend(_subscribers.get(None, []))
    for cb in targets:
        try:
            cb(event)
        except Exception:  # noqa: BLE001 — never break writers for a bad listener
            pass


def clear_subscribers() -> None:
    with _lock:
        _subscribers.clear()


def log_activity(
    workspace_id: str,
    *,
    agent_type: str,
    action: str,
    summary: str,
    level: str = "info",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = OpsActivity(
            id=new_id("oay"),
            workspace_id=workspace_id,
            agent_type=agent_type,
            action=action,
            summary=summary,
            detail_json=json.dumps(detail or {}, default=str),
            level=level,
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        event = _serialize(row)
    publish(event)
    return event


def get_recent_activity(workspace_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(OpsActivity)
            .where(OpsActivity.workspace_id == workspace_id)
            .order_by(OpsActivity.created_at.desc())
            .limit(limit)
        ).all()
        return [_serialize(r) for r in rows]


def _serialize(row: OpsActivity) -> dict[str, Any]:
    detail = None
    if row.detail_json:
        try:
            detail = json.loads(row.detail_json)
        except json.JSONDecodeError:
            detail = None
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "agent_type": row.agent_type,
        "action": row.action,
        "summary": row.summary,
        "detail": detail,
        "level": row.level,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
