"""Social posts listing and draft creation."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import OpsSocialPost
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def create_post(
    workspace_id: str,
    *,
    content: str,
    status: str = "draft",
    platform: str = "twitter",
    tweet_id: str | None = None,
) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = OpsSocialPost(
            id=new_id("osp"),
            workspace_id=workspace_id,
            platform=platform,
            content=content,
            status=status,
            tweet_id=tweet_id,
            published_at=utcnow() if status == "published" else None,
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


def list_posts(
    workspace_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        q = select(OpsSocialPost).where(OpsSocialPost.workspace_id == workspace_id)
        if status:
            q = q.where(OpsSocialPost.status == status)
        rows = session.scalars(q.order_by(OpsSocialPost.created_at.desc()).limit(limit)).all()
        return [_serialize(r) for r in rows]


def _serialize(row: OpsSocialPost) -> dict[str, Any]:
    engagement = None
    if row.engagement_json:
        try:
            engagement = json.loads(row.engagement_json)
        except json.JSONDecodeError:
            engagement = None
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "platform": row.platform,
        "content": row.content,
        "status": row.status,
        "tweet_id": row.tweet_id,
        "engagement": engagement,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }
