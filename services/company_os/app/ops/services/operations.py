"""Scoped reads of durable workflows and historical workspace operations.

No execution authority, payloads or model calls belong in this read model.
"""
from datetime import timedelta

from sqlalchemy import func, select

from app.api.jobs_api import _view
from app.db.models import Job, OpsTask, Venture
from app.state import db
from core.clock import as_utc, utcnow

INTERNAL_KINDS = frozenset({
    "build.watch", "credits.sweep", "ops.signal_sweep", "retention.purge", "lead_library.sweep",
    "import.cleanup", "infra.reconcile", "infra.meter", "infra.scale_check",
    "infra.sleep_idle", "analytics.rollup", "analytics.ingest_batch",
    "app.health_probe", "status.page", "notify.desktop",
})
LABELS = {
    "idea.clarify_turn": "Idea clarification", "idea.validate": "Idea validation",
    "research.deep": "Research", "plan.compile": "Execution planning",
    "build.run": "Build dispatch", "content.generate": "Content generation", "content.topics": "Content topics",
    "content.draft": "Content draft",
}
STATES = {
    "pending": ("scheduled", "retry_wait", "pending"),
    "in_progress": ("leased", "running", "in_progress"),
    "completed": ("completed", "succeeded"), "failed": ("failed", "failed_dead"),
    "cancelled": ("cancelled", "canceled"),
}


def normalize(status):
    return next((key for key, values in STATES.items() if status in values), "unknown")


def _scope(session, workspace_id, venture_id):
    if venture_id is not None:
        row = session.get(Venture, venture_id)
        if row is None or row.workspace_id != workspace_id:
            raise KeyError("not_found")


def _filters(model, workspace_id, venture_id):
    filters = [model.workspace_id == workspace_id]
    if model is Job:
        filters.append(Job.kind.not_in(INTERNAL_KINDS))
        if venture_id is not None:
            filters.append(Job.venture_id == venture_id)
    return filters


def _models(venture_id):
    # Historical task metadata is not authority for a venture association.
    return (Job,) if venture_id is not None else (Job, OpsTask)


def _project(row):
    if isinstance(row, Job):
        safe = _view(row)
        # revision_id is unnecessary here, even though the jobs view allows it.
        safe.pop("revision_id", None)
        return {**safe, "job_status": row.status, "status": normalize(row.status),
                "title": LABELS.get(row.kind, "Company workflow"), "agent_type": row.kind,
                "priority": row.priority, "provenance": "durable_job",
                "status_basis": "persisted_job_state_not_agent_heartbeat"}
    return {"id": row.id, "workspace_id": row.workspace_id, "venture_id": None,
            "title": row.title[:512], "agent_type": row.agent_type,
            "priority": row.priority, "status": normalize(row.status),
            "provenance": "legacy_ops_task", "status_basis": "persisted_legacy_task_state",
            "error_code": "job_failed" if row.error_message else None,
            "created_at": as_utc(row.created_at).isoformat(),
            "updated_at": as_utc(row.updated_at).isoformat()}


def list_tasks(workspace_id, *, venture_id=None, status=None, agent_type=None, limit=50):
    if not 1 <= limit <= 200 or status is not None and status not in {*STATES, "unknown"}:
        raise ValueError("invalid_task_filter")
    with db()() as session:
        _scope(session, workspace_id, venture_id)
        result = []
        for model in _models(venture_id):
            filters = _filters(model, workspace_id, venture_id)
            if status == "unknown":
                filters.append(model.status.not_in([value for values in STATES.values() for value in values]))
            elif status:
                filters.append(model.status.in_(STATES[status]))
            if agent_type:
                filters.append((Job.kind if model is Job else OpsTask.agent_type) == agent_type)
            rows = session.scalars(select(model).where(*filters).order_by(model.created_at.desc(), model.id.desc()).limit(limit))
            result.extend(_project(row) for row in rows)
        return sorted(result, key=lambda row: (row["created_at"], row["id"]), reverse=True)[:limit]


def get_task(workspace_id, task_id, *, venture_id=None):
    with db()() as session:
        _scope(session, workspace_id, venture_id)
        for model in _models(venture_id):
            row = session.scalar(select(model).where(*_filters(model, workspace_id, venture_id), model.id == task_id))
            if row is not None:
                return _project(row)
    return None


def summary(workspace_id, *, venture_id=None):
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    today = {key: 0 for key in (*STATES, "unknown")}
    total = today.copy()
    active = set()
    with db()() as session:
        _scope(session, workspace_id, venture_id)
        for model in _models(venture_id):
            filters = _filters(model, workspace_id, venture_id)
            category = Job.kind if model is Job else OpsTask.agent_type
            for counts, extra in ((total, []), (today, [model.created_at >= start, model.created_at < end])):
                query = select(model.status, func.count()).where(*filters, *extra).group_by(model.status)
                for state, count in session.execute(query):
                    counts[normalize(state)] += count
            active.update(session.scalars(select(category).where(*filters, model.status.in_(STATES["in_progress"])).distinct()))
    return {"tasks_today_total": sum(today.values()), "tasks_today_completed": today["completed"],
            "tasks_today_pending": today["pending"] + today["in_progress"],
            "tasks_today_failed": today["failed"], "tasks_today_by_status": today,
            "tasks_all_time_by_status": total, "active_agents": sorted(active),
            "active_agents_basis": "persisted_in_progress_tasks_not_heartbeat",
            "scope": {"workspace_id": workspace_id, "venture_id": venture_id,
                      "legacy_tasks_included": venture_id is None},
            "count_basis": "created_in_utc_day_classified_by_current_state",
            "date": start.date().isoformat(), "timezone": "UTC",
            "excluded_job_kinds": sorted(INTERNAL_KINDS)}


def agent_status(workspace_id, *, venture_id=None):
    """Only observed categories; task records are not evidence of a live agent."""
    from sqlalchemy import case
    from app.db.models import OpsAgentRun
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    result = []
    with db()() as session:
        _scope(session, workspace_id, venture_id)
        for model in _models(venture_id):
            category = Job.kind if model is Job else OpsTask.agent_type
            filters = _filters(model, workspace_id, venture_id)
            groups = session.execute(select(category, func.count(), func.sum(case(
                ((model.created_at >= start) & (model.created_at < end), 1), else_=0
            ))).where(*filters).group_by(category)).all()
            counts = {kind: (total, today) for kind, total, today in groups}
            if model is OpsTask:
                # A historical run without a task is still observed work, but it
                # must never inflate task totals or invent a venture association.
                for kind in session.scalars(select(OpsAgentRun.agent_type).where(
                        OpsAgentRun.workspace_id == workspace_id).distinct()):
                    counts.setdefault(kind, (0, 0))
            for kind, (total, today) in sorted(counts.items()):
                task = session.scalar(select(model).where(*filters, category == kind)
                                      .order_by(model.created_at.desc(), model.id.desc()).limit(1))
                observed = as_utc(task.created_at) if task else None
                state = normalize(task.status) if task else "unknown"
                provenance = "durable_job" if model is Job else "legacy_ops_task"
                basis = "persisted_task_state_not_agent_heartbeat"
                last_run = None
                if model is OpsTask:
                    run = session.scalar(select(OpsAgentRun).where(OpsAgentRun.workspace_id == workspace_id,
                        OpsAgentRun.agent_type == kind).order_by(OpsAgentRun.started_at.desc(), OpsAgentRun.id.desc()).limit(1))
                    if run:
                        last_run = as_utc(run.started_at)
                        if observed is None or last_run >= observed:
                            observed, state = last_run, normalize(run.status)
                            basis = "persisted_legacy_run_state_not_agent_heartbeat"
                result.append({"id": f"{provenance}:{kind}", "agent_type": kind,
                    "label": LABELS.get(kind, kind.replace("_", " ")[:128]) if model is OpsTask or kind in LABELS else "Company workflow",
                    "provenance": provenance, "status_basis": basis,
                    "last_run_at": last_run.isoformat() if last_run else None,
                    "last_run_status": None,  # Retained compatibility; UI uses observed_status.
                    "observed_at": observed.isoformat() if observed else None,
                    "observed_status": state, "tasks_today": int(today), "tasks_total": int(total),
                    "timezone": "UTC", "workspace_id": workspace_id, "venture_id": venture_id})
    return result
