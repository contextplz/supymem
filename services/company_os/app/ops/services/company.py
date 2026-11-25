"""Company config context for ops agents."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import OpsCompanyConfig, OpsDailyReport, OpsTask
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def get_config(workspace_id: str) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OpsCompanyConfig).where(OpsCompanyConfig.workspace_id == workspace_id)
        ).first()
        if row is None:
            return None
        return _serialize(row)


def upsert_config(workspace_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OpsCompanyConfig).where(OpsCompanyConfig.workspace_id == workspace_id)
        ).first()
        now = utcnow()
        if row is None:
            row = OpsCompanyConfig(
                id=new_id("opc"),
                workspace_id=workspace_id,
                name=fields.get("name") or "Company",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        for key in (
            "name",
            "mission",
            "vision",
            "description",
            "target_market",
            "value_prop",
            "website_url",
            "github_repo",
            "product_type",
            "industry",
            "timezone",
        ):
            if key in fields and fields[key] is not None:
                setattr(row, key, fields[key])
        if "daily_cycle_hour" in fields and fields["daily_cycle_hour"] is not None:
            row.daily_cycle_hour = int(fields["daily_cycle_hour"])
        if "pricing_model" in fields:
            row.pricing_model_json = _dumps(fields["pricing_model"])
        if "goals" in fields:
            row.goals_json = _dumps(fields["goals"])
        if "kpis" in fields:
            row.kpis_json = _dumps(fields["kpis"])
        row.updated_at = now
        session.commit()
        session.refresh(row)
        return _serialize(row)


def get_full_context(workspace_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        company = session.scalars(
            select(OpsCompanyConfig).where(OpsCompanyConfig.workspace_id == workspace_id)
        ).first()
        if company is None:
            return {"workspace_id": workspace_id}
        yesterday = session.scalars(
            select(OpsDailyReport)
            .where(OpsDailyReport.workspace_id == workspace_id)
            .order_by(OpsDailyReport.report_date.desc())
        ).first()
        todays = session.scalars(
            select(OpsTask)
            .where(OpsTask.workspace_id == workspace_id)
            .order_by(OpsTask.created_at.desc())
            .limit(20)
        ).all()
        return {
            "workspace_id": workspace_id,
            "company": {
                "name": company.name,
                "mission": company.mission,
                "vision": company.vision,
                "description": company.description,
                "target_market": company.target_market,
                "value_prop": company.value_prop,
            },
            "kpis": _loads(company.kpis_json) or {},
            "yesterday_summary": yesterday.evening_summary if yesterday else None,
            "todays_tasks": [
                {
                    "title": t.title,
                    "agent_type": t.agent_type,
                    "status": t.status,
                }
                for t in todays
            ],
        }


def build_context_prompt(context: dict[str, Any]) -> str:
    if not context or not context.get("company"):
        return "No company context available."
    company = context["company"]
    parts = [
        f"Company: {company.get('name')}",
        f"Mission: {company.get('mission') or ''}",
        f"Description: {company.get('description') or ''}",
        f"KPIs: {json.dumps(context.get('kpis') or {})}",
    ]
    if context.get("yesterday_summary"):
        parts.append(f"Yesterday: {context['yesterday_summary']}")
    tasks = context.get("todays_tasks") or []
    if tasks:
        parts.append("Today's tasks:")
        for t in tasks:
            parts.append(f"- [{t.get('agent_type')}] {t.get('title')} ({t.get('status')})")
    return "\n".join(parts)


def _serialize(row: OpsCompanyConfig) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "mission": row.mission,
        "vision": row.vision,
        "description": row.description,
        "target_market": row.target_market,
        "value_prop": row.value_prop,
        "pricing_model": _loads(row.pricing_model_json),
        "goals": _loads(row.goals_json),
        "kpis": _loads(row.kpis_json),
        "website_url": row.website_url,
        "github_repo": row.github_repo,
        "product_type": row.product_type,
        "industry": row.industry,
        "timezone": row.timezone,
        "daily_cycle_hour": row.daily_cycle_hour,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
