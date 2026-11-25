"""Read durable founder job state without exposing internal payloads or secrets."""
import json
from sqlalchemy import select
from sqlalchemy.orm import aliased
from core.clock import as_utc

from app.core.jobs import ACTIVE_JOB_STATUSES, LOCAL_MANAGED_BUILD_CODE, job_wait
from app.db.models import Job
from app.research.progress import job_progress
from app.state import db


LABELS = {
    "research.deep": "Research", "idea.validate": "Validation", "idea.clarify_turn": "Questions",
    "plan.compile": "Planning", "gtm.journey.prepare": "Customer search",
    "pagebuilder.build": "Page build", "pagebuilder.publish": "Page publication",
    "experiment.run": "Experiment", "experiment.exploration.prepare": "Exploring your idea",
    "estimate.compute": "Resource estimate", "device.work_order": "Mac task",
    "chat": "Reply", "idea": "Idea exploration", "research": "Research", "gtm": "Customer search",
    "content": "Content", "task": "Task", "tasks": "Task", "simulation": "Message check",
    "build": "App build", "qa": "App checks", "deploy": "App update", "experiment": "Experiment",
}


def job_label(kind: str) -> str:
    return LABELS.get(kind, LABELS.get(kind.split(".")[0], "Task"))


def _view(row: Job, session=None) -> dict:
    from app.chat.blockers import humanize_error

    known_errors = {LOCAL_MANAGED_BUILD_CODE, "capability_unavailable", "handler_failed", "job_scope_mismatch", "job_scope_missing",
                    "job_actor_missing", "job_authority_revoked", "lease_attempts_exhausted", "provider_retryable",
                    "job_outcome_uncertain", "exploration_direction_changed",
                    "clarification_insufficient_credits", "clarification_credits_unavailable", "clarification_account_suspended",
                    "clarification_credit_reservation_unavailable", "clarification_byok_unavailable", "clarification_invalid_output",
                    "clarification_boundary_blocked", "clarification_outcome_uncertain", "clarification_provider_bad_api_key",
                    "clarification_provider_out_of_credits", "clarification_provider_provider_error", "clarification_context_limit",
                    "clarification_lease_lost", "clarification_scope_mismatch"}
    known_errors.add("clarification_provider_offline")
    known_errors.update({"pagebuilder_renderer_unavailable", "pagebuilder_generation_failed", "pagebuilder_job_failed"})
    known_errors.update({"research_output_truncated", "research_inputs_changed", "research_saved_sources_unavailable", "plan_output_truncated"})
    known_errors.update({"research_credits_unavailable", "research_credit_reservation_unavailable", "research_insufficient_credits",
                         "research_outcome_uncertain", "research_invalid_output", "research_provider_offline", "research_byok_unavailable",
                         "research_provider_rejected", "research_provider_rate_limited", "research_boundary_blocked",
                         "research_confirmation_required", "research_call_budget_exhausted", "research_scope_mismatch",
                         "research_lease_lost", "validation_revision_superseded", "research_idea_missing"})
    known_errors.update({"confirmed_revision_required", "plan_research_required", "plan_resource_inventory_required",
                         "plan_inputs_changed", "plan_context_too_large", "plan_call_budget_exhausted",
                         "plan_credits_unavailable", "plan_credit_reservation_unavailable", "plan_insufficient_credits",
                         "plan_account_suspended", "plan_byok_unavailable", "plan_provider_offline",
                         "plan_provider_rejected", "plan_provider_rate_limited", "plan_invalid_output",
                         "plan_outcome_uncertain", "plan_settlement_pending", "plan_scope_mismatch",
                         "plan_lease_lost", "plan_operation_in_progress"})
    prefix = (row.last_error or "").split(":", 1)[0]
    error_code = (prefix if prefix in known_errors else "job_failed") if row.last_error else None
    revision_id = None
    if row.kind in {"idea.clarify_turn", "idea.validate", "research.deep"}:
        try:
            payload = json.loads(row.payload_json)
            candidate = payload.get("revision_id") if isinstance(payload, dict) else None
            if isinstance(candidate, str) and len(candidate) <= 128:
                revision_id = candidate
        except (ValueError, TypeError):
            pass
    wait = job_wait(row)
    if wait and session is not None and wait["reason"] != "cancel_requested":
        from app.db.task_models import Task, TaskRun, TaskStep

        paused = session.scalar(select(Task.id).join(TaskRun, TaskRun.task_id == Task.id)
            .join(TaskStep, TaskStep.run_id == TaskRun.id).where(TaskStep.job_id == row.id,
                Task.workspace_id == row.workspace_id, Task.venture_id == row.venture_id, Task.state == "paused"))
        if paused:
            wait = {"reason": "paused", "label": "Paused. Resume this work when you are ready.", "until": None}
    progress = job_progress(session, row) if session is not None and row.kind in {"idea.validate", "research.deep"} else None
    if progress and wait:
        progress["step_label"] = wait["label"]
    return {"id": row.id, "workspace_id": row.workspace_id, "venture_id": row.venture_id,
            "label": job_label(row.kind),
            "failure_reason": humanize_error(row.last_error) if row.last_error else None,
            "kind": row.kind, "status": row.status, "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "wait": wait,
            "error_code": error_code,
            "revision_id": revision_id,
            **({"progress": progress} if progress is not None else {}),
            **{name: as_utc(getattr(row, name)).isoformat() if getattr(row, name) else None
               for name in ("created_at", "scheduled_for", "started_at", "locked_at", "finished_at")}}


def list_jobs(workspace_id: str, limit: int = 50, venture_id: str | None = None,
              kinds: list[str] | None = None, active_only: bool = False, failed_only: bool = False):
    with db()() as session:
        query = select(Job).where(Job.workspace_id == workspace_id)
        newer = aliased(Job)
        replaced = select(newer.id).where(newer.venture_id == Job.venture_id,
            newer.workspace_id == Job.workspace_id, newer.kind == Job.kind,
            newer.created_at > Job.created_at, newer.status.in_(("succeeded", "done"))).exists()
        if venture_id:
            query = query.where(Job.venture_id == venture_id)
        if kinds:
            query = query.where(Job.kind.in_(kinds))
        if active_only:
            query = query.where(Job.status.in_(ACTIVE_JOB_STATUSES))
        if failed_only:
            query = query.where(Job.status.in_(("failed", "failed_dead", "poison")), ~replaced)
        rows = session.scalars(query.order_by(Job.created_at.desc()).limit(limit)).all()
        resolved = list(session.scalars(select(Job.id).where(Job.workspace_id == workspace_id,
            Job.venture_id == venture_id, Job.status.in_(("failed", "failed_dead", "poison")), replaced))) if active_only and venture_id else []
        return {"items": [_view(row, session) for row in rows],
                **({"resolved_ids": resolved} if active_only and venture_id else {})}


def read_job(job_id: str):
    with db()() as session:
        row = session.get(Job, job_id)
        # Retention may remove a job after central authorization resolved it.
        if row is None:
            return None
        return _view(row, session)


def chat_work(venture_id: str, conversation_id: str | None, *, stop: bool = False, nonce: str | None = None,
              job_ids: list[str] | None = None):
    from app.chat.work_control import work
    return work(venture_id, conversation_id, stop=stop, nonce=nonce, job_ids=job_ids)
