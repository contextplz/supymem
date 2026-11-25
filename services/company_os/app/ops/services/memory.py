"""Workspace memory — keyword search; optional Chroma (lazy)."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import or_, select

from app.db.models import OpsMemory
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)


def store_memory(
    workspace_id: str,
    *,
    category: str,
    title: str,
    content: str,
    source: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    chroma_id = new_id("omc")
    factory = db()
    with factory() as session:
        row = OpsMemory(
            id=new_id("omm"),
            workspace_id=workspace_id,
            category=category,
            title=title,
            content=content,
            source=source,
            tags_json=json.dumps(tags or []),
            chroma_id=chroma_id,
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        serialized = _serialize(row)
    _maybe_chroma_add(chroma_id, content, metadata={"title": title, "category": category})
    return serialized


def list_memories(
    workspace_id: str,
    *,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        q = select(OpsMemory).where(OpsMemory.workspace_id == workspace_id)
        if category:
            q = q.where(OpsMemory.category == category)
        q = q.order_by(OpsMemory.created_at.desc()).limit(limit)
        return [_serialize(r) for r in session.scalars(q).all()]


def search_memory(workspace_id: str, query: str, *, n_results: int = 10) -> list[dict[str, Any]]:
    """Keyword search over title/content. Good enough for local; Chroma optional."""
    tokens = [t for t in query.lower().split() if t]
    factory = db()
    with factory() as session:
        q = select(OpsMemory).where(OpsMemory.workspace_id == workspace_id)
        if tokens:
            clauses = []
            for tok in tokens:
                like = f"%{tok}%"
                clauses.append(OpsMemory.title.ilike(like))
                clauses.append(OpsMemory.content.ilike(like))
            q = q.where(or_(*clauses))
        rows = session.scalars(q.order_by(OpsMemory.created_at.desc()).limit(n_results)).all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "content": r.content,
                "category": r.category,
                "score": 1.0,
            }
            for r in rows
        ]


def _maybe_chroma_add(chroma_id: str, content: str, metadata: dict[str, Any]) -> None:
    try:
        import chromadb  # type: ignore[import-untyped]
    except ImportError:
        return
    try:
        client = chromadb.Client()
        collection = client.get_or_create_collection("ops_memory")
        collection.add(ids=[chroma_id], documents=[content], metadatas=[metadata])
    except Exception:  # noqa: BLE001
        log.debug("chroma write skipped", exc_info=True)


def _serialize(row: OpsMemory) -> dict[str, Any]:
    tags: list[str] = []
    if row.tags_json:
        try:
            tags = list(json.loads(row.tags_json))
        except json.JSONDecodeError:
            tags = []
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "category": row.category,
        "title": row.title,
        "content": row.content,
        "source": row.source,
        "tags": tags,
        "chroma_id": row.chroma_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
