"""Ops task + agent-run lifecycle."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.db.models import OpsAgentRun, OpsTask
from app.ops import VALID_AGENT_TYPES
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def create_task(
    workspace_id: str,
    *,
    title: str,
    agent_type: str,
    description: str | None = None,
    priority: int = 3,
    source: str = "orchestrator",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if agent_type not in VALID_AGENT_TYPES:
        raise ValueError(f"invalid agent_type: {agent_type}")
    factory = db()
    with factory() as session:
        now = utcnow()
        row = OpsTask(
            id=new_id("opt"),
            workspace_id=workspace_id,
            title=title,
            description=description,
            agent_type=agent_type,
            priority=priority,
            status="pending",
            source=source,
            metadata_json=json.dumps(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


def get_task(workspace_id: str, task_id: str) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.get(OpsTask, task_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return _serialize(row)


def list_tasks(
    workspace_id: str,
    *,
    status: str | None = None,
    agent_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        q = select(OpsTask).where(OpsTask.workspace_id == workspace_id)
        if status:
            q = q.where(OpsTask.status == status)
        if agent_type:
            q = q.where(OpsTask.agent_type == agent_type)
        q = q.order_by(OpsTask.created_at.desc()).limit(limit)
        return [_serialize(r) for r in session.scalars(q).all()]


def update_task_status(
    workspace_id: str,
    task_id: str,
    status: str,
    *,
    result_summary: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.get(OpsTask, task_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        row.status = status
        if result_summary is not None:
            row.result_summary = result_summary
        if error_message is not None:
            row.error_message = error_message
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return _serialize(row)


def create_agent_run(
    workspace_id: str,
    agent_type: str,
    *,
    task_id: str | None = None,
    input_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = OpsAgentRun(
            id=new_id("opa"),
            workspace_id=workspace_id,
            task_id=task_id,
            agent_type=agent_type,
            status="running",
            input_context_json=json.dumps(input_context or {}, default=str),
            started_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize_run(row)


def finish_agent_run(
    workspace_id: str,
    run_id: str,
    status: str,
    *,
    output: dict[str, Any] | None = None,
    duration_secs: float | None = None,
) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.get(OpsAgentRun, run_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        row.status = status
        row.output_json = json.dumps(output or {}, default=str)
        row.duration_secs = duration_secs
        row.ended_at = utcnow()
        session.commit()
        session.refresh(row)
        return _serialize_run(row)


def get_tasks_today_summary(workspace_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        start = datetime.combine(utcnow().date(), datetime.min.time())
        rows = session.scalars(
            select(OpsTask).where(
                OpsTask.workspace_id == workspace_id,
                OpsTask.created_at >= start,
            )
        ).all()
        total = len(rows)
        completed = sum(1 for r in rows if r.status == "completed")
        pending = sum(1 for r in rows if r.status in {"pending", "in_progress"})
        failed = sum(1 for r in rows if r.status == "failed")
        active = sorted({r.agent_type for r in rows if r.status == "in_progress"})
        return {
            "tasks_today_total": total,
            "tasks_today_completed": completed,
            "tasks_today_pending": pending,
            "tasks_today_failed": failed,
            "active_agents": active,
        }


def agent_status(workspace_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        start = datetime.combine(utcnow().date(), datetime.min.time())
        out: list[dict[str, Any]] = []
        for agent_type in VALID_AGENT_TYPES:
            last = session.scalars(
                select(OpsAgentRun)
                .where(
                    OpsAgentRun.workspace_id == workspace_id,
                    OpsAgentRun.agent_type == agent_type,
                )
                .order_by(OpsAgentRun.started_at.desc())
            ).first()
            today_count = session.scalar(
                select(func.count())
                .select_from(OpsTask)
                .where(
                    OpsTask.workspace_id == workspace_id,
                    OpsTask.agent_type == agent_type,
                    OpsTask.created_at >= start,
                )
            ) or 0
            total_count = session.scalar(
                select(func.count())
                .select_from(OpsTask)
                .where(
                    OpsTask.workspace_id == workspace_id,
                    OpsTask.agent_type == agent_type,
                )
            ) or 0
            out.append(
                {
                    "agent_type": agent_type,
                    "last_run_at": last.started_at.isoformat() if last else None,
                    "last_run_status": last.status if last else None,
                    "tasks_today": int(today_count),
                    "tasks_total": int(total_count),
                }
            )
        return out


def execute_task(workspace_id: str, task_id: str) -> dict[str, Any]:
    """Load task, run the mapped agent, persist status + activity."""
    from app.ops.crew import run_agent_for_task
    from app.ops.services import activity as activity_svc
    from app.ops.services import company as company_svc

    task = get_task(workspace_id, task_id)
    if task is None:
        raise ValueError("task_not_found")
    update_task_status(workspace_id, task_id, "in_progress")
    context = company_svc.get_full_context(workspace_id)
    run = create_agent_run(
        workspace_id, task["agent_type"], task_id=task_id, input_context=context
    )
    try:
        result = run_agent_for_task(
            task["agent_type"],
            {
                "id": task["id"],
                "title": task["title"],
                "description": task.get("description"),
                "metadata": task.get("metadata") or {},
            },
            context,
        )
        status = "completed"
        summary = result.get("summary", "Task completed.")
        error = None
    except Exception as exc:  # noqa: BLE001 — surface as failed task
        status = "failed"
        summary = None
        error = str(exc)
        result = {}
    duration = result.get("duration_secs") if isinstance(result, dict) else None
    update_task_status(
        workspace_id,
        task_id,
        status,
        result_summary=summary,
        error_message=error,
    )
    finish_agent_run(
        workspace_id,
        run["id"],
        status,
        output=result if isinstance(result, dict) else {},
        duration_secs=duration,
    )
    activity_svc.log_activity(
        workspace_id,
        agent_type=task["agent_type"],
        action="task_completed" if status == "completed" else "task_failed",
        summary=summary or error or "No output",
        level="success" if status == "completed" else "error",
        detail=result if isinstance(result, dict) else None,
    )
    updated = get_task(workspace_id, task_id)
    assert updated is not None
    return updated


def _serialize(row: OpsTask) -> dict[str, Any]:
    meta = {}
    if row.metadata_json:
        try:
            meta = json.loads(row.metadata_json)
        except json.JSONDecodeError:
            meta = {}
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "title": row.title,
        "description": row.description,
        "agent_type": row.agent_type,
        "priority": row.priority,
        "status": row.status,
        "source": row.source,
        "result_summary": row.result_summary,
        "error_message": row.error_message,
        "metadata": meta,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_run(row: OpsAgentRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "task_id": row.task_id,
        "agent_type": row.agent_type,
        "status": row.status,
        "duration_secs": row.duration_secs,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
    }
