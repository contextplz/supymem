"""APP_ROLE=worker entry: lease loop, heartbeat, SIGTERM grace (docs/supymem/09 §1)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import signal
import sqlite3
import sys
import time
import traceback
import uuid
from threading import Event, Thread

from sqlalchemy.exc import OperationalError

from app.core.jobs import (
    FatalError,
    INTERACTIVE_KINDS,
    LOCAL_MANAGED_BUILD_CODE,
    RetryableError,
    complete_job,
    fail_job,
    heartbeat,
    is_local_managed_job,
    job_class,
    lease,
    publish_wake,
    release_lease,
)
from app.core.local_mode import is_local
from app.core.local_worker import redis_url
from app.worker.catalog import enabled_kinds
from app.worker.handlers import dispatch, install_default_handlers

log = logging.getLogger("app.worker")

POLL_S = 2.0
HEARTBEAT_S = 15.0
GRACE_S = 110.0
CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "8"))
INTERACTIVE_SLOTS = int(os.environ.get("WORKER_INTERACTIVE_SLOTS", "1"))
MAINTENANCE_S = float(os.environ.get("WORKER_MAINTENANCE_SECONDS", "60"))
_last_loop_at: float | None = None


def _recover_orphaned_builds() -> int:
    """Repair saved local pages and durable build watches before leasing work.

    A worker can stop after persisting an uncertain cloud launch but before its
    follow-up watch is claimable.  The normal credit sweep repairs that outbox,
    but production must not wait for a separately scheduled maintenance job
    after a worker deployment.  This recovery is DB-only: it cannot launch,
    stop, or inspect an ECS task by itself.
    """
    try:
        if is_local():
            from app.core.jobs import reclaim_stale
            from app.state import db

            with db()() as session:
                reclaim_stale(session)
                session.commit()
        from app.builder.orchestration import enqueue_orphaned_watches

        created = enqueue_orphaned_watches()
        if created:
            log.info("recovered %s orphaned build watch jobs", created)
        return created
    except Exception:  # noqa: BLE001 -- startup remains available for other work
        log.exception("orphaned build watch recovery failed")
        return 0


def _run_maintenance() -> None:
    """Reconcile durable credit holds without relying on an external ticker.

    Production may temporarily lose its EventBridge/internal-tick delivery.  A
    worker that can lease paid work must therefore also keep its credit ledger
    moving.  The sweep is DB-only, idempotent, and row-locked; running it from
    more than one worker cannot duplicate settlement or provider calls.
    """
    from app.credits.reserve import sweep_expired
    from app.build.device_orders import sweep_device_leases
    from app.core.jobs import expire_overdue_paid_journey, reclaim_stale
    from app.state import db
    from app.approvals.service import expire_due, sweep_notifications

    # This pass runs even when all execution slots are occupied. `lease()`
    # cannot reclaim them in that state, and provider threads may stall while
    # their independent heartbeat continues. The paid journey jobs become an
    # explicit uncertain terminal result instead of spinning forever.
    with db()() as session:
        reclaimed = reclaim_stale(session)
        overdue = expire_overdue_paid_journey(session)
        session.commit()
    from app.consumer import referrals
    sweep = getattr(referrals, "sweep_activations", None)
    if sweep and not is_local():
        with db()() as session:
            sweep(session)
            session.commit()
    if reclaimed or overdue:
        log.warning("reconciled delayed jobs", extra={"reclaimed": reclaimed, "overdue": overdue})
    from app.tasks.usage import reconcile_usage
    from app.research.execution import reconcile_interrupted
    from app.idea.runner import reconcile_interrupted_clarifications
    from app.chat.requests import reconcile_stale_turns
    from app.hosting.service import reconcile_uncertain_publications
    actions = (("credits", sweep_expired), ("device_leases", sweep_device_leases),
                         ("research_receipts", reconcile_interrupted),
                         ("clarification_receipts", reconcile_interrupted_clarifications),
                         ("stale_chat_turns", reconcile_stale_turns),
                         ("uncertain_publications", reconcile_uncertain_publications),
                         ("task_usage", reconcile_usage), ("approval_expiry", expire_due),
                         ("approval_reminders", sweep_notifications))
    if is_local():
        from app.builder.orchestration import enqueue_orphaned_watches
        from app.imports.cleanup import sweep as sweep_imports
        from app.inbox.store import cleanup as cleanup_inbox

        actions += (("build_watches", enqueue_orphaned_watches), ("import_retirement", sweep_imports),
                    ("inbox_cleanup", cleanup_inbox))
    for name, action in actions:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 -- one failed sweep must not prevent the others
            log.error("maintenance action failed", extra={"action": name, "error_type": type(exc).__name__})


def _run_scheduler() -> dict:
    from app.state import db
    from app.tasks.scheduler import run_due
    from app.tasks.service import fire_due
    from core.clock import utcnow

    with db()() as session:
        result = run_due(session)
        session.commit()
    if result["maintenance_due"]:
        # Helpers own independent transactions. A crash here skips at most one
        # 60s safety sweep; the next DB fire repeats their idempotent cleanup.
        _run_maintenance()
    if result["ids"]:
        publish_wake()
    for tick in result["ticks"]:
        log.info("maintenance tick fired", extra=tick)
    with db()() as session:
        from app.core.local_worker import capability_enabled, spend_available

        if not is_local() or capability_enabled("routines") and spend_available():
            result["tasks"] = fire_due(session, now=utcnow())
        session.commit()
    if result["tasks"]:
        publish_wake()
    return result


async def _scheduler_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(_run_scheduler)
        except Exception:  # noqa: BLE001 -- scheduling must never stop the worker
            log.exception("database scheduler failed")
        try:
            if stop.is_set():
                break
            delay = random.uniform(MAINTENANCE_S * 0.8, MAINTENANCE_S * 1.2) if is_local() else MAINTENANCE_S
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            continue


def _readiness(worker_id: str) -> tuple[dict, int]:
    from core.health import Check, liveness_payload, readiness_payload

    age = time.time() - _last_loop_at if _last_loop_at is not None else None
    fresh = age is not None and 0 <= age <= 60
    payload, status = readiness_payload("worker", [Check("loop", fresh, True,
        "Your background work is running." if fresh else "Quit and reopen the app to restart your background work.")])
    return {**liveness_payload("worker"), **payload, "worker_id": worker_id, "last_loop_at": _last_loop_at}, status


def _start_health(worker_id: str) -> None:
    """Internal /health for compose and ECS (doc 22 worker health)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from app.core.local_mode import is_local
    from core.health import liveness_payload
    from core.local_readiness import readiness_headers
    from desktop_runtime.local_http import valid_loopback_host

    port = int(os.environ.get("WORKER_HEALTH_PORT") or os.environ.get("PORT") or "3301")
    local = is_local()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            if local and not valid_loopback_host(self.headers.get_all("Host", []), self.server.server_port):
                self.send_error(403, "Quit and reopen your app, then retry.")
                return
            if local and self.path == "/local-ready":
                headers = readiness_headers(self.headers.get("x-local-ready-nonce"))
                status = 403 if headers is None else _readiness(worker_id)[1]
                self.send_response(204 if status == 200 else status)
                for name, value in (headers or {"Cache-Control": "no-store"}).items():
                    self.send_header(name, value)
                self.end_headers()
                return
            if self.path.rstrip("/") in {"", "/health", "/ready"}:
                payload, status = (_readiness(worker_id) if self.path.rstrip("/") == "/ready"
                    else ({**liveness_payload("worker"), "worker_id": worker_id}, 200))
                body = (json.dumps(payload) + "\n").encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1" if local else "0.0.0.0", port), Handler)
    Thread(target=server.serve_forever, name="worker-health", daemon=True).start()
    log.info("worker health on :%s", port)



class _Wake:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def ping(self) -> None:
        self._event.set()

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except TimeoutError:
            return
        self._event.clear()


async def _redis_listener(wake: _Wake) -> None:
    url = redis_url()
    if not url:
        return
    try:
        import redis.asyncio as redis
    except ImportError:
        return
    try:
        client = redis.from_url(url)
        pubsub = client.pubsub()
        await pubsub.subscribe("jobs:wake")
        async for _msg in pubsub.listen():
            wake.ping()
    except Exception:
        log.exception("jobs:wake listener failed; falling back to poll")


def _job_scope(job):
    from app.authz.http import journey_resources
    from app.authz.workspace import assert_venture_access, assert_workspace_access, subject_person_id
    from app.tenancy import Scope
    from app.worker.handlers import hosted_mode
    from app.workspaces.service import role_of

    wid, vid = job.workspace_id, job.venture_id
    actor = str(job.payload.get("_actor_subject") or "system")
    if job.kind in {"content.topics", "content.draft", "gtm.god.run", "import.analyze", "import.cleanup", "task.run", "pagebuilder.build", "pagebuilder.publish", "simulation.rehearse", "experiment.run"} and not vid:
        raise FatalError("job_scope_missing")
    if not wid:
        if job.kind == "experiment.run" or job.kind.startswith(("idea.", "research.", "plan.", "content.", "build.", "deploy.", "app.", "import.", "infra.provision", "pagebuilder.", "simulation.")):
            raise FatalError("job_scope_missing")
        return Scope("none", "", "system")
    if job.payload.get("workspace_id") not in {None, wid} or job.payload.get("venture_id") not in {None, vid}:
        raise FatalError("job_scope_mismatch")
    if job.kind == "build.watch" and job.payload.get("runtime_version") == 1:
        from app.builder.orchestration import cleanup_scope
        if not cleanup_scope(job):
            raise FatalError("build_watch_scope_invalid")
        # This persisted watch may observe/stop/settle after account revocation.
        # Its handler independently rechecks founder authority before any new
        # cloud launch, command or model invocation.
        return Scope(wid, vid or "", actor)
    if job.kind == "import.cleanup":
        from app.imports.cleanup import cleanup_scope
        if not cleanup_scope(job):
            raise FatalError("import_cleanup_scope_invalid")
        return Scope(wid, vid or "", actor)
    if job.kind == "import.analyze" and job.payload.get("schema_version") == 1:
        from app.imports.runner import cleanup_scope
        if not cleanup_scope(job):
            raise FatalError("import_scope_invalid")
        # Exact-object read/receipt reconciliation only after revocation;
        # the handler rechecks authority before acquisition or report publication.
        return Scope(wid, vid or "", actor)
    if hosted_mode():
        if actor in {"system", "anonymous", "local", "founder"}:
            raise FatalError("job_actor_missing")
        assert_workspace_access(wid, actor)
        if vid and assert_venture_access(vid, actor)[1] != wid:
            raise FatalError("job_scope_mismatch")
        if role_of(wid, subject_person_id(actor)) in {None, "viewer"}:
            raise FatalError("job_authority_revoked")
    for resource_wid, resource_vid in journey_resources(
            {**job.payload, "workspace_id": wid, "venture_id": vid}, job_kind=job.kind):
        if resource_wid != wid or (vid and resource_vid and resource_vid != vid):
            raise FatalError("job_scope_mismatch")
    return Scope(wid, vid or "", actor)


def _record_failure(session, job, code: str, status: str | None) -> None:
    """Analytics row + alarm line for a failed attempt; never changes how the job fails."""
    if status is None:
        return
    try:
        from app.analytics.error_events import actor_person_id, record_job_failure

        record_job_failure(session, job, code=code, status=status,
                           person_id=actor_person_id(job.payload.get("_actor_subject")))
    except Exception:  # noqa: BLE001
        log.warning("job failure receipt unavailable")


def _stage_funnel(session, job, outcome: str) -> None:
    """Funnel event for this job outcome, staged in the outcome's own transaction. Never raises."""
    try:
        from app.analytics.error_events import actor_person_id
        from app.analytics.funnel import stage_job_outcome

        stage_job_outcome(session, job, outcome, person_id=actor_person_id(job.payload.get("_actor_subject")))
    except Exception:  # noqa: BLE001
        log.warning("funnel event unavailable")


def _funnel_outcome(factory, job, outcome: str) -> None:
    try:
        with factory() as session:
            _stage_funnel(session, job, outcome)
            session.commit()
    except Exception:  # noqa: BLE001
        log.warning("funnel event unavailable")


async def _run_one(factory, job, running: set[tuple[str, int]], bulk_running=None) -> None:
    from app.tenancy import bind
    from core.request_id import current_request_id, request_context

    trace = request_context(job.payload.get("_trace_id"))
    trace.__enter__()
    log.info("worker_job_started", extra={"job_id": job.id, "request_id": current_request_id(), "trace_id": current_request_id()})
    ownership = {"worker_id": job.worker_id, "attempt": job.attempts}
    duplicate_delivery = False
    try:
        if is_local_managed_job(job.kind, job.payload):
            raise FatalError(LOCAL_MANAGED_BUILD_CODE)
        scope = _job_scope(job)
        from app.tasks.service import before_job
        with factory() as session:
            from app.tasks.locks import lock_for_job
            lock_for_job(session, job.id)
            from app.core.jobs import _leased_row
            saved = _leased_row(session, job.id, job.worker_id, job.attempts)
            if saved is None:
                return
            if is_local() and not saved.cancel_requested:
                from datetime import timedelta
                from app.core.local_worker import blocked_reason
                from core.clock import utcnow

                reason = blocked_reason(job.kind)
                if reason:
                    saved.attempts = max(0, saved.attempts - 1)
                    session.flush()
                    release_lease(session, job.id, now=utcnow() + timedelta(seconds=60), interrupted=False,
                                  worker_id=job.worker_id, attempt=saved.attempts)
                    saved.last_error = reason
                    session.commit()
                    return
            allowed = before_job(session, job.id, job.attempts)
            if not allowed or saved and saved.cancel_requested:
                if saved:
                    from app.tasks.service import job_step
                    task, _, _ = job_step(session, job.id)
                    if task and task.state == "paused":
                        from datetime import timedelta
                        from app.credits._txn import naive_utc
                        saved.attempts = max(0, saved.attempts - 1)
                        session.flush()
                        release_lease(session, job.id, interrupted=False, worker_id=job.worker_id, attempt=saved.attempts)
                        saved.run_after = saved.scheduled_for = naive_utc() + timedelta(seconds=60)
                    else:
                        saved.cancel_requested = True
                        complete_job(session, job.id, **ownership)
                session.commit()
                return
            session.commit()
        _funnel_outcome(factory, job, "started")
        with bind(workspace_id=scope.workspace_id, venture_id=scope.venture_id, actor=scope.actor):
            from app.chat.conversations import bind_job_conversation
            from contextlib import ExitStack
            with ExitStack() as contexts:
                from app.chat.store import REPLY_REQUEST_ID
                contexts.callback(REPLY_REQUEST_ID.reset, REPLY_REQUEST_ID.set(job.payload.get("_chat_request_nonce") or ""))
                from core.time_context import timezone_context
                from app.tasks.time_context import founder_timezone
                if zone := founder_timezone(scope.actor):
                    contexts.enter_context(timezone_context(zone))
                from app.tasks.usage import job_context
                contexts.enter_context(job_context(job.id, kind=job.kind))
                try:
                    contexts.enter_context(bind_job_conversation(job.payload, venture_id=scope.venture_id, workspace_id=scope.workspace_id))
                except (ValueError, PermissionError) as exc:
                    raise FatalError("job_conversation_scope_mismatch") from exc
                await dispatch(job.kind, {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts})
        if is_local() and job.kind == "pagebuilder.build":
            from app.pagebuilder import runtime
            saved = runtime.read(job.id)
            if saved.status == "succeeded":
                return
            state = json.loads(saved.payload_json)["_runtime"]
            if state["state"] == "failed":
                raise FatalError(state.get("error") or "pagebuilder_generation_failed")
            if state["state"] != "ready":
                from datetime import timedelta
                from core.clock import utcnow
                with factory() as session:
                    status = release_lease(session, job.id, now=utcnow() + timedelta(seconds=15), interrupted=False, **ownership)
                    session.commit()
                if status == "failed_dead":
                    from app.pagebuilder.worker import _post_failure
                    await asyncio.to_thread(_post_failure, job)
                return
        with factory() as session:
            complete_job(session, job.id, **ownership)
            _stage_funnel(session, job, "completed")
            session.commit()
        if job.kind == "pagebuilder.build":
            from app.chat.conversations import bind_job_conversation
            from app.chat.site_actions import page_built
            with bind(workspace_id=scope.workspace_id, venture_id=scope.venture_id, actor=scope.actor), bind_job_conversation(
                    job.payload, venture_id=scope.venture_id, workspace_id=scope.workspace_id):
                await asyncio.to_thread(page_built, job.id)
    except RetryableError as exc:
        with factory() as session:
            from app.core.ratelimit import RateLimitedJob

            error = exc if isinstance(exc, RateLimitedJob) else RetryableError("provider_retryable:" + type(exc).__name__, retry_after_s=exc.retry_after_s)
            status = fail_job(session, job.id, error, **ownership)
            if job.kind != "gtm.gmail.read" or not isinstance(error, RateLimitedJob):
                _record_failure(session, job, str(error), status)
            if job.kind == "import.analyze":
                from app.imports.runner import ImportRetry
                if isinstance(exc, ImportRetry) and exc.retry_after:
                    from datetime import timedelta
                    from app.credits._txn import naive_utc
                    from app.db.models import Job
                    saved = session.get(Job, job.id)
                    if saved.status == "retry_wait" and saved.attempts == job.attempts:
                        earliest = naive_utc() + timedelta(seconds=exc.retry_after)
                        saved.run_after = saved.scheduled_for = max(earliest, naive_utc(saved.run_after))
            session.commit()
    except Exception as exc:
        if isinstance(exc, FatalError) and str(exc) in {"clarification_operation_in_progress", "research_operation_in_progress", "plan_operation_in_progress", "content_operation_in_progress", "import_operation_in_progress", "import_cleanup_operation_in_progress", "pagebuilder_operation_in_progress", "simulation_operation_in_progress"}:
            duplicate_delivery = True
            return  # Duplicate delivery does not own completion/failure of this attempt.
        page_code = str(exc).partition(":")[0] if job.kind == "pagebuilder.build" and isinstance(exc, ValueError) else ""
        if re.fullmatch(r"pagebuilder_[a-z_]{1,80}", page_code):
            exc = FatalError(page_code)
        error_code = str(exc) if isinstance(exc, FatalError) else "handler_failed:" + type(exc).__name__
        from core.redact import redact_string
        from guardrails.pii import detect_pii
        from app.munder.redact import redact_secrets
        from app.boundary.detectors.vault import known_values

        exact_values = list(known_values(job.workspace_id)) + [hit.excerpt for hit in detect_pii(str(exc))]
        log.error("job failed", extra={"job_id": job.id, "kind": job.kind,
            "error_type": type(exc).__name__, "error_code": redact_string(redact_secrets(error_code), exact_values=exact_values),
            "exception_message": redact_string(redact_secrets(str(exc)),
                exact_values=exact_values)[:512],
            "traceback": "\n".join(f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(sys.exc_info()[2], limit=-8))[:2048]})
        # Store bounded operational errors, not provider bodies or user content.
        # Unknown safe handler failures get one retry. Consequential/uncertain
        # effects retain the original fail-closed behavior and are never replayed.
        with factory() as session:
            from app.core.jobs import _call_in_flight
            uncertain = _call_in_flight(session, job.id)
        unsafe_retry = uncertain or job.kind in {"email.send", "pagebuilder.publish", "deploy.run", "deploy.unpublish"}
        failure = exc if isinstance(exc, FatalError) else (
            FatalError(error_code) if unsafe_retry or job.attempts >= 2 else RetryableError(error_code))
        with factory() as session:
            status = fail_job(session, job.id, failure, **ownership)
            _record_failure(session, job, error_code, status)
            session.commit()
    finally:
        log.info("worker_job_finished", extra={"job_id": job.id, "request_id": current_request_id(), "trace_id": current_request_id()})
        trace.__exit__(None, None, None)
        if not duplicate_delivery:
            if bulk_running is not None:
                bulk_running.discard((job.id, job.attempts))
            running.discard((job.id, job.attempts))


def _heartbeat_loop(factory, running: set[tuple[str, int]], stop: Event, worker_id: str) -> None:
    while not stop.wait(HEARTBEAT_S):
        if not running:
            continue
        try:
            with factory() as session:
                for job_id, attempt in running.copy():
                    heartbeat(session, job_id, worker_id=worker_id, attempt=attempt)
                session.commit()
            from app.worker.health import write as write_health
            if not is_local():
                write_health("running")
        except Exception:
            log.exception("lease heartbeat failed")


def _lease_available(session, worker_id, kinds, running, bulk_running, classes):
    slots = max(0, CONCURRENCY - len(running))
    interactive = "interactive" in classes and (kinds is None or bool(INTERACTIVE_KINDS.intersection(kinds)))
    bulk = "bulk" in classes and (kinds is None or bool(set(kinds) - INTERACTIVE_KINDS))
    bulk_reserve = int(bulk and not bulk_running and CONCURRENCY > 1)
    claimed = lease(session, worker_id, kinds=kinds, job_classes=["interactive"], n=max(0, slots - bulk_reserve)) if interactive else []
    reserve = min(INTERACTIVE_SLOTS, max(0, CONCURRENCY - 1)) if interactive else 0
    # shortcut: one mixed slot cannot isolate both classes, use concurrency >= 2 or class-specific workers under sustained load.
    bulk_slots = min(slots - len(claimed), max(0, CONCURRENCY - reserve - len(bulk_running)))
    if bulk and bulk_slots:
        claimed.extend(lease(session, worker_id, kinds=kinds, job_classes=["bulk"], n=bulk_slots))
    if interactive and len(claimed) < slots:
        claimed.extend(lease(session, worker_id, kinds=kinds, job_classes=["interactive"], n=slots - len(claimed)))
    return claimed


async def run_worker(*, once: bool = False, stop: asyncio.Event | None = None) -> None:
    from desktop_runtime.hosted import validate_local_state

    validate_local_state()
    from app.core.local_process import process_lock
    from app.core.local_env_import import import_local_env

    import_local_env()

    with process_lock():
        await _run_worker(once=once, stop=stop)


async def _run_worker(*, once: bool = False, stop: asyncio.Event | None = None) -> None:
    global _last_loop_at
    from app import bootstrap as bootstrap_mod
    from app.state import db

    local = is_local()
    embedded = local and stop is not None
    if not embedded:
        if local and os.environ.get("SUPYMEM_DESKTOP_INSTANCE_ID"):
            bootstrap_mod.bootstrap(recover_onboarding=False)
        else:
            bootstrap_mod.bootstrap()
    install_default_handlers()
    if local:
        from app.core.local_worker import install_handlers

        install_handlers()
    _last_loop_at = None
    factory = db()
    _recover_orphaned_builds()
    worker_id = f"wkr_{uuid.uuid4().hex[:12]}"
    kinds = sorted(enabled_kinds())
    raw_classes = os.environ.get("WORKER_JOB_CLASSES", "all").strip()
    classes = {"interactive", "bulk"} if raw_classes == "all" else {part.strip() for part in raw_classes.split(",")}
    if not classes or not classes <= {"interactive", "bulk"} or CONCURRENCY < 1 or INTERACTIVE_SLOTS < 0:
        raise ValueError("invalid worker capacity or job classes")
    log.info("worker %s kinds=%s classes=%s interactive_slots=%s", worker_id, kinds, sorted(classes), INTERACTIVE_SLOTS)
    if not embedded and (not local or os.environ.get("WORKER_HEALTH_PORT")):
        _start_health(worker_id)
    stop = stop if embedded else asyncio.Event()
    running: set[tuple[str, int]] = set()
    bulk_running: set[tuple[str, int]] = set()
    wake = _Wake()
    loop = asyncio.get_running_loop()

    def finished_slot(slot):
        # A duplicate keeps the durable lease untouched, not an idle process slot.
        running.discard(slot)
        bulk_running.discard(slot)
        wake.ping()

    # Sync handlers and per-job checks run via asyncio.to_thread. The default pool
    # (cpu + 4 threads) let a few slow bulk jobs (email.send) starve interactive turns.
    from concurrent.futures import ThreadPoolExecutor
    if not embedded:
        loop.set_default_executor(ThreadPoolExecutor(max_workers=CONCURRENCY * 2 + 4, thread_name_prefix="job"))

    def _sigterm() -> None:
        log.info("SIGTERM: stop leasing")
        from app.worker.health import write as write_health
        if not local:
            write_health("stopping")
        stop.set()

    for sig in (() if embedded else (signal.SIGTERM, signal.SIGINT)):
        try:
            loop.add_signal_handler(sig, _sigterm)
        except NotImplementedError:
            pass

    listener = asyncio.create_task(_redis_listener(wake))
    heartbeat_stop = Event()
    hb = Thread(target=_heartbeat_loop, args=(factory, running, heartbeat_stop, worker_id),
                name="worker-heartbeat", daemon=True)
    hb.start()
    scheduler = None if once else asyncio.create_task(_scheduler_loop(stop))

    def claim_available(running_slots, bulk_slots):
        with factory() as session:
            claimed = _lease_available(session, worker_id, kinds, running_slots, bulk_slots, classes)
            session.commit()
            return claimed

    try:
        while not stop.is_set():
            slots = max(0, CONCURRENCY - len(running))
            if slots:
                try:
                    claimed = (await asyncio.to_thread(claim_available, running.copy(), bulk_running.copy())
                               if local else claim_available(running, bulk_running))
                except OperationalError as exc:
                    if not (local and isinstance(exc.orig, sqlite3.OperationalError)
                            and exc.orig.sqlite_errorcode & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}):
                        raise
                    log.warning("local database busy; retrying job lease")
                    await wake.wait(POLL_S)
                    continue
                from app.worker.health import write as write_health
                if not local:
                    write_health("running")
                for job in claimed:
                    running.add((job.id, job.attempts))
                    if job_class(job.kind) == "bulk":
                        bulk_running.add((job.id, job.attempts))
                    task = asyncio.create_task(_run_one(factory, job, running, bulk_running))
                    task.add_done_callback(lambda completed, slot=(job.id, job.attempts): finished_slot(slot))
            _last_loop_at = time.time()
            if once:
                break
            await wake.wait(POLL_S)
        if running:
            try:
                await asyncio.wait_for(_drain(running), timeout=GRACE_S)
            except TimeoutError:
                from app.core.jobs import (
                    _call_in_flight,
                    _deploy_switch_may_have_happened,
                )
                from app.db.models import Job
                with factory() as session:
                    for job_id, attempt in running.copy():
                        row = session.get(Job, job_id)
                        if row and row.kind == "idea.clarify_turn" and _call_in_flight(session, job_id):
                            # ponytail: Fargate stops at 120s, pre-drain deployments to finish longer calls.
                            continue
                        if row and _deploy_switch_may_have_happened(session, row):
                            continue
                        release_lease(session, job_id, worker_id=worker_id, attempt=attempt)
                    session.commit()
    finally:
        _last_loop_at = None
        heartbeat_stop.set()
        await asyncio.to_thread(hb.join, HEARTBEAT_S)
        from app.worker.health import write as write_health
        if not local:
            write_health("stopped")
        listener.cancel()
        stop.set()
        if scheduler is not None:
            if local:
                await scheduler
            else:
                scheduler.cancel()
        if local:
            await asyncio.gather(listener, return_exceptions=True)


async def _drain(running: set[tuple[str, int]]) -> None:
    while running:
        await asyncio.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--local-ticker", action="store_true")
    args = parser.parse_args(argv)
    from core.logging import configure_json_logging
    configure_json_logging("company_os.worker", stream=sys.stderr if is_local() else None)
    logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    os.environ.setdefault("APP_ROLE", "worker")
    from core.env_contract import ensure_boot_env
    from app.admin.authn import validate_security_settings
    from core.logging import install_sensitive_url_filters
    ensure_boot_env(service="core")
    validate_security_settings()
    install_sensitive_url_filters()
    if is_local():
        from fastapi import HTTPException
        from app.api.local_settings import import_env_settings
        try:
            import_env_settings()
        except HTTPException:
            pass
    from llm_gateway.provider_foundry import log_platform_routing_config
    log_platform_routing_config()
    from app.worker.health import write as write_health
    if not is_local():
        write_health("starting")
    try:
        asyncio.run(run_worker(once=args.once))
    finally:
        if not is_local():
            write_health("stopped")
    if args.local_ticker:
        publish_wake()
    return 0


if __name__ == "__main__":
    sys.exit(main())
