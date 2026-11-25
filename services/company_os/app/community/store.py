"""Persistence for CommunityPlan / CommunityServer / CommunityPerson."""

from __future__ import annotations

import json
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import CommunityPerson, CommunityPlan, CommunityServer
from app.state import db


def _dumps(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, default=str)


def _loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return {} if default is None else default
    try:
        return json.loads(raw)
    except Exception:
        return {} if default is None else default


def plan_to_dict(row: CommunityPlan) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "venture_id": row.venture_id,
        "research_bundle_id": row.research_bundle_id,
        "schema_version": row.schema_version,
        "status": row.status,
        "verdict": row.verdict,
        "thesis_score": row.thesis_score,
        "thesis": _loads(row.thesis_json),
        "objectives": _loads(row.objectives_json) or None,
        "identity": _loads(row.identity_json) or None,
        "program_30d": _loads(row.program_json) or None,
        "work_orders": _loads(row.work_orders_json, []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def server_to_dict(row: CommunityServer) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "plan_id": row.plan_id,
        "guild_id": row.guild_id,
        "guild_name": row.guild_name,
        "connected_account_id": row.connected_account_id,
        "status": row.status,
        "provision": _loads(row.provision_json),
        "meta": _loads(row.meta_json),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def save_plan(
    *,
    workspace_id: str,
    plan: dict[str, Any],
    plan_id: str | None = None,
) -> dict[str, Any]:
    now = utcnow()
    thesis = plan.get("thesis") or {}
    with db()() as session:
        row = None
        if plan_id:
            row = session.get(CommunityPlan, plan_id)
        if row is None:
            row = CommunityPlan(
                id=plan_id or new_id("cplan"),
                workspace_id=workspace_id,
                created_at=now,
            )
            session.add(row)
        row.venture_id = plan.get("venture_id")
        row.research_bundle_id = plan.get("research_bundle_id")
        row.schema_version = str(plan.get("schema_version") or "community.plan.v1")
        row.status = str(plan.get("status") or "draft")
        row.verdict = str(thesis.get("verdict") or plan.get("verdict") or "")
        row.thesis_score = int(thesis.get("thesis_score") or plan.get("thesis_score") or 0)
        row.thesis_json = _dumps(thesis)
        row.objectives_json = _dumps(plan.get("objectives"))
        row.identity_json = _dumps(plan.get("identity"))
        row.program_json = _dumps(plan.get("program_30d"))
        row.work_orders_json = _dumps(plan.get("work_orders") or [])
        row.updated_at = now
        session.commit()
        session.refresh(row)
        return plan_to_dict(row)


def latest_plan(workspace_id: str) -> dict[str, Any] | None:
    with db()() as session:
        row = session.scalars(
            select(CommunityPlan)
            .where(CommunityPlan.workspace_id == workspace_id)
            .order_by(CommunityPlan.created_at.desc())
            .limit(1)
        ).first()
        return plan_to_dict(row) if row else None


def get_plan(plan_id: str) -> dict[str, Any] | None:
    with db()() as session:
        row = session.get(CommunityPlan, plan_id)
        return plan_to_dict(row) if row else None


def upsert_server(
    *,
    workspace_id: str,
    guild_id: str,
    plan_id: str | None = None,
    guild_name: str | None = None,
    connected_account_id: str | None = None,
    status: str = "connected",
    provision: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utcnow()
    with db()() as session:
        row = session.scalars(
            select(CommunityServer).where(
                CommunityServer.workspace_id == workspace_id,
                CommunityServer.guild_id == guild_id,
            )
        ).first()
        if row is None:
            row = CommunityServer(
                id=new_id("cserv"),
                workspace_id=workspace_id,
                guild_id=guild_id,
                created_at=now,
            )
            session.add(row)
        if plan_id:
            row.plan_id = plan_id
        if guild_name is not None:
            row.guild_name = guild_name
        if connected_account_id is not None:
            row.connected_account_id = connected_account_id
        row.status = status
        if provision is not None:
            row.provision_json = _dumps(provision)
        if meta is not None:
            row.meta_json = _dumps(meta)
        row.updated_at = now
        session.commit()
        session.refresh(row)
        return server_to_dict(row)


def list_servers(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = session.scalars(
            select(CommunityServer)
            .where(CommunityServer.workspace_id == workspace_id)
            .order_by(CommunityServer.updated_at.desc())
        ).all()
        return [server_to_dict(r) for r in rows]


def upsert_person(
    *,
    workspace_id: str,
    discord_user_id: str,
    display_name: str | None = None,
    stage: str = "member",
    server_id: str | None = None,
    gtm_contact_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = utcnow()
    with db()() as session:
        row = session.scalars(
            select(CommunityPerson).where(
                CommunityPerson.workspace_id == workspace_id,
                CommunityPerson.discord_user_id == discord_user_id,
            )
        ).first()
        if row is None:
            row = CommunityPerson(
                id=new_id("cper"),
                workspace_id=workspace_id,
                discord_user_id=discord_user_id,
                created_at=now,
            )
            session.add(row)
        if display_name is not None:
            row.display_name = display_name
        row.stage = stage
        if server_id:
            row.server_id = server_id
        if gtm_contact_id:
            row.gtm_contact_id = gtm_contact_id
        if meta is not None:
            row.meta_json = _dumps(meta)
        row.updated_at = now
        session.commit()
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "discord_user_id": row.discord_user_id,
            "display_name": row.display_name,
            "stage": row.stage,
            "gtm_contact_id": row.gtm_contact_id,
        }


def funnel_stub(workspace_id: str) -> dict[str, Any]:
    """Honest empty funnel until gateway/events exist."""
    with db()() as session:
        people = session.scalars(
            select(CommunityPerson).where(CommunityPerson.workspace_id == workspace_id)
        ).all()
    stages = {
        "visitor": 0,
        "member": 0,
        "activated": 0,
        "product_user": 0,
        "pqcl": 0,
        "customer": 0,
        "referral": 0,
    }
    for p in people:
        st = str(p.stage or "member")
        if st in stages:
            stages[st] += 1
        else:
            stages["member"] += 1
    return {
        "stages": stages,
        "note": (
            "Funnel counts stay empty/stub until Discord gateway events or "
            "honest member polling is wired. DISCORDBOT has 0 Composio triggers."
        ),
        "person_rows": len(people),
    }
