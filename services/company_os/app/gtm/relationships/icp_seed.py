"""Workspace ICP from the YAML template library — not a shared people graph (OT-25)."""

from __future__ import annotations

import json
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import GtmMarketProfile
from app.gtm.relationships.icp import load_icp
from app.state import db


def seed_icp_template(workspace_id: str) -> None:
    """Copy template keys into *this* workspace’s market profile. Idempotent."""

    cfg = load_icp()
    now = utcnow()
    with db()() as session:
        existing = session.scalar(
            select(GtmMarketProfile)
            .where(
                GtmMarketProfile.workspace_id == workspace_id,
                GtmMarketProfile.status == "active",
            )
            .order_by(GtmMarketProfile.version.desc())
        )
        if existing is not None:
            return
        session.add(
            GtmMarketProfile(
                id=new_id("gtmmp"),
                workspace_id=workspace_id,
                version=1,
                name=str(cfg.get("name") or "workspace_icp"),
                status="active",
                seller_json="{}",
                product_json="{}",
                segment_json=json.dumps(
                    {
                        "who": "",
                "roles": ((cfg.get("include") or {}).get("roles") or []),
                "template": cfg.get("name"),
                    }
                ),
                constraints_json="{}",
                evidence_json="[]",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def get_segment(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        row = session.scalar(
            select(GtmMarketProfile)
            .where(
                GtmMarketProfile.workspace_id == workspace_id,
                GtmMarketProfile.status.in_(["active", "draft"]),
            )
            .order_by(GtmMarketProfile.version.desc())
        )
        if row is None:
            return {}
        try:
            return json.loads(row.segment_json or "{}")
        except json.JSONDecodeError:
            return {}


def save_segment(workspace_id: str, segment: dict[str, Any], *, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    now = utcnow()
    with db()() as session:
        row = session.scalar(
            select(GtmMarketProfile)
            .where(GtmMarketProfile.workspace_id == workspace_id)
            .order_by(GtmMarketProfile.version.desc())
        )
        if row is None:
            row = GtmMarketProfile(
                id=new_id("gtmmp"),
                workspace_id=workspace_id,
                version=1,
                name="workspace_icp",
                status="active",
                seller_json="{}",
                product_json="{}",
                segment_json=json.dumps(segment),
                constraints_json=json.dumps(constraints or {}),
                evidence_json="[]",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.segment_json = json.dumps(segment)
            if constraints is not None:
                row.constraints_json = json.dumps(constraints)
            row.updated_at = now
            row.status = "active"
        session.commit()
        return json.loads(row.segment_json)
