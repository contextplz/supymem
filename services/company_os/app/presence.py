"""Workspace presence: last-seen heartbeats for 2–3 people on one venture."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import PresenceHeartbeat
from app.state import db
from core.clock import utcnow
from core.ids import new_id

LIVE_SECONDS = 90
REDIS_TTL = 60


def _redis():
    from app.core.local_worker import redis_url

    url = redis_url()
    if not url:
        return None
    try:
        import redis

        return redis.Redis.from_url(url)
    except Exception:
        return None


def beat(
    workspace_id: str,
    person_id: str,
    *,
    display_name: str | None = None,
    surface: str | None = None,
) -> dict[str, object]:
    now = utcnow()
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(PresenceHeartbeat).where(
                PresenceHeartbeat.workspace_id == workspace_id,
                PresenceHeartbeat.person_id == person_id,
            )
        ).first()
        if row is None:
            row = PresenceHeartbeat(
                id=new_id("pre"),
                workspace_id=workspace_id,
                person_id=person_id,
                display_name=display_name,
                surface=surface,
                last_seen_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                row = session.scalars(
                    select(PresenceHeartbeat).where(
                        PresenceHeartbeat.workspace_id == workspace_id,
                        PresenceHeartbeat.person_id == person_id,
                    )
                ).first()
                if row is None:
                    raise
                row.display_name = display_name or row.display_name
                row.surface = surface or row.surface
                row.last_seen_at = now
                session.commit()
        else:
            row.display_name = display_name or row.display_name
            row.surface = surface or row.surface
            row.last_seen_at = now
            session.commit()
    client = _redis()
    if client is not None:
        try:
            key = f"presence:{workspace_id}"
            client.hset(
                key,
                person_id,
                json.dumps(
                    {
                        "person_id": person_id,
                        "display_name": display_name or person_id,
                        "surface": surface,
                    }
                ),
            )
            client.expire(key, REDIS_TTL)
        except Exception:
            pass
    return list_live(workspace_id)


def list_live(workspace_id: str) -> dict[str, object]:
    cutoff = utcnow() - timedelta(seconds=LIVE_SECONDS)
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(PresenceHeartbeat)
            .where(
                PresenceHeartbeat.workspace_id == workspace_id,
                PresenceHeartbeat.last_seen_at >= cutoff,
            )
            .order_by(PresenceHeartbeat.last_seen_at.desc())
        ).all()
        items = [
            {
                "person_id": r.person_id,
                "display_name": r.display_name or r.person_id,
                "surface": r.surface,
                "last_seen_at": r.last_seen_at.isoformat() + "Z",
            }
            for r in rows
        ]
    return {"items": items, "live_seconds": LIVE_SECONDS}

from app.db.models import Person

def presence_display_name(person_id: str) -> str | None:
    factory = db()
    with factory() as session:
        person = session.get(Person, person_id)
        return person.display_name if person else None
