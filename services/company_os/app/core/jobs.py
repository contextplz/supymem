"""SQLite-backed job queue: visibility timeout, retries, exponential backoff, poison."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from core.clock import as_utc, utcnow
from core.enums import JobStatus
from core.ids import new_id
from sqlalchemy import and_, case, func, inspect, literal, or_, select, tuple_, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.core.local_worker import redis_url
from app.db.models import Job, VentureDocument, Workspace
from app.db.session import make_engine, make_session_factory, serialize_writes

DEFAULT_VISIBILITY_TIMEOUT_S = 30
DEFAULT_MAX_ATTEMPTS = 3
BASE_BACKOFF_S = 1.0
STALE_LOCK_S = 90
PAID_JOURNEY_KINDS = frozenset({"idea.clarify_turn", "idea.validate", "research.deep", "plan.compile"})
IDEMPOTENT_RETRY_KINDS = frozenset({"experiment.exploration.prepare", "estimate.compute"})
RETRY_BASE_S = 30.0
RETRY_CAP_S = 1800.0
CLAIMABLE = frozenset(
    {
        JobStatus.PENDING.value,
        JobStatus.SCHEDULED.value,
        JobStatus.RETRY_WAIT.value,
    }
)
LEASED = frozenset({JobStatus.RUNNING.value, JobStatus.LEASED.value})
ACTIVE_JOB_STATUSES = CLAIMABLE | LEASED
WAKE_CHANNEL = "jobs:wake"
JOB_CLASSES = {
    **dict.fromkeys({
        "chat.respond", "chat.recovery", "idea.recon", "idea.clarify_turn",
        "idea.generate_candidates", "idea.validate", "scope.classify", "estimate.compute",
    }, "interactive"),
    **dict.fromkeys({
        "inbox.briefing.tick", "inbox.cos_brief.tick", "memory.extract",
        "research.deep", "knowledge.loot_drop_refresh", "pagebuilder.build", "pagebuilder.publish",
        "simulation.rehearse", "gtm.god.run", "gtm.journey.prepare", "gtm.journey.maintain", "gtm.linkedin.search",
        "gtm.gmail.read", "gtm.outreach.retry",
        "content.topics", "content.draft", "tasks.verify", "tasks.execute", "task.run", "plan.compile",
        "build.run", "build.watch", "qa.run", "qa.triage", "deploy.run", "deploy.unpublish",
        "deploy.rollback", "app.debug", "app.iterate", "import.cleanup", "import.analyze",
        "infra.provision", "infra.meter", "infra.scale_check", "infra.sleep_idle", "credits.sweep",
        "autopilot.tick", "suggestions.generate", "analytics.rollup", "analytics.ingest_batch",
        "ops.signal_sweep", "ops.slate", "ops.brain_cycle", "ops.channel_post", "retention.purge", "lead_library.sweep",
        "workspace.purge", "experiment.evaluate", "experiment.run", "experiment.beliefs.tick", "experiment.exploration.prepare", "infra.reconcile", "infra.forecast", "app.backup",
        "app.health_probe", "learning.cycle", "status.page", "milestone.reassess", "founder.digest",
        "email.send", "email.schedule", "notify.desktop", "idea.page_refresh", "chat.reminder", "routines.fire", "routines.report",
    }, "bulk"),
}
INTERACTIVE_KINDS = frozenset(kind for kind, cls in JOB_CLASSES.items() if cls == "interactive")
log = logging.getLogger("app.jobs")


def job_class(kind: str) -> str:
    return JOB_CLASSES.get(kind, "bulk")


def job_wait(row: Job, *, now=None) -> dict[str, Any] | None:
    if row.status not in CLAIMABLE:
        return None
    if getattr(row, "cancel_requested", False):
        return {"reason": "cancel_requested", "label": "Stopping. You asked to stop this work.", "until": None}
    clock = as_utc(now or utcnow())
    dates = [as_utc(value) for name in ("run_after", "scheduled_for") if (value := getattr(row, name, None))]
    due = max(dates) if dates else None
    reason, label = "queued", "Your work is saved but has not started yet."
    if due and due > clock:
        if row.status == JobStatus.RETRY_WAIT.value:
            if (row.last_error or "").split(":", 1)[0] == "rate_limited":
                reason, label = "usage_limit", "Your usage limit needs time to reset."
            else:
                reason, label = "retry_backoff", "Waiting before trying again after a temporary problem."
        else:
            reason, label = "scheduled", "Your work is scheduled for later."
    elif row.status == JobStatus.RETRY_WAIT.value:
        label = "Your work is ready to try again."
    return {"reason": reason, "label": "Starting soon. " + label,
            "until": due.isoformat() if due else None}


class RetryableError(Exception):
    """Handler failed; job should retry with backoff."""

    def __init__(self, code: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(code)
        self.retry_after_s = (min(60.0, max(1.0, retry_after_s))
            if type(retry_after_s) in {int, float} and math.isfinite(retry_after_s) else None)


class FatalError(Exception):
    """Handler failed permanently; job goes to failed_dead."""


LOCAL_MANAGED_BUILD_CODE = "local_managed_build_blocked"
LOCAL_MANAGED_BUILD_MESSAGE = ("Your saved work is unchanged. Open your venture chat and retry through your connected coding tool. "
                               "If you need to connect it, open Settings > Keys and connections > Coding tools.")


def is_local_managed_job(kind: str, payload: dict[str, Any]) -> bool:
    from app.core.local_mode import is_local

    return is_local() and (
        kind in {"infra.provision", "qa.run"}
        or (kind in {"build.run", "build.watch"} and payload.get("runtime_version") == 1)
    )


@dataclass
class JobSpec:
    kind: str
    payload: dict[str, Any]
    venture_id: str | None = None
    priority: int = 5
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    timeout_s: int = 900
    token_budget: int | None = None
    idempotency_key: str | None = None


@dataclass
class ClaimedJob:
    id: str
    kind: str
    venture_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    timeout_s: int
    token_budget: int | None
    workspace_id: str | None = None
    worker_id: str | None = None


class JobQueue:
    """Claim / complete / fail jobs with visibility leases and a poison queue."""

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._engine = engine or make_engine("sqlite:///:memory:")
        self._factory = session_factory or make_session_factory(self._engine)
        self._visibility_timeout_s = visibility_timeout_s
        self._clock = clock or utcnow

    def enqueue(self, spec: JobSpec) -> str:
        from core.request_id import current_request_id

        if is_local_managed_job(spec.kind, spec.payload):
            raise ValueError(LOCAL_MANAGED_BUILD_MESSAGE)
        payload = {key: value for key, value in spec.payload.items() if key != "_trace_id"}
        if current_request_id():
            payload["_trace_id"] = current_request_id()
        now = self._clock()
        job_id = new_id("job")
        with self._factory() as session:
            if spec.idempotency_key:
                existing = session.scalar(
                    select(Job).where(Job.idempotency_key == spec.idempotency_key)
                )
                if existing is not None:
                    return existing.id
            row = Job(
                id=job_id,
                kind=spec.kind,
                venture_id=spec.venture_id,
                payload_json=json.dumps(payload),
                status=JobStatus.PENDING.value,
                priority=spec.priority,
                attempts=0,
                max_attempts=spec.max_attempts,
                timeout_s=spec.timeout_s,
                token_budget=spec.token_budget,
                idempotency_key=spec.idempotency_key,
                last_error=None,
                scheduled_for=now,
                started_at=None,
                finished_at=None,
                created_at=now,
            )
            session.add(row)
            session.commit()
        return job_id

    def reclaim_expired(self, session: Session) -> int:
        """Move crashed running jobs back to pending after visibility timeout."""
        now = self._clock()
        cutoff = now - timedelta(seconds=self._visibility_timeout_s)
        result = session.execute(
            update(Job)
            .where(
                and_(
                    Job.status == JobStatus.RUNNING.value,
                    Job.started_at.is_not(None),
                    Job.started_at < cutoff,
                )
            )
            .values(status=JobStatus.PENDING.value, started_at=None)
        )
        return result.rowcount or 0

    def claim(self, *, kinds: list[str] | None = None) -> ClaimedJob | None:
        with self._factory() as session:
            self.reclaim_expired(session)
            now = self._clock()
            q = (
                select(Job)
                .where(
                    and_(
                        Job.status == JobStatus.PENDING.value,
                        or_(Job.scheduled_for.is_(None), Job.scheduled_for <= now),
                    )
                )
                .order_by(Job.priority.desc(), Job.created_at.asc())
                .limit(1)
            )
            if kinds:
                q = q.where(Job.kind.in_(kinds))
            row = session.scalar(q)
            if row is None:
                session.commit()
                return None
            row.status = JobStatus.RUNNING.value
            row.attempts += 1
            row.started_at = now
            session.commit()
            return ClaimedJob(
                id=row.id,
                kind=row.kind,
                venture_id=row.venture_id,
                payload=json.loads(row.payload_json),
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                timeout_s=row.timeout_s,
                token_budget=row.token_budget,
            )

    def complete(self, job_id: str) -> None:
        with self._factory() as session:
            row = session.get(Job, job_id)
            if row is None:
                return
            row.status = JobStatus.DONE.value
            row.finished_at = self._clock()
            row.last_error = None
            session.commit()

    def fail(self, job_id: str, error: str) -> str:
        """Mark failure; reschedule with backoff or move to poison queue.

        Returns the resulting status: ``pending``, ``failed``, or ``poison``.
        """
        with self._factory() as session:
            row = session.get(Job, job_id)
            if row is None:
                return JobStatus.FAILED.value
            row.last_error = error[:4000]
            now = self._clock()
            if row.attempts >= row.max_attempts:
                row.status = JobStatus.POISON.value
                row.finished_at = now
                session.commit()
                return JobStatus.POISON.value
            # Exponential backoff: base * 2^(attempts-1)
            delay = BASE_BACKOFF_S * math.pow(2, max(row.attempts - 1, 0))
            row.status = JobStatus.PENDING.value
            row.started_at = None
            row.scheduled_for = now + timedelta(seconds=delay)
            session.commit()
            return JobStatus.PENDING.value

    def get(self, job_id: str) -> Job | None:
        with self._factory() as session:
            return session.get(Job, job_id)

    def status(self, job_id: str) -> str | None:
        with self._factory() as session:
            row = session.get(Job, job_id)
            return None if row is None else row.status


def _payload(row: Job) -> dict[str, Any]:
    try:
        return json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}


def _due(row: Job, now) -> bool:
    when = row.run_after or row.scheduled_for
    return when is None or when <= now


def _wake() -> None:
    url = redis_url()
    if not url:
        return
    try:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        client = redis.Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2,
                                      retry=Retry(NoBackoff(), 0))
        client.publish(WAKE_CHANNEL, "1")
    except Exception:
        return


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    run_after: Any | None = None,
    dedupe_key: str | None = None,
    effect_key: str | None = None,
    priority: int = 0,
    max_attempts: int = 5,
) -> str:
    """Insert or replace unstarted work inside the caller's transaction (outbox)."""
    from app.autopilot.receipts import queued_job
    from app.tenancy import current_or_none
    payload = dict(payload or {})
    if is_local_managed_job(kind, payload):
        raise ValueError(LOCAL_MANAGED_BUILD_MESSAGE)
    from core.request_id import current_request_id

    payload.pop("_trace_id", None)
    if current_request_id():
        payload["_trace_id"] = current_request_id()
    scope = current_or_none()
    for key, value in (("workspace_id", workspace_id), ("venture_id", venture_id)):
        if value is not None:
            if payload.get(key) not in {None, value}:
                raise ValueError("job_scope_mismatch")
            payload[key] = value
    if scope and workspace_id and scope.workspace_id == workspace_id:
        payload["_actor_subject"] = scope.actor
    from app.chat.conversations import capture_job_conversation
    conversation = capture_job_conversation(venture_id, workspace_id)
    if payload.get("_chat_conversation") not in (None, conversation):
        raise ValueError("job_conversation_scope_mismatch")
    if conversation:
        payload["_chat_conversation"] = conversation
    from app.chat.store import CLIENT_REQUEST_ID, REPLY_REQUEST_ID
    from app.chat.work_control import check_stopped
    if session.get_bind().dialect.name == "sqlite":
        session.execute(update(Job).where(False).values(status=Job.status))
    check_stopped(session, CLIENT_REQUEST_ID.get())
    if request_nonce := CLIENT_REQUEST_ID.get() or REPLY_REQUEST_ID.get():
        payload["_chat_request_nonce"] = request_nonce
    if dedupe_key and session.get_bind().dialect.name == "postgresql":
        serialize_writes(session, key="job-dedupe:" + dedupe_key)
    now = utcnow()
    if effect_key:
        existing_effect = session.scalar(
            select(Job).where(
                Job.effect_key == effect_key,
                Job.status.in_(
                    (JobStatus.SUCCEEDED.value, JobStatus.DONE.value)
                ),
            )
        )
        if existing_effect is not None:
            if (existing_effect.workspace_id, existing_effect.venture_id, existing_effect.kind) != (workspace_id, venture_id, kind):
                raise ValueError("job_dedupe_scope_mismatch")
            if json.loads(existing_effect.payload_json).get("_chat_conversation") != payload.get("_chat_conversation"):
                raise ValueError("job_conversation_scope_mismatch")
            from app.tasks.service import attach_job
            attach_job(session, existing_effect)
            queued_job(existing_effect.id)
            return existing_effect.id
    if dedupe_key:
        existing = session.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is not None and (existing.workspace_id, existing.venture_id, existing.kind) != (workspace_id, venture_id, kind):
            raise ValueError("job_dedupe_scope_mismatch")
        if existing is not None and json.loads(existing.payload_json).get("_chat_conversation") != payload.get("_chat_conversation"):
            raise ValueError("job_conversation_scope_mismatch")
        if existing is not None and existing.status in CLAIMABLE and not existing.attempts:
            existing.payload_json = json.dumps(payload or {})
            existing.run_after = run_after
            existing.scheduled_for = run_after or now
            existing.priority = priority
            existing.kind = kind
            existing.workspace_id = workspace_id or existing.workspace_id
            existing.venture_id = venture_id or existing.venture_id
            from app.tasks.service import attach_job
            attach_job(session, existing)
            queued_job(existing.id)
            return existing.id
        if existing is not None:
            from app.tasks.service import attach_job
            attach_job(session, existing)
            queued_job(existing.id)
            return existing.id  # Completed/failed intent needs an explicit new key.
    job_id = new_id("job")
    session.add(
        Job(
            id=job_id,
            kind=kind,
            venture_id=venture_id,
            workspace_id=workspace_id,
            payload_json=json.dumps(payload or {}),
            status=JobStatus.SCHEDULED.value,
            priority=priority,
            attempts=0,
            max_attempts=max_attempts,
            timeout_s=900,
            token_budget=None,
            idempotency_key=dedupe_key,
            dedupe_key=dedupe_key,
            effect_key=effect_key,
            last_error=None,
            scheduled_for=run_after or now,
            run_after=run_after,
            started_at=None,
            finished_at=None,
            created_at=now,
        )
    )
    session.flush()
    from app.tasks.service import attach_job
    attach_job(session, session.get(Job, job_id))
    queued_job(job_id)
    return job_id


def _call_in_flight(session: Session, job_id: str) -> bool:
    """Unknown paid outcomes are never retried, across every durable journal."""
    from app.db.supymem_models import ExecutionPlan, IdeaClarificationRun, ResearchDossier
    from app.planning.execution import identifier as plan_identifier
    from app.research.execution import identifier

    job = session.get(Job, job_id)
    if job and job.kind == "idea.clarify_turn" and session.scalar(select(IdeaClarificationRun.id).where(
        IdeaClarificationRun.job_id == job_id, IdeaClarificationRun.state.in_(("calling", "uncertain")),
    )) is not None:
        return True
    dossier = session.get(ResearchDossier, identifier(job_id))
    plan = session.get(ExecutionPlan, plan_identifier(job_id))
    documents = [job.payload_json if job else "{}", dossier.summary if dossier else "{}",
                 plan.structured if plan else "{}"]
    for raw in documents:
        try:
            data = json.loads(raw or "{}")
            operations = data.get("_runtime", {}).get("operations", {})
            if any(operation.get("state") in {"calling", "uncertain"} for operation in operations.values()):
                return True
        except (ValueError, TypeError, AttributeError):
            return True  # A damaged journal cannot authorize another paid call.
    return False


def retry_is_safe(session: Session, row: Job) -> bool:
    from app.core.local_mode import is_local
    from app.db.models import CreditReservation, EventLog

    if is_local() and row.kind in PAID_JOURNEY_KINDS | IDEMPOTENT_RETRY_KINDS:
        from app.credits import local_meter
        from app.credits.errors import CreditsUnavailable

        # shortcut: unlinked calls fence the venture; use task-scoped receipts when unrelated calls need independent retries.
        try:
            with local_meter._database() as connection:
                if connection.execute("SELECT 1 FROM calls WHERE workspace_id IS ? AND venture_id IS ? "
                        "AND status NOT IN ('confirmed', 'unspent') LIMIT 1",
                        (row.workspace_id, row.venture_id)).fetchone():
                    return False
        except CreditsUnavailable:
            return False
    if row.kind == "experiment.exploration.prepare":
        # shortcut: receipts lack task IDs; use task-scoped billing when unrelated pending calls must not block retries.
        pending_hold = session.scalar(select(CreditReservation.id).where(
            CreditReservation.workspace_id == row.workspace_id, CreditReservation.venture_id == row.venture_id,
            CreditReservation.is_pool.is_(False), CreditReservation.status.in_(("open", "active", "reconciling"))).limit(1))
        closed = aliased(EventLog)
        pending_call = session.scalar(select(EventLog.id).where(
            EventLog.workspace_id == row.workspace_id, EventLog.venture_id == row.venture_id,
            EventLog.topic == "credits.model.receipt.begin",
            ~select(closed.id).where(closed.workspace_id == EventLog.workspace_id,
                closed.venture_id == EventLog.venture_id, closed.target_ref == EventLog.target_ref,
                closed.topic.in_(("credits.model.receipt.completed", "credits.model.receipt.absorbed"))).exists()).limit(1))
        if pending_hold or pending_call:
            return False
    error = (row.last_error or "").lower()
    return (row.kind in PAID_JOURNEY_KINDS | IDEMPOTENT_RETRY_KINDS
            and not any(word in error for word in ("uncertain", "reconcil", "settlement_pending"))
            and not row.cancel_requested and not _call_in_flight(session, row.id))


def retry_failed_job(session: Session, row: Job) -> None:
    if row.kind not in IDEMPOTENT_RETRY_KINDS or not retry_is_safe(session, row):
        raise ValueError("job_retry_unsafe")
    if row.status not in {"failed", "failed_dead", "poison"}:
        raise ValueError("job_not_failed")
    row.status, row.attempts = JobStatus.SCHEDULED.value, 0
    row.run_after = row.scheduled_for = utcnow()
    row.locked_by = row.locked_at = row.started_at = row.finished_at = None


def _deploy_switch_may_have_happened(session: Session, row: Job) -> bool:
    from app.db.supymem_models import Deployment

    payload = _payload(row)
    if (row.kind != "deploy.run" or not isinstance(payload, dict) or payload.get("schema_version") != 1
            or not isinstance(payload.get("deployment_id"), str)):
        return False
    deployment = session.get(Deployment, payload["deployment_id"], with_for_update=True)
    return deployment is not None and deployment.state in {"creating_infra", "health_checking"}


def _watchdog_failure(session: Session, row: Job) -> None:
    """A worker disappearing must leave the same error/recovery receipts as a handler failure."""
    from app.analytics.error_events import actor_person_id, record_job_failure
    from app.chat.milestone_hooks import content_failed, failed
    from app.core.local_mode import is_local

    record_job_failure(session, row, code=row.last_error or "job_outcome_uncertain",
                       status=row.status,
                       person_id=actor_person_id(_payload(row).get("_actor_subject")))
    experiment_reply = None
    if row.kind == "experiment.run" and row.venture_id:
        from app.db.supymem_models import Experiment
        from app.experiments import autonomous, ledger

        payload = _payload(row)
        experiment = session.get(Experiment, payload.get("experiment_id"), with_for_update=True)
        context = json.loads(experiment.context or "{}") if experiment else {}
        if (experiment and experiment.kind == ledger.KIND and context.get("runner") == "system"
                and (experiment.venture_id, experiment.workspace_id) == (row.venture_id, row.workspace_id)
                and context.get("job_id") == row.id and context.get("run_id") == payload.get("run_id")
                and not context.get("result")):
            context.update(error="Your experiment stopped before you could get a confirmed result. Its outcome and any reserved credits need recovery. Nothing was repeated automatically.",
                           retryable=False, outreach_state="blocked")
            experiment.context = json.dumps(context)
            experiment_reply = autonomous.failure_reply({**context["spec"], "error": context["error"], "retryable": False})
    if not row.venture_id:
        return
    from app.chat.conversations import job_receipt_conversation
    with job_receipt_conversation(_payload(row), venture_id=row.venture_id, workspace_id=row.workspace_id) as qualified:
        if not qualified:
            return
        if experiment_reply:
            from app.chat.milestones import post_milestone

            post_milestone(row.venture_id, key=_payload(row)["run_id"] + ":finished",
                           kind=experiment_reply["kind"], requested=True, body_md=experiment_reply["body_md"],
                           actions=experiment_reply["actions"], citations=experiment_reply["citations"], session=session)
        elif row.kind in {"idea.validate", "research.deep", "plan.compile", "idea.clarify_turn"}:
            failed(session, row.venture_id, row.kind, row.id, row.last_error or "")
        elif row.kind in OTHER_FAILURE_JOBS:
            content_failed(session, row.venture_id, row.kind, row.id, row.last_error or "")
        elif row.kind == "pagebuilder.build" and is_local():
            from app.chat.milestones import post_milestone
            from app.pagebuilder.api import _view

            view = _view(row)
            if view["state"] == "failed":
                post_milestone(row.venture_id, key=f"page_failed:{row.id}", kind="text", requested=True,
                    body_md=view["error_reason"], session=session,
                    actions=[{"label": "Retry", "action": "pagebuilder.build", "payload": {"retry_job_id": row.id}, "retry": True}]
                        if "retry" in view else [])
        elif row.kind == "deploy.run" and row.last_error == "job_outcome_uncertain":
            from app.chat.milestone_hooks import deployment_failed

            deployment_failed(row.venture_id, _payload(row)["deployment_id"],
                              "hosting_public_state_uncertain", session=session)
        elif row.kind == "task.run":
            from app.chat.recovery import post_failure

            post_failure(row.venture_id, step="task.run", kind="task_failed",
                         key=f"watchdog:{row.id}", code=row.last_error or "job_outcome_uncertain",
                         job_id=str(_payload(row).get("task_id") or ""), session=session,
                         body_md="Your task stopped before I could confirm its result. Your saved work is kept.",
                         actions=[{"label": "Open work", "href": f"/v/{row.venture_id}/tasks", "style": "secondary"}])
        elif row.kind == "chat.recovery":
            from app.chat.recovery import post_failure

            post_failure(row.venture_id, step="chat.action", kind="action_failed", key=f"recovery-failed:{row.id}",
                         code="job_outcome_uncertain", job_id=row.id, session=session,
                         body_md="I could not confirm the alternative guidance finished. Your saved work is kept; no potentially charged request will be repeated automatically.")


def _locked_watchdog_rows(session: Session, query):
    # Competing workers must not both announce the same terminal transition.
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    return session.scalars(query).all()


def reclaim_stale(session: Session, *, now=None, stale_s: int = STALE_LOCK_S) -> int:
    if session.get_bind().dialect.name == "sqlite" and not session.connection().connection.driver_connection.in_transaction:
        from app.db.session import serialize_writes
        serialize_writes(session, key="job-watchdog")
    session.flush()
    clock = now or utcnow()
    cutoff = clock - timedelta(seconds=stale_s)
    expired = (Job.status.in_(tuple(LEASED)), Job.locked_at.is_not(None), Job.locked_at < cutoff)
    from app.core.local_mode import is_local
    pages = []
    recovered_pages = 0
    if is_local():
        pages = [row for row in _locked_watchdog_rows(session, select(Job).where(Job.kind == "pagebuilder.build",
            Job.status.in_({"failed_dead", "failed", "poison", *CLAIMABLE, *LEASED})))
            if not row.cancel_requested and (row.status in LEASED and row.locked_at is not None
                and as_utc(row.locked_at) < as_utc(cutoff)
                or _payload(row).get("_runtime", {}).get("state") == "ready"
                or _payload(row).get("result", {}).get("ready_delivered") is True)]
        for row in pages:
            from app.tasks.locks import lock_for_job

            lock_for_job(session, row.id)
            payload = _payload(row)
            if (payload.get("_runtime", {}).get("state") == "ready"
                    or payload.get("result", {}).get("ready_delivered") is True):
                from app.pagebuilder.native_reference import _completed_candidate, _encode

                original_payload = row.payload_json
                payload["_runtime"].update(state="ready", stage="ready", progress=1)
                payload["_runtime"].pop("error", None)
                payload["_runtime"].pop("error_reason", None)
                row.payload_json = json.dumps(payload)
                try:
                    saved = _completed_candidate(session, row)
                    for variant in range(saved["variant_count"]):
                        _encode(row, saved, variant)
                except (KeyError, TypeError, ValueError):
                    row.payload_json = original_payload
                    if (row.status not in LEASED or row.locked_at is None
                            or as_utc(row.locked_at) >= as_utc(cutoff)):
                        continue
                    row.last_error = "pagebuilder_candidate_invalid"
                else:
                    if row.status in LEASED:
                        complete_job(session, row.id, now=clock, worker_id=row.locked_by, attempt=row.attempts)
                    else:
                        row.status, row.last_error = JobStatus.SUCCEEDED.value, None
                        row.locked_by = row.locked_at = None
                    recovered_pages += 1
                    continue
            else:
                row.last_error = "pagebuilder_interrupted"
            row.status, row.finished_at = JobStatus.FAILED_DEAD.value, clock
            row.locked_by = row.locked_at = None
            _watchdog_failure(session, row)
            recovered_pages += 1
        if pages:
            session.flush()
    # Paid handlers journal every provider call (research.execution): a replay
    # recovers a saved receipt and refuses to re-send a call that may have been
    # charged. So a lost worker is retried unless a call is
    # in flight without a receipt; that stays uncertain for the founder.
    paid = _locked_watchdog_rows(session, select(Job).where(*expired, Job.kind.in_(PAID_JOURNEY_KINDS)))
    uncertain = retried = 0
    for row in paid:
        if row.kind == "research.deep":
            release_lease(session, row.id, now=clock)
            retried += row.status == JobStatus.RETRY_WAIT.value
            uncertain += row.status == JobStatus.FAILED_DEAD.value
        elif row.attempts < row.max_attempts and not _call_in_flight(session, row.id):
            row.status, row.run_after, row.scheduled_for = JobStatus.RETRY_WAIT.value, clock, clock
            retried += 1
        else:
            row.status, row.last_error, row.finished_at = JobStatus.FAILED_DEAD.value, "job_outcome_uncertain", clock
            _watchdog_failure(session, row)
            uncertain += 1
        row.locked_by = row.locked_at = None
    experiments = _locked_watchdog_rows(session, select(Job).where(*expired, Job.kind == "experiment.run"))
    for row in experiments:
        row.status, row.last_error, row.finished_at = JobStatus.FAILED_DEAD.value, "job_outcome_uncertain", clock
        row.locked_by = row.locked_at = None
        _watchdog_failure(session, row)
        uncertain += 1
    from app.db.supymem_models import Deployment
    deployments = _locked_watchdog_rows(session, select(Job).where(*expired, Job.kind == "deploy.run"))
    for row in deployments:
        if not _deploy_switch_may_have_happened(session, row):
            continue
        row.status, row.last_error, row.finished_at = JobStatus.FAILED_DEAD.value, "job_outcome_uncertain", clock
        row.locked_by = row.locked_at = None
        deployment = session.get(Deployment, _payload(row)["deployment_id"])
        if deployment.state != "live":
            deployment.state, deployment.error = "failed", "hosting_public_state_uncertain"
        _watchdog_failure(session, row)
        uncertain += 1
    expired = (*expired, Job.kind.not_in((*PAID_JOURNEY_KINDS, "experiment.run")))
    dead = _locked_watchdog_rows(session, select(Job).where(*expired, Job.attempts >= Job.max_attempts))
    for row in dead:
        row.status, row.last_error, row.finished_at = JobStatus.FAILED_DEAD.value, "lease_attempts_exhausted", clock
        row.locked_by = row.locked_at = None
        _watchdog_failure(session, row)
    # Bulk UPDATE would wait behind another worker's row lock before lease()
    # can reach its own SKIP LOCKED query. Admit retry transitions under the
    # same watchdog row fence as paid/dead transitions; deferred rows remain
    # eligible for a later sweep after their owner's transaction releases.
    if session.get_bind().dialect.name == "postgresql":
        retry = _locked_watchdog_rows(session, select(Job).where(*expired, Job.attempts < Job.max_attempts))
        for row in retry:
            row.status = JobStatus.RETRY_WAIT.value
            row.locked_by = row.locked_at = None
            row.run_after = row.scheduled_for = clock
        retry_count = len(retry)
    else:
        # SQLite has no row-lock admission: retain its atomic conditional UPDATE.
        result = session.execute(update(Job).where(*expired, Job.attempts < Job.max_attempts).values(
            status=JobStatus.RETRY_WAIT.value, locked_by=None, locked_at=None,
            run_after=clock, scheduled_for=clock).execution_options(synchronize_session="fetch"))
        retry_count = result.rowcount or 0
    # Attempts count actual leases, not both the crash and the next lease.
    return recovered_pages + uncertain + retried + len(dead) + retry_count


def expire_overdue_paid_journey(session: Session, *, now=None) -> int:
    """Bound paid journey work even when a live worker keeps heartbeating.

    The worker may still own an in-flight provider request. Terminalizing this
    job fences any later response application; it never creates another job or
    calls the provider again. The operation receipt remains for reconciliation.
    """
    clock = now or utcnow()
    query = select(Job).where(Job.kind.in_(PAID_JOURNEY_KINDS), Job.status.in_(tuple(LEASED)))
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    expired = 0
    for row in session.scalars(query):
        started = as_utc(row.started_at)
        if started is None or (clock - started).total_seconds() <= max(1, row.timeout_s or 900):
            continue
        if row.kind == "research.deep":
            release_lease(session, row.id, now=clock)
            expired += 1
            continue
        row.status = JobStatus.FAILED_DEAD.value
        row.locked_by = row.locked_at = None
        row.last_error = "job_outcome_uncertain"
        row.finished_at = clock
        _watchdog_failure(session, row)
        expired += 1
    return expired


def _lease_scope(model, has_workspaces):
    fallback = func.coalesce(literal("workspace:") + model.workspace_id,
        literal("venture:") + model.venture_id, literal("job:") + model.id)
    if not has_workspaces:
        return fallback
    owner = select(Workspace.owner_id).where(Workspace.id == model.workspace_id).correlate(model).scalar_subquery()
    return func.coalesce(literal("founder:") + owner, fallback)


def lease(
    session: Session,
    worker_id: str,
    *,
    kinds: list[str] | None = None,
    job_classes: list[str] | None = None,
    n: int = 1,
    now=None,
) -> list[ClaimedJob]:
    classes = set(job_classes) if job_classes is not None else {"interactive", "bulk"}
    if not classes <= {"interactive", "bulk"}:
        raise ValueError("unknown worker job class")
    if n <= 0 or kinds == [] or not classes:
        return []
    clock = now or utcnow()
    dialect = session.get_bind().dialect.name
    cap = int(os.environ.get("JOB_WORKSPACE_INFLIGHT_CAP", "8"))
    if cap < 1:
        raise ValueError("JOB_WORKSPACE_INFLIGHT_CAP must be positive")
    if dialect == "sqlite":
        session.execute(update(Job).where(False).values(status=Job.status))
    reclaim_stale(session, now=clock)
    q = (
        select(Job)
        .where(
            and_(
                Job.status.in_([literal(status, literal_execute=True) for status in sorted(CLAIMABLE)]),
                Job.cancel_requested.is_(False),
                or_(Job.run_after.is_(None), Job.run_after <= clock),
                or_(Job.scheduled_for.is_(None), Job.scheduled_for <= clock),
            )
        )
    )
    if kinds:
        q = q.where(Job.kind.in_(kinds))
    if classes == {"interactive"}:
        q = q.where(Job.kind.in_(INTERACTIVE_KINDS))
    elif classes == {"bulk"}:
        q = q.where(Job.kind.not_in(INTERACTIVE_KINDS))
    schema = inspect(session.connection())
    has_workspaces = schema.has_table(Workspace.__tablename__) and any(
        column["name"] == "owner_id" for column in schema.get_columns(Workspace.__tablename__))
    scope = _lease_scope(Job, has_workspaces)
    class_order = case((Job.kind.in_([literal(kind, literal_execute=True) for kind in sorted(INTERACTIVE_KINDS)]),
                        literal(0, literal_execute=True)), else_=literal(1, literal_execute=True))
    tenants = q.with_only_columns(scope.label("scope"),
        class_order.label("class_order"),
        func.min(case((and_(Job.workspace_id.is_(None), Job.venture_id.is_(None)), Job.priority),
                      else_=0)).label("unscoped_priority"),
        func.min(Job.created_at).label("oldest")).group_by(scope, class_order).subquery()
    history = aliased(Job)
    history_scope = _lease_scope(history, has_workspaces)
    history_class = case((history.kind.in_([literal(kind, literal_execute=True) for kind in sorted(INTERACTIVE_KINDS)]),
                         literal(0, literal_execute=True)), else_=literal(1, literal_execute=True))
    inflight = select(func.count()).where(history_scope == tenants.c.scope,
        history_class == tenants.c.class_order,
        history.status.in_(tuple(LEASED))).correlate(tenants).scalar_subquery()
    last_claim = select(func.max(history.started_at)).where(
        history_scope == tenants.c.scope, history_class == tenants.c.class_order,
        or_(history.workspace_id.is_not(None), history.venture_id.is_not(None)),
        history.started_at.is_not(None)).correlate(tenants).scalar_subquery()
    rotation = select(tenants.c.scope, tenants.c.class_order).where(inflight < cap).order_by(
        tenants.c.class_order, inflight, last_claim.asc().nulls_first(), tenants.c.unscoped_priority, tenants.c.oldest, tenants.c.scope)
    rows = []
    skipped = set()
    while len(rows) < n:
        session.flush()
        candidate = session.execute(rotation.where(
            tuple_(tenants.c.scope, tenants.c.class_order).not_in(skipped)).limit(1)).first()
        if candidate is None:
            break
        tenant, selected_class = candidate
        if dialect == "postgresql":
            locked = session.scalar(select(func.pg_try_advisory_xact_lock(
                func.hashtextextended("job-workspace:" + tenant + ":" + str(selected_class), 0))))
            if not locked:
                skipped.add((tenant, selected_class))
                continue
            if session.scalar(select(func.count()).select_from(Job).where(
                    scope == tenant, class_order == selected_class, Job.status.in_(tuple(LEASED)))) >= cap:
                skipped.add((tenant, selected_class))
                continue
        claim = q.where(scope == tenant, class_order == selected_class).order_by(
            class_order,
            Job.priority.asc(), Job.created_at.asc()).limit(1)
        if dialect == "postgresql":
            claim = claim.with_for_update(skip_locked=True)
        row = session.scalar(claim)
        if row is None:
            skipped.add((tenant, selected_class))
            continue
        row.status = JobStatus.LEASED.value
        if row.attempts >= row.max_attempts:
            code = "pagebuilder_retry_limit" if row.kind == "pagebuilder.build" else "job_retry_limit"
            fail_job(session, row.id, FatalError(code), now=clock)
            continue
        row.started_at = clock
        rows.append(row)
    claimed: list[ClaimedJob] = []
    for row in rows:
        log.info("job_queue_wait", extra={"job_id": row.id, "job_kind": row.kind,
            # Wait since the job became due: deferred jobs (quiet hours, retries) are not backlog.
            "job_class": job_class(row.kind), "queue_wait_s": max(0.0,
                (as_utc(clock) - max(as_utc(row.created_at), as_utc(row.run_after or row.created_at))).total_seconds()),
            "job_attempt": (row.attempts or 0) + 1})
        row.status = JobStatus.LEASED.value
        row.locked_by = worker_id
        row.locked_at = clock
        row.started_at = clock
        row.attempts = (row.attempts or 0) + 1
        claimed.append(
            ClaimedJob(
                id=row.id,
                kind=row.kind,
                venture_id=row.venture_id,
                payload=_payload(row),
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                timeout_s=row.timeout_s,
                token_budget=row.token_budget,
                workspace_id=row.workspace_id,
                worker_id=worker_id,
            )
        )
    return claimed


def _leased_row(session: Session, job_id: str, worker_id: str | None, attempt: int | None):
    query = select(Job).where(Job.id == job_id)
    if worker_id is not None:
        query = query.where(Job.locked_by == worker_id, Job.status.in_(tuple(LEASED)))
    if attempt is not None:
        query = query.where(Job.attempts == attempt)
    return session.scalar(query.with_for_update())


def heartbeat(session: Session, job_id: str, *, now=None, worker_id: str | None = None, attempt: int | None = None) -> None:
    row = _leased_row(session, job_id, worker_id, attempt)
    if row is None:
        return
    row.locked_at = now or utcnow()


def complete_job(session: Session, job_id: str, *, now=None, worker_id: str | None = None, attempt: int | None = None) -> None:
    from app.tasks.locks import lock_for_job
    lock_for_job(session, job_id)
    row = _leased_row(session, job_id, worker_id, attempt)
    if row is None:
        return
    row.status = JobStatus.CANCELLED.value if row.cancel_requested else JobStatus.SUCCEEDED.value
    row.finished_at = now or utcnow()
    row.locked_by = None
    row.locked_at = None
    row.last_error = None
    from app.tasks.service import after_job
    after_job(session, job_id)
    from app.ventures import idea_page
    if row.kind in idea_page.REFRESH_ON_JOBS and row.status == JobStatus.SUCCEEDED.value:
        idea_page.schedule(session, row.workspace_id, row.venture_id, actor=_payload(row).get("_actor_subject"))


OTHER_FAILURE_JOBS = frozenset({"content.topics", "content.draft", "gtm.god.run", "gtm.journey.prepare", "import.analyze", "experiment.evaluate"})


def fail_job(session: Session, job_id: str, error: BaseException, *, now=None, worker_id: str | None = None, attempt: int | None = None) -> str | None:
    from app.tasks.locks import lock_for_job
    lock_for_job(session, job_id)
    row = _leased_row(session, job_id, worker_id, attempt)
    if row is None:
        return None
    from core.local_diagnostics import write_error
    write_error(error, code="local_job_failed")
    clock = now or utcnow()
    if row.kind == "research.deep" and not isinstance(error, FatalError) and _call_in_flight(session, job_id):
        error = FatalError("research_outcome_uncertain")
    if row.kind == "research.deep" and isinstance(error, FatalError):
        row.max_attempts = min(row.max_attempts, 3)
    from app.core.ratelimit import RateLimitedJob

    if isinstance(error, RateLimitedJob):
        row.max_attempts += 1
    row.last_error = str(error)[:4000]
    row.locked_by = None
    row.locked_at = None
    fatal = isinstance(error, FatalError) or row.attempts >= row.max_attempts
    if row.cancel_requested:
        row.status, row.finished_at = JobStatus.CANCELLED.value, clock
        return row.status
    if fatal:
        row.status = JobStatus.FAILED_DEAD.value
        row.finished_at = clock
        if row.kind == "pagebuilder.build" and row.last_error == "pagebuilder_retry_limit":
            payload = _payload(row)
            if isinstance(payload.get("_runtime"), dict):
                payload["_runtime"].update(state="failed", stage="failed", error=row.last_error)
                row.payload_json = json.dumps(payload)
        if row.kind == "experiment.run":
            _watchdog_failure(session, row)
        from app.chat.conversations import job_receipt_conversation
        with job_receipt_conversation(_payload(row), venture_id=row.venture_id, workspace_id=row.workspace_id) as qualified:
            if qualified:
                if row.kind == "idea.validate":
                    from app.idea.validation import post_outcome_to_chat
                    p = json.loads(row.payload_json or "{}")
                    post_outcome_to_chat(session, row.id, p["session_id"], p.get("revision_id"), ok=False,
                                         code=str(row.last_error or ""))
                elif row.kind in {"research.deep", "plan.compile", "idea.clarify_turn"} and row.venture_id:
                    from app.chat.milestone_hooks import failed
                    failed(session, row.venture_id, row.kind, row.id, str(row.last_error or ""))
                elif row.venture_id and row.kind in OTHER_FAILURE_JOBS:
                    from app.chat.milestone_hooks import content_failed
                    content_failed(session, row.venture_id, row.kind, row.id, str(row.last_error or ""))
                elif row.kind == "chat.recovery":
                    _watchdog_failure(session, row)
        from app.tasks.service import after_job
        after_job(session, job_id, error=row.last_error)
        return row.status
    delay = min(RETRY_CAP_S, RETRY_BASE_S * math.pow(2, min(16, max(row.attempts - 1, 0))))
    if isinstance(error, RetryableError) and error.retry_after_s is not None:
        from app.connections.composio_pacing import RateLimited
        if isinstance(error, RateLimited):
            delay = max(delay, error.retry_after_s)
            jitter = int(hashlib.sha256(f"{row.id}:{row.attempts}".encode()).hexdigest()[:4], 16) / 65535
            delay += min(5, delay / 10) * jitter
        else:
            delay = error.retry_after_s
    row.status = JobStatus.RETRY_WAIT.value
    row.run_after = clock + timedelta(seconds=delay)
    row.scheduled_for = row.run_after
    from app.tasks.service import after_job
    after_job(session, job_id, error=row.last_error, retrying=True)
    return row.status


def release_lease(session: Session, job_id: str, *, now=None, worker_id: str | None = None, attempt: int | None = None, interrupted: bool = True) -> str | None:
    """Release an owned lease within its retry limit; unknown research outcomes fail closed."""
    from app.tasks.locks import lock_for_job
    lock_for_job(session, job_id)
    row = _leased_row(session, job_id, worker_id, attempt)
    if row is None:
        return
    clock = now or utcnow()
    if row.kind == "research.deep" and interrupted:
        row.max_attempts = min(row.max_attempts, 3)
        error = (FatalError("job_outcome_uncertain") if _call_in_flight(session, row.id)
                 else RetryableError("research_interrupted", retry_after_s=0))
        status = fail_job(session, job_id, error,
                          now=clock, worker_id=worker_id, attempt=attempt)
        from app.analytics.error_events import actor_person_id, record_job_failure
        record_job_failure(session, row, code=str(error), status=status,
                           person_id=actor_person_id(_payload(row).get("_actor_subject")))
        if status == JobStatus.RETRY_WAIT.value:
            row.last_error = None
        return status
    if row.kind == "pagebuilder.build" and not interrupted and row.attempts <= row.max_attempts:
        from app.core.local_mode import is_local
        if is_local():
            payload = _payload(row)
            order_id = (payload.get("tool_build") or {}).get("order_id")
            order = session.get(VentureDocument, order_id) if isinstance(order_id, str) else None
            if (order and order.venture_id == row.venture_id and order.lease_expires_at
                    and as_utc(order.lease_expires_at) > as_utc(clock)):
                from app.build.device_orders import KIND
                try:
                    body = json.loads(order.provenance_json or "{}")
                    active = (order.kind == KIND and body["state"] == "claimed"
                        and body["workspace_id"] == row.workspace_id and body["actor_id"] == payload.get("_actor_subject")
                        and body["task_snapshot"]["pagebuilder_job_id"] == row.id
                        and body["manifest"]["coding_loop_verification"]["operation"] == "workspace_loop_v1")
                except (KeyError, TypeError, ValueError):
                    active = False
                if active:
                    row.attempts = max(1, row.attempts - 1)
    if row.kind == "pagebuilder.build" and row.attempts >= row.max_attempts:
        return fail_job(session, job_id, FatalError("pagebuilder_retry_limit"), now=clock, worker_id=worker_id, attempt=attempt)
    row.status = JobStatus.RETRY_WAIT.value
    row.locked_by = None
    row.locked_at = None
    row.run_after = clock
    row.scheduled_for = clock
    return row.status


async def run_inline(kind: str, payload: dict[str, Any] | None = None) -> str:
    """Enqueue and run one job in-process (tests / local)."""
    from app.db.session import make_engine as _eng, make_session_factory as _sf
    from app.state import db
    from app.worker.handlers import dispatch, install_default_handlers

    install_default_handlers()
    try:
        factory = db()
    except Exception:
        factory = _sf(_eng())
    with factory() as session:
        job_id = enqueue(session, kind, payload or {})
        session.commit()
    with factory() as session:
        claimed = lease(session, "inline", kinds=[kind], n=1)
        session.commit()
    if not claimed:
        return job_id
    ownership = {"worker_id": claimed[0].worker_id, "attempt": claimed[0].attempts}
    try:
        await dispatch(kind, payload or {})
        with factory() as session:
            complete_job(session, claimed[0].id, **ownership)
            session.commit()
    except RetryableError as exc:
        with factory() as session:
            fail_job(session, claimed[0].id, exc, **ownership)
            session.commit()
    except Exception as exc:
        with factory() as session:
            fail_job(session, claimed[0].id, FatalError(str(exc)), **ownership)
            session.commit()
        raise
    return claimed[0].id


def publish_wake() -> None:
    _wake()


def request_cancel(session: Session, job_id: str) -> bool:
    """Stop future work; provider calls already in progress settle normally."""
    from app.tasks.locks import lock_for_job
    lock_for_job(session, job_id)
    row = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if row is None or row.status not in ACTIVE_JOB_STATUSES:
        return False
    row.cancel_requested = True
    if row.status in CLAIMABLE:
        row.status = JobStatus.CANCELLED.value
        row.finished_at = utcnow()
    return True
