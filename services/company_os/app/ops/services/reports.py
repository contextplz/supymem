"""Daily morning/evening report helpers."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select

from app.db.models import OpsDailyReport
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def get_or_create_daily_report(workspace_id: str, report_date: date) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OpsDailyReport).where(
                OpsDailyReport.workspace_id == workspace_id,
                OpsDailyReport.report_date == report_date,
            )
        ).first()
        if row is None:
            now = utcnow()
            row = OpsDailyReport(
                id=new_id("odr"),
                workspace_id=workspace_id,
                report_date=report_date,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
        return _serialize(row)


def save_morning_plan(
    workspace_id: str,
    report_date: date,
    plan: str,
    *,
    tasks_planned: int = 0,
) -> dict[str, Any]:
    get_or_create_daily_report(workspace_id, report_date)
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OpsDailyReport).where(
                OpsDailyReport.workspace_id == workspace_id,
                OpsDailyReport.report_date == report_date,
            )
        ).one()
        row.morning_plan = plan
        row.tasks_planned = tasks_planned
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return _serialize(row)


def save_evening_summary(
    workspace_id: str,
    report_date: date,
    *,
    summary: str,
    insights: list[str] | None = None,
    tasks_completed: int = 0,
    tasks_failed: int = 0,
    metrics_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    get_or_create_daily_report(workspace_id, report_date)
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OpsDailyReport).where(
                OpsDailyReport.workspace_id == workspace_id,
                OpsDailyReport.report_date == report_date,
            )
        ).one()
        row.evening_summary = summary
        row.insights_json = json.dumps(insights or [])
        row.tasks_completed = tasks_completed
        row.tasks_failed = tasks_failed
        row.metrics_snapshot_json = json.dumps(metrics_snapshot or {}, default=str)
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return _serialize(row)


def get_daily_report(workspace_id: str, report_date: date | None = None) -> dict[str, Any] | None:
    target = report_date or date.today()
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OpsDailyReport).where(
                OpsDailyReport.workspace_id == workspace_id,
                OpsDailyReport.report_date == target,
            )
        ).first()
        return _serialize(row) if row else None


def _serialize(row: OpsDailyReport) -> dict[str, Any]:
    insights: list[str] = []
    metrics: dict[str, Any] = {}
    if row.insights_json:
        try:
            insights = list(json.loads(row.insights_json))
        except json.JSONDecodeError:
            insights = []
    if row.metrics_snapshot_json:
        try:
            metrics = dict(json.loads(row.metrics_snapshot_json))
        except json.JSONDecodeError:
            metrics = {}
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "report_date": row.report_date.isoformat(),
        "morning_plan": row.morning_plan,
        "evening_summary": row.evening_summary,
        "tasks_planned": row.tasks_planned,
        "tasks_completed": row.tasks_completed,
        "tasks_failed": row.tasks_failed,
        "insights": insights,
        "metrics_snapshot": metrics,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
