"""Current wallet eligibility and explicit, receipt-preserving credit recovery."""
from __future__ import annotations

import json

from sqlalchemy import select

from app.credits.accounts import account_for_venture
from app.core.jobs import ACTIVE_JOB_STATUSES, FatalError
from app.db.models import CreditReservation, Job
from app.db.supymem_models import IdeaClarificationRun, IdeaSession, ResearchDossier
from core.clock import utcnow


def eligible_job(session, job, *, actor_subject=None) -> bool:
    code = str(job.last_error or "").partition(":")[0]
    if job.status not in {"failed", "failed_dead", "poison"} or code not in {
            "clarification_insufficient_credits", "research_insufficient_credits"}:
        return False
    try:
        payload = json.loads(job.payload_json)
        idea = session.get(IdeaSession, payload.get("session_id"))
        if not idea or (idea.workspace_id, idea.venture_id, idea.current_revision_id) != (
                job.workspace_id, job.venture_id, payload.get("revision_id")) or idea.state == "abandoned":
            return False
        latest = session.scalar(select(IdeaSession.id).where(IdeaSession.venture_id == job.venture_id,
            IdeaSession.workspace_id == job.workspace_id, IdeaSession.state != "abandoned")
            .order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
        latest_job = session.scalar(select(Job.id).where(Job.venture_id == job.venture_id,
            Job.workspace_id == job.workspace_id, Job.kind == job.kind)
            .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        active = session.scalars(select(Job).where(Job.venture_id == job.venture_id,
            Job.workspace_id == job.workspace_id, Job.kind.in_(("idea.clarify_turn", "idea.validate", "research.deep")),
            Job.status.in_(ACTIVE_JOB_STATUSES))).all()
        if latest != idea.id or latest_job != job.id or any(json.loads(other.payload_json).get("session_id") == idea.id for other in active):
            return False
        if job.kind == "idea.clarify_turn" and code == "clarification_insufficient_credits":
            from app.idea.runner import MAX_TOKENS, unresolved_clarification
            from app.idea.agent import messages_for
            from app.credits.estimate import count_prompt_tokens, estimate_llm_micro
            from app.llm.founder_provider import resolve_llm_config
            run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job.id))
            if (idea.state == "confirmed" or not run or run.state != "failed" or run.error_code != code
                    or run.response is not None or (run.session_id, run.workspace_id, run.venture_id, run.revision_id) != (
                        idea.id, job.workspace_id, job.venture_id, idea.current_revision_id)
                    or set(json.loads(run.usage)) - {"required_micro"} or unresolved_clarification(session, idea)):
                return False
            newest = session.scalar(select(IdeaClarificationRun.job_id).where(IdeaClarificationRun.session_id == idea.id)
                .order_by(IdeaClarificationRun.created_at.desc(), IdeaClarificationRun.id.desc()).limit(1))
            if newest != job.id:
                return False
            resolved = resolve_llm_config(job.workspace_id)
            required = estimate_llm_micro(resolved.model,
                prompt_tokens=count_prompt_tokens(messages_for(json.loads(run.snapshot))), max_tokens=MAX_TOKENS)
        elif job.kind == "research.deep" and code == "research_insufficient_credits":
            from app.research.execution import identifier
            from app.research.service import review_quote
            from app.credits.policy import web_fetch_credits, web_search_credits
            from app.credits.units import credits_to_micro
            dossier = session.get(ResearchDossier, identifier(job.id))
            if (idea.state != "confirmed" or idea.confirmed_revision_id != payload.get("revision_id") or not dossier
                    or (dossier.workspace_id, dossier.venture_id, dossier.revision_id) != (
                        job.workspace_id, job.venture_id, idea.confirmed_revision_id)):
                return False
            runtime = json.loads(dossier.summary).get("_runtime", {})
            operations = runtime.get("operations", {})
            for operation in operations.values():
                usage = operation.get("usage", {})
                reservation = session.get(CreditReservation, usage["reservation_id"]) if usage.get("reservation_id") else None
                if operation.get("state") in {"calling", "provider_responded", "responded", "uncertain", "release_pending"} or (
                        reservation and reservation.status in {"active", "open", "reconciling"}):
                    return False
            pending = [(key, op) for key, op in operations.items() if op.get("state") == "reserving"]
            if not pending:
                return False
            key, _operation = pending[-1]
            if "review" in key:
                required = review_quote(dossier.id)["ceiling_micro"]
            else:
                from app.research.deep import queries_for
                if (not key.startswith(("scrape_", "prior_", "product_followup_", "name_followup_"))
                        and key != "fallback" and key not in dict(queries_for(runtime.get("snapshot", {}), "all"))):
                    return False
                required = credits_to_micro(web_fetch_credits() if key.startswith("scrape_") else web_search_credits())
        else:
            return False
        account = account_for_venture(job.venture_id, acting_person_id=actor_subject, session=session)
        return bool(type(required) is int and required > 0 and account and account.status == "active"
            and account.granted_micro - account.spent_micro - account.reserved_micro >= required)
    except (RuntimeError, ValueError, KeyError, TypeError, FatalError):
        return False


def continue_job(session, job, *, venture_id, actor_subject=None):
    if job.venture_id != venture_id:
        raise KeyError(job.id)
    payload = json.loads(job.payload_json)
    if payload.get("_credits_continued") and job.status in (*ACTIVE_JOB_STATUSES, "succeeded", "done"):
        return job.id
    if not eligible_job(session, job, actor_subject=actor_subject):
        raise RuntimeError("credits_continue_unavailable")
    if job.kind == "idea.clarify_turn":
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == job.id).with_for_update())
        run.state, run.error_code, run.updated_at = "retryable", None, utcnow()
    else:
        from app.research.execution import identifier
        dossier = session.get(ResearchDossier, identifier(job.id), with_for_update=True)
        document = json.loads(dossier.summary)
        document.pop("error_code", None)
        dossier.summary, dossier.state, dossier.completed_at = json.dumps(document), "running", None
    payload["_credits_continued"] = True
    if actor_subject:
        payload["_actor_subject"] = actor_subject
    job.payload_json = json.dumps(payload)
    job.status, job.last_error, job.finished_at = "scheduled", None, None
    job.locked_by = job.locked_at = None
    job.run_after = job.scheduled_for = utcnow()
    job.max_attempts = max(job.max_attempts, job.attempts + 5)
    return job.id
