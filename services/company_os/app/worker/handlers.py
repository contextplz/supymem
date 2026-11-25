"""Registered handlers for kinds this image can actually run."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.local_mode import is_local
from app.worker.catalog import enabled_kinds

log = logging.getLogger("app.worker.handlers")

Handler = Callable[[dict[str, Any]], Awaitable[None] | None]
HANDLERS: dict[str, Handler] = {}



# These repository adapters currently contain deterministic fixtures or local
# infrastructure shortcuts. They remain callable only in explicit local mode.
UNAVAILABLE_HOSTED = {
    "idea.generate_candidates": "Use the metered idea.clarify_turn workflow for candidate proposals.",
    "idea.recon": "The legacy recon job lacks the canonical metered evidence contract.",
    "build.run": "An isolated hosted build provider is not integrated.",
    "build.watch": "A real hosted build controller is not integrated.",
    "qa.run": "The hosted QA executor is not integrated.",
    "deploy.run": "Verified hosted deployment is not integrated.",
    "deploy.unpublish": "Use the target-specific founder-confirmed static Website or chat workflow.",
    "infra.provision": "Managed founder database/auth provisioning is not integrated.",
    "infra.sleep_idle": "The cloud resource sleep/wake controller is not integrated.",
    "infra.reconcile": "The hosted infrastructure reconciler is not integrated.",
    "app.debug": "The hosted debugging executor is not integrated.",
    "app.iterate": "The hosted product iteration executor is not integrated.",
    "import.analyze": "The imported-code sandbox has not passed hosted isolation acceptance.",
}


def hosted_mode() -> bool:
    if is_local():
        return False
    from app.admin.authn import require_signed_caller
    return require_signed_caller() or os.getenv("APP_ENV", "local").lower() in {"production", "staging", "prod"}


def require_capability(kind: str) -> None:
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_worker import blocked_reason
        from app.core.ratelimit import RateLimitedJob
        if reason := blocked_reason(kind):
            error = RateLimitedJob(60)
            error.args = (reason,)
            raise error
        return
    from app.core.jobs import FatalError
    if hosted_mode() and kind in UNAVAILABLE_HOSTED:
        raise FatalError("capability_unavailable:" + kind)


def require_capability_http(kind: str) -> None:
    from fastapi import HTTPException
    if hosted_mode() and kind in UNAVAILABLE_HOSTED:
        raise HTTPException(503, detail={"code": "capability_unavailable", "capability": kind,
                                       "message": UNAVAILABLE_HOSTED[kind]})

def register(kind: str, fn: Handler) -> None:
    if kind not in enabled_kinds():
        raise RuntimeError(f"refusing to register {kind}: not enabled for this image")
    HANDLERS[kind] = fn


async def dispatch(kind: str, payload: dict[str, Any]) -> None:
    from app.core.jobs import FatalError, LOCAL_MANAGED_BUILD_CODE, is_local_managed_job

    if is_local_managed_job(kind, payload):
        raise FatalError(LOCAL_MANAGED_BUILD_CODE)
    managed = kind in {"build.run", "build.watch"} and payload.get("runtime_version") == 1
    managed_import = kind == "import.analyze" and payload.get("schema_version") == 1
    managed_deploy = kind in {"deploy.run", "deploy.unpublish"} and payload.get("schema_version") == 1
    managed_infra = kind == "infra.provision" and payload.get("schema_version") == 1
    if not managed and not managed_import and not managed_deploy and not managed_infra:
        require_capability(kind)
    handler = HANDLERS.get(kind)
    if handler is None:
        raise RuntimeError(f"no handler registered for {kind}")
    from app.core.ratelimit import check_job
    from fastapi import HTTPException

    try:
        await asyncio.to_thread(check_job, kind)
    except HTTPException as exc:
        from app.core.ratelimit import RateLimitedJob

        raise RateLimitedJob(int(exc.headers["Retry-After"])) from exc
    from app.core.jobs import job_class
    from llm_gateway.limiter import request_class
    from app.core.scheduled import FOUNDER_JOB, KINDS, authorize_founder, claim_founder, finish_founder
    claim = None
    if kind in KINDS and payload.get("founder_id"):
        await asyncio.to_thread(authorize_founder, payload)
        claim = await asyncio.to_thread(claim_founder, kind, payload)
        if claim is None:
            return
    token = FOUNDER_JOB.set(claim is not None)
    succeeded = False
    try:
        with request_class(job_class(kind)):
            if inspect.iscoroutinefunction(handler):
                await handler(payload)
            else:
                result = await asyncio.to_thread(handler, payload)
                if inspect.isawaitable(result):
                    await result
        succeeded = True
    finally:
        FOUNDER_JOB.reset(token)
        if claim:
            await asyncio.to_thread(finish_founder, claim, succeeded=succeeded)


def _ops_signal_sweep(_payload: dict[str, Any]) -> None:
    from app.watch.bridge import sweep_all

    sweep_all()


async def _ops_slate(_payload: dict[str, Any]) -> None:
    from app.will.driver import drive_open_work

    await drive_open_work()


async def _ops_brain_cycle(_payload: dict[str, Any]) -> None:
    from app.gbrain.cycle import brain_cycle

    await brain_cycle()


async def _chat_recovery(payload: dict[str, Any]) -> None:
    from app.chat.recovery import run_alternative

    await run_alternative(payload)


def _credits_sweep(_payload: dict[str, Any]) -> None:
    from app.builder.orchestration import enqueue_orphaned_watches
    from app.credits.reserve import sweep_expired

    sweep_expired()
    from app.consumer.referrals import sweep_rewards
    # ponytail: rewards wait for the existing five-minute sweep, use a dedicated worker if lower latency matters
    try:
        sweep_rewards()
    except Exception:  # noqa: BLE001 - referral rewards must not stop the rest of the credits sweep
        logging.getLogger(__name__).warning("referral_reward_sweep_failed", exc_info=True)
    enqueue_orphaned_watches()  # DB-only outbox repair; never cloud/provider I/O.
    from app.imports.cleanup import sweep
    sweep()  # Exact import retirement ledger maintenance; no S3 I/O.


async def _autopilot_tick(_payload: dict[str, Any]) -> None:
    from app.autopilot.service import tick, tick_all
    from app.tenancy import current_or_none

    if _payload.get("founder_id"):
        await asyncio.to_thread(_autopilot_housekeeping, _payload["founder_id"])
        await tick_all(_payload["founder_id"])
        return

    scope = current_or_none()
    if scope and scope.workspace_id != "none" and scope.venture_id:
        # An actor-bound wakeup may only advance its own venture; it is not
        # a capability to schedule every other founder under this actor.
        await tick(scope.venture_id)
        return

    from app.core.scheduled import fanout
    await asyncio.to_thread(fanout, "autopilot.tick", _payload)


def _autopilot_housekeeping(founder_id):
    from app.needs_you.service import remind_due
    from app.connections.composio_pacing import RateLimited

    try:
        from app.chat.connect import check_pending

        from app.needs_you.readiness import sync_all

        check_pending(founder_id=founder_id)
        sync_all(founder_id=founder_id)
    except RateLimited:
        raise
    except Exception:  # noqa: BLE001
        log.warning("connect_pending_failed", exc_info=True)
    remind_due(founder_id=founder_id)
    try:
        from app.chat.proactive import sweep_all
        sweep_all(founder_id=founder_id)
    except Exception:  # noqa: BLE001
        log.warning("proactive_sweep_failed", exc_info=True)


def _suggestions_generate(_payload: dict[str, Any]) -> None:
    from app.suggestions.service import generate, generate_all

    if is_local():
        from app.autopilot.authority import founder_scope
        from app.autopilot.guard import get_settings
        from app.core.local_worker import _targets

        for venture_id in _targets(_payload):
            try:
                with founder_scope(venture_id, autonomous=True):
                    if get_settings(venture_id)["god_mode"]:
                        generate(venture_id)
            except Exception as exc:
                log.warning("Open Tasks and review your saved work, then retry the background work you requested.",
                    extra={"venture_id": venture_id, "error_type": type(exc).__name__})
        return

    generate_all()  # fresh, non-duplicate cards per active venture; each venture isolated


def _analytics_rollup(_payload: dict[str, Any]) -> None:
    from app.analytics.rollup import rollup_today
    from app.product_analytics.service import rollup_all_apps

    rollup_today()
    rollup_all_apps()


def _retention_purge(_payload: dict[str, Any]) -> None:
    from app.core.local_mode import is_local
    if is_local():
        from app.ventures.lifecycle import purge_expired as purge_deleted_ventures
        purge_deleted_ventures()
        return

    from app.inbox.store import cleanup as cleanup_inbox
    cleanup_inbox()
    import logging
    import os
    if os.environ.get("SUPYMEM_ENABLE_DATA_RETENTION_PURGE", "0") != "1":
        logging.getLogger("app.worker").info("retention purge skipped", extra={"reason": "founder_data_deletion_not_authorized", "deleted_rows": 0})
        return
    from app.worker.retention import purge_expired
    purge_expired()


def _lead_library_sweep(_payload: dict[str, Any]) -> None:
    from app.lead_library.service import sweep
    sweep()


def _idea_recon(payload: dict[str, Any]):
    from app.idea.recon import run_recon

    return run_recon(payload)


def _idea_clarify(payload: dict[str, Any]) -> None:
    from app.idea.service import run_clarify_turn

    run_clarify_turn(payload)


async def _idea_page_refresh(payload: dict[str, Any]) -> None:
    from app.ventures.idea_page import refresh

    await refresh(payload)


def _idea_candidates(payload: dict[str, Any]) -> None:
    require_capability("idea.generate_candidates")
    from app.idea.candidates import generate_candidates

    generate_candidates(payload)


def _idea_validate(payload: dict[str, Any]) -> None:
    require_capability("idea.validate")
    from app.idea.validation import run_validation

    run_validation(payload)


def _research_deep(payload: dict[str, Any]):
    require_capability("research.deep")
    from app.research.deep import run_deep

    result = run_deep(payload)
    from app.chat.milestone_hooks import research_done
    research_done(payload)
    return result


def _plan_compile(payload: dict[str, Any]) -> None:
    from app.planning.compiler import run_compile

    run_compile(payload)
    from app.chat.milestone_hooks import plan_done
    plan_done(payload)


def _gtm_diagnosis(payload: dict[str, Any]) -> None:
    from app.gtm.god.generation import run_diagnosis
    run_diagnosis(payload)


def _content_topics(payload: dict[str, Any]) -> None:
    from app.content.generation import run_topics

    run_topics(payload)


def _content_draft(payload: dict[str, Any]) -> None:
    from app.content.generation import run_draft

    run_draft(payload)


def _loot_drop_refresh(payload: dict[str, Any]) -> None:
    if is_local() and payload.get("tick"):
        return
    from app.research.prior_art.loot_drop import run_refresh
    run_refresh(payload)


async def _cos_brief_tick(payload: dict[str, Any]) -> None:
    from app.inbox.weekly import tick

    if is_local() and payload.get("founder_id"):
        import json
        from sqlalchemy import select
        from app.db.inbox_models import InboxBriefingState
        from app.db.models import Workspace
        from app.state import db

        with db()() as session:
            saved = session.scalar(select(InboxBriefingState.data_json).join(Workspace,
                Workspace.id == InboxBriefingState.workspace_id).where(
                InboxBriefingState.founder_id == payload["founder_id"],
                Workspace.owner_id == payload["founder_id"], Workspace.archived_at.is_(None),
                InboxBriefingState.workspace_id == payload.get("workspace_id"),
                InboxBriefingState.mailbox_id == ""))
        try:
            values = json.loads(saved or "{}")
        except (ValueError, TypeError):
            return
        if not isinstance(values, dict) or not isinstance(values.get("cos_brief"), dict):
            return
    await tick(payload)


def _scope_classify(payload: dict[str, Any]) -> None:
    from app.core.jobs import FatalError
    raise FatalError("plan_scope_requires_reviewed_plan")


def _estimate_compute(payload: dict[str, Any]) -> None:
    from app.planning.resources import snapshot
    return snapshot(payload["workspace_id"], payload["venture_id"])


def _build_run(payload: dict[str, Any]) -> None:
    if is_local():
        if not payload.get("build_run_id"):
            from app.builder.ctl import run_build
            return run_build(payload)
        from app.builder.execution import run_local
        return run_local(payload)
    if payload.get("runtime_version") == 1:
        from app.builder.orchestration import run
        return run(payload)
    require_capability("build.run")
    from app.builder.ctl import run_build

    run_build(payload)


def _build_watch(payload: dict[str, Any]) -> None:
    if is_local():
        from app.builder.execution import watch_local
        return watch_local(payload)
    if payload.get("runtime_version") == 1:
        from app.builder.orchestration import watch
        return watch(payload)
    require_capability("build.watch")
    from app.builder.ctl import watch_build

    watch_build(payload)


def _qa_run(payload: dict[str, Any]) -> None:
    require_capability("qa.run")
    from app.builder.ctl import run_qa

    run_qa(payload)


def _deploy_unpublish(payload: dict[str, Any]) -> None:
    from app.hosting.unpublish import execute
    execute(payload)


def _deploy_run(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") == 1 and payload.get("kind") == "firstlook":
        from app.hosting.firstlook_publish import publish

        publish(payload)
        return
    if payload.get("schema_version") == 1:
        from app.hosting.service import publish_accepted_build

        publish_accepted_build(
            payload["deployment_id"],
            payload["app_id"],
            payload["build_id"],
            actor=payload["actor"],
        )
        return
    if is_local():
        from app.builder.execution import local_caller, local_tool
        from app.core.local_owner import current_owner
        from app.pagebuilder.api import start

        actor = payload.get("_actor_subject") or current_owner().id
        local_tool(payload["venture_id"], actor)
        return start(payload["venture_id"], local_caller(actor))
    require_capability("deploy.run")
    from app.db.models import Venture
    from app.landing.service import generate_and_publish
    from app.state import db as _db

    factory = _db()
    with factory() as session:
        ven = session.get(Venture, payload["venture_id"])
        idea = ven.raw_idea if ven else ""
    generate_and_publish(payload["venture_id"], payload["workspace_id"], idea or "")


def _infra_provision(payload: dict[str, Any]) -> None:
    from app.core.jobs import FatalError, LOCAL_MANAGED_BUILD_CODE, RetryableError, is_local_managed_job

    if is_local_managed_job("infra.provision", payload):
        raise FatalError(LOCAL_MANAGED_BUILD_CODE)
    if payload.get("schema_version") == 1:
        from app.provisioning.database_controller import reconcile

        result = reconcile(payload["runtime_intent_id"])
        receipts = result.get("receipts", {})
        if result.get("phase") == "runtime_qa" and result.get("state") == "review":
            return
        image_observation = receipts.get("image_observation", {})
        if image_observation.get("state") in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}:
            raise FatalError("founder_image_build_failed")
        if (image_observation.get("state") == "SUCCEEDED"
                and image_observation.get("scan_status") == "COMPLETE"
                and (image_observation.get("critical", 0) or image_observation.get("high", 0))):
            raise FatalError("founder_image_scan_failed")
        observation = receipts.get("database_observation", {})
        if observation.get("state") == "STOPPED" and not observation.get("succeeded"):
            raise FatalError("founder_database_provision_failed")
        migration = receipts.get("migration_observation", {})
        if migration.get("state") == "STOPPED" and not migration.get("succeeded"):
            raise FatalError("founder_application_migration_failed")
        service = receipts.get("service_observation", {})
        if service.get("failed") is True:
            raise FatalError("founder_application_service_failed")
        raise RetryableError("founder_runtime_provision_pending")
    require_capability("infra.provision")
    from app.infra.apps import provision

    provision(payload)


def _infra_sleep(payload: dict[str, Any]) -> None:
    require_capability("infra.sleep_idle")
    from app.infra.apps import sleep_app

    if payload.get("app_id"):
        sleep_app(payload["app_id"])


def _infra_reconcile(payload: dict[str, Any]) -> None:
    require_capability("infra.reconcile")
    from app.infra.apps import reconcile

    reconcile(payload)


def _app_debug(payload: dict[str, Any]) -> None:
    require_capability("app.debug")
    from app.debug.service import run_debug

    run_debug(payload)


async def _task_run(payload: dict[str, Any]) -> None:
    from app.planning.execute import run_task_job

    await run_task_job(payload)


def _app_iterate(payload: dict[str, Any]) -> None:
    require_capability("app.iterate")
    from app.iterate.service import run_iterate

    run_iterate(payload)


def _import_analyze(payload: dict[str, Any]) -> None:
    from app.core.jobs import FatalError
    if payload.get("schema_version") != 1:
        raise FatalError("import_legacy_contract_unavailable")
    from app.imports.runner import run_analyze
    run_analyze(payload)


def _import_cleanup(payload: dict[str, Any]) -> None:
    from app.imports.cleanup import run_cleanup
    run_cleanup(payload)


def _ingest_batch(_payload: dict[str, Any]) -> None:
    from app.product_analytics.service import rollup_all_apps

    rollup_all_apps()


def _experiment_evaluate(payload: dict[str, Any]) -> None:
    from app.experiments.service import run_evaluate

    run_evaluate(payload)


def _learning_cycle(payload: dict[str, Any]) -> None:
    from app.learning.cycle import run_cycle

    run_cycle(payload)


def _stage_evidence_module():
    """Optional S04 dependency: this image cannot advertise an absent callee."""
    from importlib import import_module
    try:
        module = import_module("app.ventures.stage")
    except ImportError:
        return None
    return module if all(callable(getattr(module, name, None))
        for name in ("evidence_context", "reassess_evidence")) else None


def _milestone_reassess(payload: dict[str, Any]):
    """Consume one scoped evidence outbox job, without models or new tasks."""
    import json
    from datetime import timedelta
    from sqlalchemy import select
    from sqlalchemy.exc import InterfaceError, OperationalError
    from app.authz.workspace import subject_person_id
    from app.builder.access import authorize
    from app.core.jobs import FatalError, RetryableError, STALE_LOCK_S, _leased_row
    from app.credits._txn import naive_utc
    from app.db.models import Person, Venture, Workspace
    from app.secrets.detect import looks_like_secret
    from app.state import db
    from app.tenancy import current_or_none

    module = _stage_evidence_module()
    if module is None:
        raise FatalError("stage_reassessment_unavailable")
    try:
        if (not isinstance(payload, dict) or not isinstance(payload.get("_job_id"), str)
                or not isinstance(payload.get("_worker_id"), str) or not payload["_worker_id"]
                or type(payload.get("_job_attempt")) is not int or payload["_job_attempt"] < 1):
            raise ValueError("stage_evidence_invalid")
        with db()() as session:
            job = _leased_row(session, payload["_job_id"], payload["_worker_id"], payload["_job_attempt"])
            if (not job or job.kind != "milestone.reassess" or getattr(job, "cancel_requested", False) or not job.locked_at
                    or naive_utc(job.locked_at) <= naive_utc() - timedelta(seconds=STALE_LOCK_S)):
                raise ValueError("stage_evidence_invalid")
            saved = json.loads(job.payload_json)
            allowed = {"event_key", "workspace_id", "venture_id", "_actor_subject", "_chat_conversation"}
            runtime = {"_job_id", "_worker_id", "_job_attempt"}
            key = saved.get("event_key") if isinstance(saved, dict) else None
            if (not isinstance(saved, dict) or not set(saved) <= allowed
                    or set(payload) != set(saved) | runtime
                    or any(payload[name] != value for name, value in saved.items())
                    or not isinstance(key, str) or not 1 <= len(key) <= 256 or looks_like_secret(key)
                    or job.max_attempts != 2 or job.dedupe_key != "stage.evidence:" + key
                    or job.idempotency_key != job.dedupe_key):
                raise ValueError("stage_evidence_invalid")
            scope = current_or_none()
            if (not scope or not job.workspace_id or not job.venture_id
                    or (scope.workspace_id, scope.venture_id, scope.actor)
                        != (job.workspace_id, job.venture_id, saved.get("_actor_subject"))
                    or (saved.get("workspace_id"), saved.get("venture_id")) != (job.workspace_id, job.venture_id)):
                raise PermissionError("stage_reassessment_scope_mismatch")
            owner = subject_person_id(scope.actor)
            workspace = session.execute(select(Workspace.owner_id, Workspace.archived_at, Workspace.settings_json)
                .where(Workspace.id == job.workspace_id)).one_or_none()
            venture = session.execute(select(Venture.workspace_id, Venture.archived_at)
                .where(Venture.id == job.venture_id)).one_or_none()
            try:
                settings = json.loads(workspace.settings_json or "{}") if workspace else None
            except (TypeError, ValueError):
                settings = None
            if (not workspace or workspace.owner_id != owner or workspace.archived_at
                    or not venture or venture.workspace_id != job.workspace_id or venture.archived_at
                    or not isinstance(settings, dict) or settings.get("blocked")
                    or session.scalar(select(Person.id).where(Person.id == owner)) is None):
                raise PermissionError("stage_reassessment_scope_mismatch")
            try:
                authorize(session, workspace_id=job.workspace_id, venture_id=job.venture_id, actor=owner, mutate=True)
            except (KeyError, PermissionError) as error:
                raise PermissionError("stage_reassessment_scope_mismatch") from error
            context = module.evidence_context(job.venture_id, key, workspace_id=job.workspace_id)
            if (not isinstance(context, dict) or set(context) != {"venture_id", "workspace_id", "owner_person_id"}
                    or (context["venture_id"], context["workspace_id"], context["owner_person_id"])
                        != (job.venture_id, job.workspace_id, owner)):
                raise PermissionError("stage_reassessment_scope_mismatch")
            result = module.reassess_evidence(job.venture_id, key, workspace_id=job.workspace_id)
            if result is None or isinstance(result, bool):
                raise ValueError("stage_evidence_invalid")
            return result
    except (OperationalError, InterfaceError) as error:
        raise RetryableError("stage_reassessment_database_retryable") from error
    except PermissionError as error:
        raise FatalError("stage_reassessment_scope_mismatch") from error
    except (KeyError, TypeError, ValueError) as error:
        raise FatalError("stage_evidence_invalid") from error


def _chat_reminder(payload: dict[str, Any]) -> None:
    from app.chat.reminders import deliver

    deliver(payload)


def _email_send(payload: dict[str, Any]) -> None:
    from app.email.service import deliver

    deliver(payload["message_id"], int(payload.get("_job_attempt") or 1), job_id=payload.get("_job_id"))


def _email_schedule(_payload: dict[str, Any]) -> None:
    from app.email.triggers import run_schedule
    from app.release_notices.service import reconcile

    run_schedule()
    reconcile()


def _pagebuilder_build(payload: dict[str, Any]) -> None:
    from app.pagebuilder.jobs import run

    return run(payload)


def _pagebuilder_publish(payload: dict[str, Any]) -> None:
    from app.pagebuilder_admission.publication import run

    run(payload)


async def _gtm_journey_prepare(payload: dict[str, Any]) -> None:
    from app.gtm.journey import run_prepare

    await run_prepare(payload)


async def _experiment_run(payload: dict[str, Any]) -> None:
    from app.experiments.autonomous import run_job

    await run_job(payload)


async def _market_beliefs_tick(payload: dict[str, Any]) -> None:
    from app.experiments.belief_autopilot import run_job
    await run_job(payload)


async def _entry_exploration(payload: dict[str, Any]) -> None:
    from app.entry.chat_start import run_exploration
    await run_exploration(payload)


async def _gtm_journey_maintain(_payload: dict[str, Any]) -> None:
    from app.gtm.journey import maintain_all
    if _payload.get("founder_id"):
        await maintain_all(founder_id=_payload["founder_id"])
    else:
        from app.tenancy import current_or_none
        scope = current_or_none()
        if scope and scope.workspace_id not in {"", "none"}:
            await maintain_all()
            return
        from app.core.scheduled import fanout
        await asyncio.to_thread(fanout, "gtm.journey.maintain", _payload)


async def _gmail_read(payload):
    from app.autopilot.outreach import poll_replies
    from app.core.scheduled import FOUNDER_JOB, enqueue_mailboxes
    from app.gtm.supymem.workers import claim_read, release_read
    if not payload.get("mailbox_id"):
        await asyncio.to_thread(enqueue_mailboxes, workspace_id=payload["workspace_id"],
            cursor=payload.get("mailbox_cursor", ""), window=payload.get("window"))
        return
    def target():
        from sqlalchemy import select
        from app.db.models import Venture
        from app.gtm.supymem.pipeline import _gmail_workspace_ids
        from app.state import db
        with db()() as session:
            return session.scalar(select(Venture.id).where(Venture.workspace_id.in_(_gmail_workspace_ids(session, payload["workspace_id"])),
                Venture.archived_at.is_(None)).order_by(Venture.created_at, Venture.id).limit(1))
    venture_id = await asyncio.to_thread(target)
    if not venture_id:
        return
    claim = await asyncio.to_thread(claim_read, payload["workspace_id"], payload["mailbox_id"], window=payload.get("window"))
    if claim is None:
        return
    token = FOUNDER_JOB.set(True)
    try:
        result = await poll_replies(venture_id, force=True, mailbox_id=claim[4], read_claim=claim)
        if result.get("retry_after_s"):
            from app.connections.composio_pacing import RateLimited
            raise RateLimited(result["retry_after_s"])
    except Exception as exc:
        await asyncio.to_thread(release_read, claim, error=exc)
        raise
    else:
        await asyncio.to_thread(release_read, claim)
    finally:
        FOUNDER_JOB.reset(token)


async def _outreach_retry(payload):
    from app.autopilot.outreach import _send_batch_admitted
    from app.autopilot.send_admission import try_send_admission
    from app.core.jobs import RetryableError
    with try_send_admission(payload["venture_id"]) as admitted:
        if not admitted:
            raise RetryableError("outreach_send_busy")
        await _send_batch_admitted(payload["venture_id"], payload["request"], actor_subject=payload.get("_actor_subject"))


async def _gtm_linkedin_search(payload: dict[str, Any]) -> None:
    from app.gtm.exa_people import run

    await run(payload)


async def _simulation_rehearse(payload: dict[str, Any]) -> None:
    from app.simulation.jobs import handle

    await handle(payload)


def install_default_handlers() -> None:
    from app.tasks.routines import fire as routine_fire, report as routine_report
    from app.inbox.briefing import tick as inbox_tick
    from app.memory.extraction import run_capture
    mapping: dict[str, Handler] = {
        "routines.fire": routine_fire,
        "routines.report": routine_report,
        "inbox.briefing.tick": inbox_tick,
        "inbox.cos_brief.tick": _cos_brief_tick,
        "memory.extract": run_capture,
        "ops.signal_sweep": _ops_signal_sweep,
        "ops.slate": _ops_slate,
        "ops.brain_cycle": _ops_brain_cycle,
        "credits.sweep": _credits_sweep,
        "autopilot.tick": _autopilot_tick,
        "suggestions.generate": _suggestions_generate,
        "analytics.rollup": _analytics_rollup,
        "analytics.ingest_batch": _ingest_batch,
        "retention.purge": _retention_purge,
        "lead_library.sweep": _lead_library_sweep,
        "idea.recon": _idea_recon,
        "idea.clarify_turn": _idea_clarify,
        "idea.page_refresh": _idea_page_refresh,
        "chat.recovery": _chat_recovery,
        "chat.reminder": _chat_reminder,
        "idea.generate_candidates": _idea_candidates,
        "idea.validate": _idea_validate,
        "research.deep": _research_deep,
        "browse.web": lambda payload: __import__("app.tools.browse", fromlist=["handle"]).handle(payload),
        "knowledge.loot_drop_refresh": _loot_drop_refresh,
        "pagebuilder.build": _pagebuilder_build,
        "pagebuilder.publish": _pagebuilder_publish,
        "simulation.rehearse": _simulation_rehearse,
        "plan.compile": _plan_compile,
        "task.run": _task_run,
        "tasks.execute": _durable_task,
        "tasks.verify": _verify_task,
        "content.topics": _content_topics,
        "gtm.god.run": _gtm_diagnosis,
        "gtm.journey.prepare": _gtm_journey_prepare,
        "gtm.journey.maintain": _gtm_journey_maintain,
        "gtm.gmail.read": _gmail_read,
        "gtm.outreach.retry": _outreach_retry,
        "gtm.linkedin.search": _gtm_linkedin_search,
        "content.draft": _content_draft,
        "scope.classify": _scope_classify,
        "estimate.compute": _estimate_compute,
        "build.run": _build_run,
        "build.watch": _build_watch,
        "qa.run": _qa_run,
        "deploy.run": _deploy_run,
        "deploy.unpublish": _deploy_unpublish,
        "infra.provision": _infra_provision,
        "infra.sleep_idle": _infra_sleep,
        "infra.reconcile": _infra_reconcile,
        "app.debug": _app_debug,
        "app.iterate": _app_iterate,
        "import.analyze": _import_analyze,
        "import.cleanup": _import_cleanup,
        "experiment.evaluate": _experiment_evaluate,
        "experiment.run": _experiment_run,
        "experiment.beliefs.tick": _market_beliefs_tick,
        "experiment.exploration.prepare": _entry_exploration,
        "learning.cycle": _learning_cycle,
        "email.send": _email_send,
        "email.schedule": _email_schedule,
    }
    if "milestone.reassess" in enabled_kinds() and _stage_evidence_module() is not None:
        mapping["milestone.reassess"] = _milestone_reassess
    for kind, fn in mapping.items():
        if kind in enabled_kinds() and kind not in HANDLERS:
            HANDLERS[kind] = fn
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_worker import install_handlers
        install_handlers()


async def _durable_task(payload: dict[str, Any]) -> None:
    from app.tasks.execution import execute
    await execute(payload)


async def _verify_task(payload: dict[str, Any]) -> None:
    from app.tasks.verification import verify
    await verify(payload)
