"""Workspace media directories and license allowlists."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import MarketingAsset
from app.marketing.media.quality import looks_synthetic
from app.state import db

REPO_ROOT = Path(__file__).resolve().parents[5]
MEDIA_ROOT = REPO_ROOT / "data" / "marketing_media"

BLOCKED_WEIGHTS = (
    "flux.1-dev",
    "flux1-dev",
    "flux_dev",
    "instantid",
    "pulid",
    "ip-adapter-faceid",
    "ipadapter-faceid",
    "photomaker-v2",
    "photomaker_v2",
    "reactor",
    "insightface",
    "antelopev2",
    "supir",
    "bria-rmbg",
    "rmbg-1.4",
    "ic-light-v2",
    "iclight_v2",
)


def media_dir(workspace_id: str, *parts: str) -> Path:
    path = MEDIA_ROOT / workspace_id
    for part in parts:
        path = path / part
    path.mkdir(parents=True, exist_ok=True)
    return path


def refuse_blocked_weights(text: str | None) -> str | None:
    hay = (text or "").lower()
    for token in BLOCKED_WEIGHTS:
        if token in hay:
            return f"license_blocked:{token}"
    return None


def serialize_asset(row: MarketingAsset) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "kind": row.kind,
        "source": row.source,
        "title": row.title,
        "path": row.path,
        "mime": row.mime,
        "width": row.width,
        "height": row.height,
        "duration_ms": row.duration_ms,
        "license": _loads(row.license_json),
        "meta": _loads(row.meta_json),
        "review_status": row.review_status,
        "url": f"/api/ops/marketing/assets/{row.id}/file",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def insert_asset(
    *,
    workspace_id: str,
    kind: str,
    path: str,
    source: str,
    mime: str = "image/png",
    title: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_ms: int | None = None,
    license_info: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    review_status: str = "pending",
) -> dict[str, Any]:
    now = utcnow()
    factory = db()
    with factory() as session:
        row = MarketingAsset(
            id=new_id("mas"),
            workspace_id=workspace_id,
            kind=kind,
            source=source,
            title=title,
            path=path,
            mime=mime,
            width=width,
            height=height,
            duration_ms=duration_ms,
            license_json=json.dumps(license_info or {}, ensure_ascii=False),
            meta_json=json.dumps(meta or {}, ensure_ascii=False),
            review_status=review_status,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return serialize_asset(row)


def list_assets(workspace_id: str, *, kind: str | None = None, limit: int = 80) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        stmt = select(MarketingAsset).where(MarketingAsset.workspace_id == workspace_id)
        if kind:
            stmt = stmt.where(MarketingAsset.kind == kind)
        stmt = stmt.order_by(MarketingAsset.created_at.desc()).limit(max(1, min(limit, 200)))
        return [serialize_asset(row) for row in session.scalars(stmt).all()]


def get_asset(workspace_id: str, asset_id: str) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.get(MarketingAsset, asset_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return serialize_asset(row)


def set_review(workspace_id: str, asset_id: str, status: str) -> dict[str, Any]:
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError("invalid_review_status")
    factory = db()
    with factory() as session:
        row = session.get(MarketingAsset, asset_id)
        if row is None or row.workspace_id != workspace_id:
            raise ValueError("asset_not_found")
        row.review_status = status
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return serialize_asset(row)


def latest_of_kind(workspace_id: str, kind: str) -> dict[str, Any] | None:
    rows = list_assets(workspace_id, kind=kind, limit=40)
    for row in rows:
        if row.get("review_status") == "rejected":
            continue
        path = row.get("path") or ""
        if not path or not Path(path).is_file():
            continue
        if kind in {"founder", "product"} and looks_synthetic(path, kind=kind):
            continue
        if kind == "logo" and looks_synthetic(path, kind="logo"):
            continue
        return row
    return None


def delete_asset(workspace_id: str, asset_id: str, *, unlink: bool = False) -> bool:
    factory = db()
    with factory() as session:
        row = session.get(MarketingAsset, asset_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        path = row.path
        session.delete(row)
        session.commit()
    if unlink and path:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    return True


def purge_synthetic(workspace_id: str) -> dict[str, int]:
    """Drop drawn placeholders and stills/clips that were built from them."""
    removed = {"founder": 0, "product": 0, "logo": 0, "generated_image": 0, "generated_video": 0}
    rows = list_assets(workspace_id, limit=200)
    for row in rows:
        kind = str(row.get("kind") or "")
        path = str(row.get("path") or "")
        drop = False
        if kind in {"founder", "product", "logo"} and looks_synthetic(path, kind=kind):
            drop = True
        elif kind in {"generated_image", "generated_video"}:
            drop = True
        if drop and delete_asset(workspace_id, row["id"], unlink=True):
            removed[kind] = removed.get(kind, 0) + 1
    return removed
