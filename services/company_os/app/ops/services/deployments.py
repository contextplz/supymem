"""Deployment history — sandbox records intent only."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import OpsDeployment
from app.ops import config as ops_config
from app.ops.sandbox import SandboxBlocked, assert_render_write
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def record_deployment(
    workspace_id: str,
    *,
    service_name: str,
    status: str = "pending",
    trigger_source: str = "api",
    external_id: str | None = None,
    summary: str | None = None,
    health_check_url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        now = utcnow()
        row = OpsDeployment(
            id=new_id("odp"),
            workspace_id=workspace_id,
            service_name=service_name,
            status=status,
            trigger_source=trigger_source,
            external_id=external_id,
            health_check_url=health_check_url,
            summary=summary,
            metadata_json=json.dumps(metadata or {}, default=str),
            created_at=now,
            updated_at=now,
            finished_at=now if status in {"live", "failed", "sandbox_intent"} else None,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


def list_deployments(workspace_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(OpsDeployment)
            .where(OpsDeployment.workspace_id == workspace_id)
            .order_by(OpsDeployment.created_at.desc())
            .limit(limit)
        ).all()
        return [_serialize(r) for r in rows]


def latest_deployments(workspace_id: str) -> list[dict[str, Any]]:
    """Most recent deploy per service_name."""
    rows = list_deployments(workspace_id, limit=200)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        name = row["service_name"]
        if name in seen:
            continue
        seen.add(name)
        out.append(row)
    return out


def trigger_deployment(
    workspace_id: str,
    *,
    service_name: str,
    health_check_url: str | None = None,
) -> dict[str, Any]:
    if ops_config.sandbox_mode():
        return record_deployment(
            workspace_id,
            service_name=service_name,
            status="sandbox_intent",
            trigger_source="api",
            summary=f"Sandbox deploy intent for {service_name}",
            health_check_url=health_check_url,
        )
    try:
        assert_render_write()
    except SandboxBlocked:
        return record_deployment(
            workspace_id,
            service_name=service_name,
            status="sandbox_intent",
            trigger_source="api",
            summary=f"Blocked deploy for {service_name}",
            health_check_url=health_check_url,
        )
    return record_deployment(
        workspace_id,
        service_name=service_name,
        status="triggered",
        trigger_source="api",
        external_id="render_pending",
        summary=f"Triggered deploy for {service_name}",
        health_check_url=health_check_url,
    )


def _serialize(row: OpsDeployment) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if row.metadata_json:
        try:
            meta = dict(json.loads(row.metadata_json))
        except json.JSONDecodeError:
            meta = {}
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "service_name": row.service_name,
        "status": row.status,
        "trigger_source": row.trigger_source,
        "external_id": row.external_id,
        "commit_sha": row.commit_sha,
        "health_check_url": row.health_check_url,
        "health_check_passed": bool(row.health_check_passed)
        if row.health_check_passed is not None
        else None,
        "summary": row.summary,
        "metadata": meta,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
