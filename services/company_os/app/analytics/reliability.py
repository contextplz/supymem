"""Read-only, bounded reliability metrics for administrators."""

import json
from datetime import timedelta

from sqlalchemy import case, func, or_, select

from app.db.models import Job, VentureDocument
from app.db.supymem_models import FounderRequest, IdeaClarificationRun, ResearchDossier
from app.db.task_models import Task, TaskRun, TaskStep
from app.state import db
from core.clock import utcnow

CAP = 1000


def reliability_metrics() -> dict:
    now = utcnow().replace(tzinfo=None)

    def metric(count, oldest, capped):
        return {"count": count, "capped": capped,
                "oldest_age_seconds": int((now - oldest).total_seconds()) if oldest else None}

    def object_json(value):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    with db()() as session:
        # ponytail: capped scans report sampled counts/ages, add state/time indexes for exact totals.
        def stale(timestamp, *conditions):
            rows = select(timestamp.label("at")).where(
                timestamp < now - timedelta(minutes=15), *conditions,
            ).limit(CAP + 1).subquery()
            count, oldest = session.execute(select(func.count(), func.min(rows.c.at))).one()
            return metric(count, oldest, count > CAP)

        stuck = {
            "clarification_calling": stale(IdeaClarificationRun.updated_at, IdeaClarificationRun.state == "calling"),
            "chat_turn_pending": stale(FounderRequest.created_at, FounderRequest.source == "chat_turn_v1",
                                       FounderRequest.outcome == "pending"),
        }
        held_states = ("delivery_unconfirmed", "delivery_pending")
        drafts = session.execute(select(VentureDocument.updated_at, VentureDocument.provenance_json).where(
            VentureDocument.kind == "outreach_draft",
            or_(*[VentureDocument.provenance_json.like(f"%{state}%") for state in held_states]),
        ).limit(CAP + 1)).all()
        counts = dict.fromkeys(held_states, 0)
        held_dates = []
        for updated_at, provenance in drafts:
            state = object_json(provenance).get("state")
            if state in held_states:
                counts[state] += 1
                held_dates.append(updated_at)
        stuck["outreach_delivery_held"] = metric(
            sum(counts.values()), min(held_dates, default=None), len(drafts) > CAP,
        ) | {"states": counts}

        dossiers = session.execute(select(ResearchDossier.id, ResearchDossier.created_at, ResearchDossier.summary).where(
            or_(ResearchDossier.summary.like('%"calling"%'), ResearchDossier.summary.like('%"uncertain"%')),
        ).limit(CAP + 1)).all()
        uncertain_dates = []
        for _, created_at, summary in dossiers:
            runtime = object_json(summary).get("_runtime")
            operations = runtime.get("operations") if isinstance(runtime, dict) else None
            if isinstance(operations, dict) and any(
                isinstance(operation, dict) and operation.get("state") in ("calling", "uncertain")
                for operation in operations.values()
            ):
                uncertain_dates.append(created_at)
        stuck["research_operation_uncertain"] = metric(
            len(uncertain_dates), min(uncertain_dates, default=None), len(dossiers) > CAP,
        )

        groups = [dict(row) for row in session.execute(select(
            Job.kind, Job.last_error, func.count().label("count"),
        ).where(Job.status == "failed_dead", Job.finished_at >= now - timedelta(days=14))
            .group_by(Job.kind, Job.last_error).order_by(func.count().desc()).limit(50)).mappings()]

        tasks = select(Task.id, Task.state).where(Task.created_at >= now - timedelta(days=7)).limit(CAP + 1).subquery()
        evidence = select(TaskStep.id).join(TaskRun, TaskRun.id == TaskStep.run_id).where(
            TaskRun.task_id == tasks.c.id, TaskStep.state == "completed",
            TaskStep.evidence_json.not_in(("", "[]", "{}")),
        ).exists()
        completed_task = tasks.c.state == "completed"
        started, completed, verified = session.execute(select(
            func.count(), func.count(case((completed_task, 1))),
            func.count(case((completed_task & evidence, 1))),
        ).select_from(tasks)).one()

    return {
        "generated_at": now.isoformat(), "stuck": stuck,
        "dead_jobs_14d": {"groups": groups, "total": sum(group["count"] for group in groups)},
        "verified_completion_7d": {"started": started, "completed": completed, "verified": verified,
                                   "rate": round(verified / started, 3) if started else None, "capped": started > CAP},
    }
