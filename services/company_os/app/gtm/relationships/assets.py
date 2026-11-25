"""OT-41: monthly SKU factory. Always stops at in_review until a human approves."""

from __future__ import annotations

import json
import re
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import func, select

from app.db.models import GtmArtifactSku, GtmInterview
from app.gtm.relationships.skus import SkuAttachError, attach_sku, match_sku
from app.state import db

_INTERVIEWED_N = re.compile(r"interviewed\s+(\d+)", re.IGNORECASE)


class AssetFactoryError(ValueError):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def catalogue_honesty(workspace_id: str, body_md: str) -> dict[str, Any]:
    """Refuse fabricated interview counts (I-126-12 honesty on numbers)."""

    flags: list[str] = []
    with db()() as session:
        n_interviews = session.scalar(
            select(func.count()).select_from(GtmInterview).where(
                GtmInterview.workspace_id == workspace_id
            )
        ) or 0
    for match in _INTERVIEWED_N.finditer(body_md or ""):
        claimed = int(match.group(1))
        if claimed > int(n_interviews):
            flags.append("fabricated_interview_count")
    return {"blocked": bool(flags), "flags": flags, "interview_count": int(n_interviews)}


def mint_monthly_sku(
    workspace_id: str,
    *,
    period: str,
    kind: str = "benchmark",
    title: str,
    body_md: str,
    sku_key: str | None = None,
    match_tags: list[str] | None = None,
) -> dict[str, Any]:
    honesty = catalogue_honesty(workspace_id, body_md)
    if honesty["blocked"]:
        raise AssetFactoryError("catalogue_honesty_failed")
    if kind not in {"benchmark", "teardown", "quote_card", "calculator"}:
        raise AssetFactoryError("unknown_sku_kind", 400)
    key = sku_key or f"{period}-{kind}-01"
    now = utcnow()
    with db()() as session:
        existing = session.scalar(
            select(GtmArtifactSku).where(
                GtmArtifactSku.workspace_id == workspace_id,
                GtmArtifactSku.sku_key == key,
            )
        )
        if existing is not None:
            raise AssetFactoryError("sku_exists")
        row = GtmArtifactSku(
            id=new_id("gtmsku"),
            workspace_id=workspace_id,
            sku_key=key,
            kind=kind,
            period=period,
            title=title,
            body_md=body_md,
            match_tags_json=json.dumps(match_tags or []),
            status="in_review",
            created_at=now,
            approved_at=None,
        )
        session.add(row)
        session.commit()
        return {
            "id": row.id,
            "sku_key": row.sku_key,
            "status": row.status,
            "honesty": honesty,
        }


def approve_sku(workspace_id: str, sku_key: str, *, reviewed: bool) -> dict[str, Any]:
    if not reviewed:
        raise SkuAttachError("sku_not_reviewed")
    with db()() as session:
        row = session.scalar(
            select(GtmArtifactSku).where(
                GtmArtifactSku.workspace_id == workspace_id,
                GtmArtifactSku.sku_key == sku_key,
            )
        )
        if row is None:
            raise SkuAttachError("sku_not_approved")
        row.status = "approved"
        row.approved_at = utcnow()
        session.commit()
        return {"ok": True, "sku_key": row.sku_key, "status": row.status}


__all__ = [
    "AssetFactoryError",
    "SkuAttachError",
    "approve_sku",
    "attach_sku",
    "catalogue_honesty",
    "match_sku",
    "mint_monthly_sku",
]
