"""SKU catalogue attach (OT-21). Attach fails closed until approved + reviewed."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import GtmArtifactSku, GtmContact
from app.gtm.relationships.scope import gtm_get
from app.state import db


class SkuAttachError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def match_sku(workspace_id: str, contact_id: str) -> str | None:
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            return None
        rows = list(
            session.scalars(
                select(GtmArtifactSku).where(
                    GtmArtifactSku.workspace_id == workspace_id,
                    GtmArtifactSku.status == "approved",
                )
            )
        )
        if not rows:
            return None
        return rows[0].sku_key


def attach_sku(
    workspace_id: str,
    contact_id: str,
    sku_key: str,
    *,
    reviewed: bool,
) -> dict[str, Any]:
    if not reviewed:
        raise SkuAttachError("sku_not_reviewed")
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise SkuAttachError("contact_not_found")
        sku = session.scalar(
            select(GtmArtifactSku).where(
                GtmArtifactSku.workspace_id == workspace_id,
                GtmArtifactSku.sku_key == sku_key,
            )
        )
        if sku is None or sku.status != "approved":
            raise SkuAttachError("sku_not_approved")
        contact.sku_key = sku.sku_key
        session.commit()
        return {"ok": True, "sku_key": sku.sku_key, "tags": json.loads(sku.match_tags_json or "[]")}
