"""One durable QA task after builder retirement; no generic code executor."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import re
import secrets

from sqlalchemy import select

from app.connectors.secrets import encrypt_at_rest, decrypt_at_rest
from app.credits import pools
from app.credits._txn import naive_utc
from app.db.supymem_models import QaRun
from app.infra.fargate_build import BuildProviderError, LaunchRejected
from app.infra.fargate_qa import QaProfile, configured_provider, run_request
from app.state import db
from . import lifecycle, qa_receipts, qa_transport
from .artifact_store import S3ArtifactStore
from .finalization import finish
from .model_proxy import BuildModelError, digest
from .orchestration import _begin, _commit, _fence_args, _followup, _revoke_if_needed, _save, LAUNCH_RECOVERY_SECONDS


logger = logging.getLogger(__name__)


def _launch_rejection_code(exc):
    """Expose only fixed, secret-free QA preflight codes to the durable run."""
    code = str(exc)
    return code if re.fullmatch(r"qa_[a-z_]{1,80}", code) else "qa_launch_rejected"


def _row(session, ctx, spec):
    row = session.scalar(select(QaRun).where(QaRun.id == spec["lifecycle"]["qa_id"]).with_for_update().execution_options(populate_existing=True))
    if not row or (row.build_run_id, row.workspace_id, row.venture_id, row.generation) != (ctx.run.id, ctx.run.workspace_id, ctx.run.venture_id, spec["generation"]):
        raise BuildModelError("qa_receipt_scope_invalid")
    receipt = json.loads(row.receipt_json)
    return row, receipt, receipt.setdefault("lifecycle", {})


def _write(row, receipt):
    row.receipt_json = json.dumps(receipt, sort_keys=True)


def _admission(session, ctx, spec):
    if spec.get("credentials_revoked"):
        raise BuildModelError("qa_admission_revoked")
    from .execution import _recheck_inputs
    from app.db.supymem_models import FounderApp
    from app.db.models import Venture
    session.get(Venture, ctx.run.venture_id, with_for_update=True)
    expected = spec["inputs"]
    if spec.get("lifecycle_version") == 3:
        from app.build.subscription_static_controller import recheck
        recheck(session, ctx, spec)
        current = expected
    else:
        current = _recheck_inputs(session, session.get(FounderApp, ctx.run.app_id), expected,
                                  actor=spec["actor"], active_run_id=ctx.run.id)
    if digest(current) != digest(expected):
        raise BuildModelError("qa_approved_inputs_changed")
    # No shortened/hidden QA budget: a late builder cannot consume the time
    # reserved for a whole independent check plus cleanup.
    if not spec["lifecycle"].get("qa_id"):
        bounds = spec["runtime"]["deadlines"]
        if naive_utc() + timedelta(seconds=bounds["qa_seconds"] + bounds["qa_stop_seconds"]) >= datetime.fromisoformat(spec["lifecycle"]["total_deadline"]):
            raise BuildModelError("qa_insufficient_remaining_runtime")


def _error(session, ctx, spec, reason):
    from app.analytics.error_events import closed_code
    logger.warning("publication_qa_failed", extra={"build_run_id": ctx.run.id, "error_code": closed_code(reason)})
    _save(ctx, spec)
    pools.mark_reconciliation(session, ctx, reason)
    return json.loads(ctx.run.spec)


def _reschedule(session, ctx, spec, payload):
    _save(ctx, spec)
    if not finish(session, ctx, spec):
        _followup(session, ctx, json.loads(ctx.run.spec), delay=300 if spec.get("cleanup_state") == "complete" else 30)
    _commit(session, ctx, payload)


def _prepare(payload):
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        if spec["lifecycle"].get("qa_id"):
            _commit(session, ctx, payload)
            return spec["lifecycle"]["qa_id"]
        if spec.get("credentials_revoked"):
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            _reschedule(session, ctx, spec, payload)
            return None
        try:
            _admission(session, ctx, spec)
            profile = QaProfile.from_dict(spec["runtime"]["qa_profile"])
            if spec.get("artifact_profile") == "founder_product_v1":
                product_name = spec["inputs"]["product_context"]["product_name"]
                contract = {"profile": "founder_product_v1", "product_name": product_name,
                            "required_text": [product_name], "actions": [], "visuals": []}
            else:
                contract = spec["inputs"]["task"]["landing_expectations"]["contract"]
        except Exception:
            spec = _error(session, ctx, spec, "qa_approved_contract_unavailable")
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            _reschedule(session, ctx, spec, payload)
            return None
        args = _fence_args(payload, spec)
        _commit(session, ctx, payload)
    try:
        prepared = qa_receipts.prepare(payload["build_run_id"], contract=contract, policy=profile.receipt_policy(), **args)
    except Exception as exc:
        with db()() as session:
            ctx, spec = _begin(session, payload)
            spec = _error(session, ctx, spec, "qa_source_preparation_failed" if not isinstance(exc, BuildModelError) else str(exc))
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            _reschedule(session, ctx, spec, payload)
        return None
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        if spec.get("credentials_revoked"):
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            _reschedule(session, ctx, spec, payload)
            return None
        spec["lifecycle"]["qa_id"] = prepared["id"]
        spec["qa_state"] = "queued"
        spec["lifecycle"]["qa"] = {"cleanup_state": "pending"}
        row, receipt, launch = _row(session, ctx, spec)
        token = secrets.token_urlsafe(32)
        request = run_request(profile, run_id=ctx.run.id, qa_id=row.id, generation=spec["generation"],
                              workspace_id=ctx.run.workspace_id, venture_id=ctx.run.venture_id, app_id=ctx.run.app_id, task_token=token)
        if launch:
            raise BuildModelError("qa_launch_receipt_conflict")
        launch.update(phase="prepared", request_ref=encrypt_at_rest(json.dumps(request, sort_keys=True)), request_digest=digest(request),
                      control_token_ref=encrypt_at_rest(token), profile=profile.snapshot())
        _write(row, receipt)
        _save(ctx, spec)
        _commit(session, ctx, payload)
        return row.id


def _release(session, ctx, spec, row, receipt):
    pools.release_child(session, ctx, reservation_id=spec["lifecycle"]["holds"]["qa"], no_external_call=True)
    spec["lifecycle"]["qa"].update(cleanup_state="complete", no_external_call=True)
    row.state = "failed"
    receipt["lifecycle"]["phase"] = "rejected"
    _write(row, receipt)


def _launch(payload, provider):
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        row, receipt, launch = _row(session, ctx, spec)
        request = json.loads(decrypt_at_rest(launch["request_ref"]))
        if digest(request) != launch["request_digest"]:
            raise BuildModelError("qa_launch_request_corrupt")
        if launch.get("task_arn"):
            return request, launch["task_arn"]
        first = launch.get("first_attempt_at")
        prior = bool(first)
        try:
            _admission(session, ctx, spec)
        except Exception:
            spec = _error(session, ctx, spec, "qa_admission_revoked")
        if spec.get("credentials_revoked") and not prior:
            _release(session, ctx, spec, row, receipt)
            _reschedule(session, ctx, spec, payload)
            return None, None
        discovery = bool(spec.get("credentials_revoked") or launch.get("discovery_only")
                         or first and naive_utc() > datetime.fromisoformat(first) + timedelta(seconds=LAUNCH_RECOVERY_SECONDS))
        if not discovery:
            launch.update(phase="calling", first_attempt_at=first or naive_utc().isoformat())
        _write(row, receipt)
        _save(ctx, spec)
        _commit(session, ctx, payload)
    rejection_code = None
    try:
        arn = provider.discover(request) if discovery else provider.launch(request)
    except LaunchRejected as exc:
        arn, rejection_code = None, _launch_rejection_code(exc)
        logger.warning("QA launch rejected: %s", rejection_code)
    except Exception:
        arn = None
    with db()() as session:
        ctx, spec = _begin(session, payload)
        row, receipt, launch = _row(session, ctx, spec)
        if rejection_code and not prior and not discovery:
            launch["last_error"] = rejection_code
            spec = _error(session, ctx, spec, rejection_code)
            _release(session, ctx, spec, row, receipt)
            _reschedule(session, ctx, spec, payload)
            return None, None
        if not arn:
            launch.update(phase="uncertain", discovery_only=discovery or bool(rejection_code)
                          or spec.get("lifecycle_version") == 3)
            spec["phase"] = "qa_launch_reconciliation_required"
            _write(row, receipt)
            _followup(session, ctx, spec)
            _commit(session, ctx, payload)
            return None, None
        launch.update(task_arn=arn, phase="accepted")
        _write(row, receipt)
        _commit(session, ctx, payload)
    return request, arn


def _observe(payload, provider, request, arn):
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        row, receipt, launch = _row(session, ctx, spec)
        first = launch.get("first_attempt_at")
        if first and naive_utc() > datetime.fromisoformat(first) + timedelta(seconds=spec["runtime"]["deadlines"]["qa_seconds"] + (spec["runtime"]["deadlines"]["qa_stop_seconds"] if launch.get("normal_stop_intent") else 0)):
            spec = _error(session, ctx, spec, "qa_runtime_deadline")
        stop = bool(spec.get("credentials_revoked") or launch.get("normal_stop_intent"))
        if stop:
            launch["stop_intent_at"] = launch.get("stop_intent_at") or naive_utc().isoformat()
        _write(row, receipt)
        _save(ctx, spec)
        _commit(session, ctx, payload)
    try:
        if stop:
            provider.stop(arn, request)
        observation = provider.observe(arn, request, identity_only=stop)
    except Exception as exc:
        with db()() as session:
            ctx, spec = _begin(session, payload)
            if isinstance(exc, BuildProviderError) and str(exc) in {"build_task_image_unverified", "build_task_network_unverified"}:
                spec = _error(session, ctx, spec, "qa_task_attestation_failed")
            spec["phase"] = "qa_observation_pending"
            _followup(session, ctx, spec)
            _commit(session, ctx, payload)
        return
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        row, receipt, launch = _row(session, ctx, spec)
        launch["observation"] = observation
        if observation["state"] == "STOPPED":
            if not launch.get("normal_stop_intent") and not spec.get("credentials_revoked"):
                spec = _error(session, ctx, spec, "qa_task_stopped_before_report")
            try:
                lifecycle.settle_task(session, ctx, spec, "qa", observation, arn)
            except BuildModelError as exc:
                spec = _error(session, ctx, spec, str(exc))
            _write(row, receipt)
            _reschedule(session, ctx, spec, payload)
            return
        if stop or spec.get("credentials_revoked") or observation["state"] != "RUNNING":
            _write(row, receipt)
            _followup(session, ctx, spec)
            _commit(session, ctx, payload)
            return
        prior = receipt.get("trusted_task")
        observed_binding = observation["task_binding"]
        if prior:
            if {k: v for k, v in prior.items() if k != "observed_at"} != {k: v for k, v in observed_binding.items() if k != "observed_at"}:
                spec = _error(session, ctx, spec, "qa_task_binding_changed")
                _followup(session, ctx, spec)
                _commit(session, ctx, payload)
                return
            observed_binding = prior
        spec["qa_state"] = "running"
        _save(ctx, spec)
        _write(row, receipt)
        args, qid = _fence_args(payload, spec), row.id
        binding = {"build_id": ctx.run.id, "qa_id": qid, "generation": spec["generation"],
                   "endpoint": observation["endpoint"], "token": decrypt_at_rest(launch["control_token_ref"])}
        _commit(session, ctx, payload)
    qa_receipts.bind_task(qid, observation=observed_binding, **args)
    _command(payload, binding)


def _command(payload, binding):
    def permit():
        with db()() as session:
            ctx, spec = _begin(session, payload)
            spec = _revoke_if_needed(session, ctx, spec)
            _admission(session, ctx, spec)
            _save(ctx, spec)
            _commit(session, ctx, payload)
    with db()() as session:
        ctx, spec = _begin(session, payload)
        row, receipt, launch = _row(session, ctx, spec)
        request_input, source, scope, binding_digest = receipt["input"], receipt["source"], receipt["scope"], receipt["task_binding_digest"]
    error = None
    try:
        current = qa_transport.progress(binding, request_input, lambda: S3ArtifactStore.configured().source(source, scope), permit_execution=permit)
        if current["phase"] == "completed":
            raw, evidence = qa_transport.result(binding, current)
            qa_receipts.capture(binding["qa_id"], report_blob=raw, evidence=evidence, task_binding_digest=binding_digest,
                                **_fence_args(payload, spec))
        elif current["phase"] in {"failed", "interrupted"}:
            error = "qa_command_failed_or_interrupted"
    except BuildModelError as exc:
        # A lost object-store acknowledgement is deliberately recoverable: the
        # next watch replays the same frozen report against the same immutable
        # keys and verifies the existing objects. Other model errors are fixed
        # controller validation failures and cannot be repaired by polling.
        if str(exc) == "qa_store_outcome_uncertain":
            error = "qa_transfer_or_capture_pending:StoreOutcomeUncertain"
            logger.warning("QA report storage outcome is uncertain; retrying the immutable receipt")
        else:
            error = str(exc)
            logger.warning("QA report rejected: %s", exc)
    except Exception as exc:
        error = f"qa_transfer_or_capture_pending:{type(exc).__name__}"
        logger.warning("QA transfer or capture raised %s", type(exc).__name__)
    with db()() as session:
        ctx, spec = _begin(session, payload)
        row, receipt, launch = _row(session, ctx, spec)
        if receipt.get("phase") == "stored":
            launch["normal_stop_intent"] = {"qa_id": row.id, "input_digest": row.input_digest,
                "report_digest": row.report_digest, "task_binding_digest": receipt["task_binding_digest"]}
            spec["phase"] = "qa_stopping"
            spec["qa_state"] = receipt["outcome"]
            if receipt["outcome"] != "passed":
                spec = _error(session, ctx, spec, "qa_runtime_failed" if receipt.get("report_complete") is False else "qa_checks_failed")
        elif error and not error.startswith("qa_transfer_or_capture_pending:"):
            spec = _error(session, ctx, spec, error)
        else:
            spec["phase"] = "qa_running"
        launch["last_error"] = error
        _write(row, receipt)
        _followup(session, ctx, spec)
        _commit(session, ctx, payload)


def _refresh_usage(payload):
    """Read-only cloud recovery for omitted terminal timestamps, never RunTask."""
    from app.infra.fargate_build import configured_provider as builder_provider
    for purpose in ("builder", "qa"):
        with db()() as session:
            ctx, spec = _begin(session, payload)
            slot = spec["lifecycle"].get(purpose, {})
            if not slot.get("terminal_observation") or slot.get("settlement_receipt"):
                continue
            if purpose == "builder":
                launch = spec["launch"]
            else:
                _row_value, _receipt, launch = _row(session, ctx, spec)
            profile, arn = launch["profile"], launch["task_arn"]
            request = json.loads(decrypt_at_rest(launch["request_ref"]))
            if digest(request) != launch["request_digest"]:
                raise BuildModelError("qa_launch_request_corrupt")
        try:
            provider = builder_provider(profile) if purpose == "builder" else configured_provider(profile)
            observation = provider.observe(arn, request, identity_only=True)
        except Exception:
            continue
        with db()() as session:
            ctx, spec = _begin(session, payload)
            try:
                lifecycle.settle_task(session, ctx, spec, purpose, observation, arn)
            except BuildModelError as exc:
                spec = _error(session, ctx, spec, str(exc))
            _save(ctx, spec)
            _commit(session, ctx, payload)


def watch(payload):
    _refresh_usage(payload)
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        if lifecycle.cleanup_complete(spec):
            _reschedule(session, ctx, spec, payload)
            return
    qid = _prepare(payload)
    if qid is None:
        return
    with db()() as session:
        ctx, spec = _begin(session, payload)
        row, receipt, launch = _row(session, ctx, spec)
        profile = launch["profile"]
    try:
        provider = configured_provider(profile)
    except BuildProviderError:
        with db()() as session:
            ctx, spec = _begin(session, payload)
            spec["phase"] = "qa_runtime_configuration_unavailable"
            _followup(session, ctx, spec)
            _commit(session, ctx, payload)
        return
    request, arn = _launch(payload, provider)
    if arn:
        _observe(payload, provider, request, arn)
