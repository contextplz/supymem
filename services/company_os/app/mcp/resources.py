"""MCP resources — charter, map, house style, engagement deliverables (41 §3)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import Charter, CompanyMap, Engagement, Venture
from app.state import db

RESOURCE_SPECS: list[dict[str, str]] = [
    {
        "uri": "companyos://workspace/{id}/charter",
        "name": "charter",
        "description": "Locked charter for a workspace, if one exists.",
    },
    {
        "uri": "companyos://workspace/{id}/map",
        "name": "map",
        "description": "Company map sections with confidence.",
    },
    {
        "uri": "companyos://workspace/{id}/house-style",
        "name": "house-style",
        "description": "Derived house style for a connected company.",
    },
    {
        "uri": "companyos://workspace/{id}/engagement/{eid}/deliverables",
        "name": "deliverables",
        "description": "Deliverables for one engagement.",
    },
]


def list_resources() -> list[dict[str, str]]:
    return list(RESOURCE_SPECS)


def _workspace_venture(workspace_id: str) -> Venture | None:
    factory = db()
    with factory() as session:
        return session.scalars(
            select(Venture).where(Venture.workspace_id == workspace_id)
        ).first()


def read_resource(uri: str) -> dict[str, Any]:
    raw = (uri or "").strip()
    if not raw.startswith("companyos://"):
        return {"error": {"code": "unknown_resource", "message": f"No resource {uri}"}}
    path = raw.removeprefix("companyos://")
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "workspace" and parts[2] == "charter":
        return {"uri": raw, "mime": "application/json", "body": _charter(parts[1])}
    if len(parts) >= 3 and parts[0] == "workspace" and parts[2] == "map":
        return {"uri": raw, "mime": "application/json", "body": _map(parts[1])}
    if len(parts) >= 3 and parts[0] == "workspace" and parts[2] == "house-style":
        return {"uri": raw, "mime": "application/json", "body": _house_style(parts[1])}
    if (
        len(parts) >= 5
        and parts[0] == "workspace"
        and parts[2] == "engagement"
        and parts[4] == "deliverables"
    ):
        return {
            "uri": raw,
            "mime": "application/json",
            "body": _deliverables(parts[1], parts[3]),
        }
    return {"error": {"code": "unknown_resource", "message": f"No resource {uri}"}}


def _charter(workspace_id: str) -> dict[str, Any]:
    ven = _workspace_venture(workspace_id)
    if ven is None:
        return {"workspace_id": workspace_id, "charter": None}
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(Charter)
            .where(Charter.venture_id == ven.id)
            .order_by(Charter.created_at.desc())
        ).first()
        if row is None:
            return {"workspace_id": workspace_id, "venture_id": ven.id, "charter": None}
        try:
            content = json.loads(row.content_json or "{}")
        except json.JSONDecodeError:
            content = {}
        return {
            "workspace_id": workspace_id,
            "venture_id": ven.id,
            "charter": content,
            "locked": bool(row.locked),
            "version": row.version,
        }


def _map(workspace_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(CompanyMap)
            .where(CompanyMap.workspace_id == workspace_id)
            .order_by(CompanyMap.version.desc())
        ).first()
        if row is None:
            return {"workspace_id": workspace_id, "map": None}
        try:
            content = json.loads(row.content_json or "{}")
        except json.JSONDecodeError:
            content = {}
        return {"workspace_id": workspace_id, "map": content, "version": row.version}


def _house_style(workspace_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(CompanyMap)
            .where(CompanyMap.workspace_id == workspace_id)
            .order_by(CompanyMap.version.desc())
        ).first()
        if row is None:
            return {"workspace_id": workspace_id, "house_style": None}
        try:
            style = json.loads(row.house_style_json or "{}")
        except json.JSONDecodeError:
            style = {}
        return {"workspace_id": workspace_id, "house_style": style or None}


def _deliverables(workspace_id: str, engagement_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.get(Engagement, engagement_id)
        if row is None or row.workspace_id != workspace_id:
            return {
                "workspace_id": workspace_id,
                "engagement_id": engagement_id,
                "deliverables": [],
            }
        try:
            items = json.loads(row.deliverables_json or "[]")
        except json.JSONDecodeError:
            items = []
        return {
            "workspace_id": workspace_id,
            "engagement_id": engagement_id,
            "deliverables": items,
        }
