"""Persistence for marketing plans and jobs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import MarketingJob, MarketingPlan
from app.state import db


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def serialize_plan(row: MarketingPlan) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "brief_id": row.brief_id,
        "horizon_days": row.horizon_days,
        "status": row.status,
        "strategy": _loads(row.strategy_json, {}),
        "constraints": _loads(row.constraints_json, {}),
        "source": row.source,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_job(row: MarketingJob) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "plan_id": row.plan_id,
        "kind": row.kind,
        "platform": row.platform,
        "title": row.title,
        "payload": _loads(row.payload_json, {}),
        "depends_on": _loads(row.depends_on_json, []),
        "status": row.status,
        "priority": row.priority,
        "scheduled_for": row.scheduled_for.isoformat() if row.scheduled_for else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "attempt_count": row.attempt_count,
        "executor": row.executor,
        "result": _loads(row.result_json, {}),
        "proof": _loads(row.proof_json, {}),
        "block_reason": row.block_reason,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def active_plan(workspace_id: str) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(MarketingPlan)
            .where(
                MarketingPlan.workspace_id == workspace_id,
                MarketingPlan.status == "active",
            )
            .order_by(MarketingPlan.created_at.desc())
        ).first()
        return serialize_plan(row) if row else None


def get_plan(workspace_id: str, plan_id: str) -> dict[str, Any] | None:
    factory = db()
    with factory() as session:
        row = session.get(MarketingPlan, plan_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return serialize_plan(row)


def save_plan(
    *,
    workspace_id: str,
    strategy: dict[str, Any],
    constraints: dict[str, Any],
    brief_id: str | None,
    horizon_days: int,
    source: str = "strategist",
    activate: bool = True,
    starts_at: datetime | None = None,
) -> dict[str, Any]:
    now = utcnow()
    start = starts_at or now
    ends = start + timedelta(days=max(1, horizon_days))
    factory = db()
    with factory() as session:
        if activate:
            for existing in session.scalars(
                select(MarketingPlan).where(
                    MarketingPlan.workspace_id == workspace_id,
                    MarketingPlan.status == "active",
                )
            ):
                existing.status = "archived"
                existing.updated_at = now
        row = MarketingPlan(
            id=new_id("mpl"),
            workspace_id=workspace_id,
            brief_id=brief_id,
            horizon_days=horizon_days,
            status="active" if activate else "draft",
            strategy_json=json.dumps(strategy, ensure_ascii=False),
            constraints_json=json.dumps(constraints, ensure_ascii=False),
            source=source,
            starts_at=start,
            ends_at=ends,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return serialize_plan(row)


def insert_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not jobs:
        return []
    now = utcnow()
    factory = db()
    out: list[dict[str, Any]] = []
    with factory() as session:
        for spec in jobs:
            row = MarketingJob(
                id=spec.get("id") or new_id("mjb"),
                workspace_id=spec["workspace_id"],
                plan_id=spec["plan_id"],
                kind=spec["kind"],
                platform=spec.get("platform"),
                title=spec["title"],
                payload_json=json.dumps(spec.get("payload") or {}, ensure_ascii=False),
                depends_on_json=json.dumps(spec.get("depends_on") or []),
                status=spec.get("status") or "pending",
                priority=int(spec.get("priority") or 50),
                scheduled_for=spec.get("scheduled_for"),
                attempt_count=0,
                result_json="{}",
                proof_json="{}",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            out.append(row)
        session.commit()
        return [serialize_job(r) for r in out]


def list_jobs(
    workspace_id: str,
    *,
    plan_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        q = select(MarketingJob).where(MarketingJob.workspace_id == workspace_id)
        if plan_id:
            q = q.where(MarketingJob.plan_id == plan_id)
        if status:
            q = q.where(MarketingJob.status == status)
        q = q.order_by(MarketingJob.scheduled_for.asc().nulls_last(), MarketingJob.priority.asc()).limit(
            limit
        )
        return [serialize_job(r) for r in session.scalars(q).all()]


def due_jobs(workspace_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Jobs whose schedule has arrived and whose dependencies are done."""
    now = utcnow()
    factory = db()
    with factory() as session:
        candidates = session.scalars(
            select(MarketingJob)
            .where(
                MarketingJob.workspace_id == workspace_id,
                MarketingJob.status.in_(("pending", "ready")),
                MarketingJob.scheduled_for.is_not(None),
                MarketingJob.scheduled_for <= now,
            )
            .order_by(MarketingJob.priority.asc(), MarketingJob.scheduled_for.asc())
            .limit(max(limit * 20, 80))
        ).all()
        done_ids = {
            r.id
            for r in session.scalars(
                select(MarketingJob).where(
                    MarketingJob.workspace_id == workspace_id,
                    MarketingJob.status == "done",
                )
            ).all()
        }
        ready: list[MarketingJob] = []
        for row in candidates:
            deps = _loads(row.depends_on_json, [])
            if all(dep in done_ids for dep in deps):
                ready.append(row)
            if len(ready) >= limit:
                break
        return [serialize_job(r) for r in ready]


def mark_job(
    job_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    proof: dict[str, Any] | None = None,
    error_message: str | None = None,
    block_reason: str | None = None,
    executor: str | None = None,
    bump_attempt: bool = False,
) -> dict[str, Any] | None:
    now = utcnow()
    factory = db()
    with factory() as session:
        row = session.get(MarketingJob, job_id)
        if row is None:
            return None
        row.status = status
        row.updated_at = now
        if bump_attempt:
            row.attempt_count = int(row.attempt_count or 0) + 1
        if executor:
            row.executor = executor
        if status == "running" and row.started_at is None:
            row.started_at = now
        if status in {"done", "failed", "blocked", "cancelled"}:
            row.finished_at = now
        if result is not None:
            row.result_json = json.dumps(result, ensure_ascii=False, default=str)
        if proof is not None:
            row.proof_json = json.dumps(proof, ensure_ascii=False, default=str)
        if error_message is not None:
            row.error_message = error_message[:500]
        if block_reason is not None:
            row.block_reason = block_reason[:500]
        # Propagate content_post_id into dependent payloads when an upstream
        # job finishes with a post id (create_content or create_visual).
        post_id = None
        if status == "done":
            if result and result.get("content_post_id"):
                post_id = result["content_post_id"]
            elif proof and proof.get("content_post_id"):
                post_id = proof["content_post_id"]
        if post_id:
            dependents = session.scalars(
                select(MarketingJob).where(
                    MarketingJob.workspace_id == row.workspace_id,
                    MarketingJob.status.in_(("pending", "ready")),
                )
            ).all()
            for dep in dependents:
                deps = _loads(dep.depends_on_json, [])
                if row.id not in deps:
                    continue
                payload = _loads(dep.payload_json, {})
                changed = False
                if not payload.get("content_post_id"):
                    payload["content_post_id"] = post_id
                    changed = True
                # Carry live URL into check_result for OpenClaw verify.
                if proof:
                    if proof.get("external_url") and not payload.get("external_url"):
                        payload["external_url"] = proof["external_url"]
                        changed = True
                    if proof.get("external_id") and not payload.get("external_id"):
                        payload["external_id"] = proof["external_id"]
                        changed = True
                if changed:
                    dep.payload_json = json.dumps(payload, ensure_ascii=False)
                    dep.updated_at = now
        session.commit()
        session.refresh(row)
        return serialize_job(row)


def job_counts(workspace_id: str, plan_id: str | None = None) -> dict[str, int]:
    factory = db()
    with factory() as session:
        q = select(MarketingJob).where(MarketingJob.workspace_id == workspace_id)
        if plan_id:
            q = q.where(MarketingJob.plan_id == plan_id)
        counts: dict[str, int] = {}
        for row in session.scalars(q).all():
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts
