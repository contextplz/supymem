"""Fenced Mac-step leases; native orders remain the sole effect admission path.

S07's authenticated native routes call these helpers in their own transactions.
Only claim responses contain the raw lease token. It is hashed in the database.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta, timezone

from sqlalchemy import select

from app.credits._txn import naive_utc
from app.db.models import CreditReservation, Venture, VentureDocument, Workspace
from app.db.supymem_models import DesktopDevice
from app.db.task_models import TaskEvent, TaskRun, TaskStep
from app.tasks import service
from app.tenancy import current

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15


def _serialize(session, key):
    from app.db.session import serialize_writes
    if (session.get_bind().dialect.name == "sqlite"
            and session.connection().connection.driver_connection.in_transaction):
        return  # The caller already wrote; SQLite cannot BEGIN IMMEDIATE twice.
    serialize_writes(session, key=key)


def _locked(session, task_id, step_id, device_id):
    from app.authz.workspace import subject_person_id
    from app.tasks.locks import lock_step

    scope = current()
    task, run, step = lock_step(session, step_id)
    _serialize(session, "task-device:" + task_id)
    if (not task or task.id != task_id or (task.workspace_id, task.venture_id or "", task.owner_person_id)
            != (scope.workspace_id, scope.venture_id, subject_person_id(scope.actor))):
        raise PermissionError("task_device_scope_mismatch")
    workspace = session.get(Workspace, task.workspace_id)
    venture = session.get(Venture, task.venture_id) if task.venture_id else None
    device = session.get(DesktopDevice, device_id)
    if (not workspace or workspace.owner_id != task.owner_person_id
            or (task.venture_id and (not venture or venture.workspace_id != task.workspace_id))
            or not device or device.person_id != task.owner_person_id or device.revoked_at):
        raise PermissionError("task_device_scope_mismatch")
    if not run or not step or run.task_id != task.id or step.runs_on not in {"device", "either"}:
        raise PermissionError("task_device_step_mismatch")
    return task, run, step


def _evidence(step):
    value = json.loads(step.evidence_json or "{}")
    if isinstance(value, list):
        refs = [item.get("ref") if isinstance(item, dict) else item for item in value]
        if any(not isinstance(ref, str) for ref in refs):
            raise ValueError("task_device_checkpoint_invalid")
        return {"refs": refs, "effect_outcome": "none"}
    if not isinstance(value, dict):
        raise ValueError("task_device_checkpoint_invalid")
    return value


def _invalidate(step):
    step.lease_token, step.lease_expires_at = None, None
    step.lease_version += 1


def _reconciliation(session, task, run, step):
    if task.state in service.TERMINAL | {"paused"} or run.state in service.TERMINAL | {"paused"}:
        _invalidate(step)
        return
    reason = {"class": "needs_input", "reason": "An earlier Mac action needs its result checked before we can continue.",
              "unblock_action": "Review the saved result. This action will not repeat automatically.",
              "reconciliation_required": True}
    service.block(session, task, reason, run=run, step=step)
    step.state = "reconciliation_required"
    _invalidate(step)


def _stale(session, task, run, step):
    if _evidence(step).get("effect_outcome") in {"calling", "uncertain"}:
        evidence = _evidence(step)
        evidence["effect_outcome"] = "uncertain"
        step.evidence_json = json.dumps(evidence)
        _reconciliation(session, task, run, step)
    else:
        reason = {"class": "needs_device_online", "reason": "Your Mac stopped responding.",
                  "unblock_action": "Open Supymem on the same Mac to resume from saved progress."}
        service.block(session, task, reason, run=run, step=step)
        step.state = "waiting_for_device"
        _invalidate(step)


def _result(step, *, accepted, reason=None):
    value = {"accepted": accepted, "step_id": step.id, "state": step.state,
             "lease_version": step.lease_version,
             "lease_expires_at": step.lease_expires_at.replace(tzinfo=timezone.utc).isoformat()
             if step.lease_expires_at else None}
    if reason:
        value["reason"] = reason
    return value


def _duration(value):
    if type(value) is not int or not 1 <= value <= LEASE_SECONDS:
        raise ValueError("task_device_lease_duration_invalid")
    return timedelta(seconds=value)


def claim_step(session, task_id, step_id, device_id, duration_seconds=LEASE_SECONDS, *, now=None):
    duration, moment = _duration(duration_seconds), naive_utc(now)
    task, run, step = _locked(session, task_id, step_id, device_id)
    evidence = _evidence(step)
    if evidence.get("effect_outcome") == "uncertain" or step.state == "reconciliation_required":
        if step.state != "reconciliation_required":
            _reconciliation(session, task, run, step)
        return _result(step, accepted=False, reason="reconciliation_required")
    if step.lease_owner and step.lease_owner != device_id:
        raise PermissionError("task_device_move_requires_approval")
    if step.lease_token and step.lease_expires_at and naive_utc(step.lease_expires_at) > moment:
        return _result(step, accepted=False, reason="lease_active")
    if task.state in service.TERMINAL | {"paused"} or run.state in service.TERMINAL:
        return _result(step, accepted=False, reason="task_not_active")
    if evidence.get("effect_outcome") == "calling":
        _reconciliation(session, task, run, step)
        return _result(step, accepted=False, reason="reconciliation_required")
    if step.lease_token and step.lease_expires_at and naive_utc(step.lease_expires_at) <= moment:
        _stale(session, task, run, step)
    waiting = json.loads(task.blocker_json or "{}")
    if (step.state not in {"submitted", "waiting_for_device"}
            or ((task.state in service.WAITING or run.state in service.WAITING)
                and waiting.get("class") != "needs_device_online")):
        return _result(step, accepted=False, reason="task_not_ready")
    # The same authenticated Mac supplies the missing input. Re-enter working
    # before applying the ordinary run guard, which rejects waiting states.
    from app.tasks.budgets import is_legacy_profile
    # A server-bound legacy order has one immutable execution. Reacquiring its
    # lease changes the fence, rather than starting another logical attempt.
    attempt_delta = 0 if is_legacy_profile(task, run) and run.attempts else 1
    run.state = task.state = "working"
    if not service.guard(session, task, run, attempts=attempt_delta):
        service.block(session, task, service.blocker("budget_exhausted"), run=run, step=step)
        return _result(step, accepted=False, reason="budget_exhausted")
    token = secrets.token_urlsafe(32)
    run.attempts += attempt_delta
    step.lease_owner, step.lease_token = device_id, hashlib.sha256(token.encode()).hexdigest()
    step.lease_expires_at, step.lease_version = moment + duration, step.lease_version + 1
    step.state, step.started_at = "working", step.started_at or moment
    run.state = task.state = "working"
    task.blocker_json = None
    service.event(session, task, "device_resumed", "Your Mac is working on this step.", run_id=run.id, step_id=step.id)
    return {**_result(step, accepted=True), "lease_token": token, "heartbeat_seconds": HEARTBEAT_SECONDS}


def _fenced(session, task, run, step, device_id, token, version, moment):
    if (step.state != "working" or task.state in service.TERMINAL | service.WAITING
            or run.state in service.TERMINAL | service.WAITING or step.lease_owner != device_id
            or type(version) is not int or step.lease_version != version
            or not isinstance(token, str) or not 20 <= len(token) <= 128 or not step.lease_token
            or not hmac.compare_digest(step.lease_token, hashlib.sha256(token.encode()).hexdigest())):
        raise PermissionError("task_device_lease_fence_invalid")
    if not step.lease_expires_at or naive_utc(step.lease_expires_at) <= moment:
        _stale(session, task, run, step)
        return False
    return True


def heartbeat_step(session, task_id, step_id, device_id, lease_token, lease_version, *, now=None):
    moment = naive_utc(now)
    task, run, step = _locked(session, task_id, step_id, device_id)
    if not _fenced(session, task, run, step, device_id, lease_token, lease_version, moment):
        return _result(step, accepted=False, reason="lease_expired")
    if not service.guard(session, task, run):
        service.block(session, task, service.blocker("budget_exhausted"), run=run, step=step)
        _invalidate(step)
        return _result(step, accepted=False, reason="budget_exhausted")
    step.lease_expires_at = moment + timedelta(seconds=LEASE_SECONDS)
    return _result(step, accepted=True)


def _refs(session, task, refs):
    from app.secrets.detect import looks_like_secret
    if not isinstance(refs, (list, tuple)) or len(refs) > 16:
        raise ValueError("task_device_evidence_invalid")
    for ref in refs:
        if (not isinstance(ref, str) or len(ref) > 220 or looks_like_secret(ref)
                or not re.fullmatch(r"(?:venture_document|device_order|task_event):[A-Za-z0-9_.:-]+", ref)):
            raise ValueError("task_device_evidence_invalid")
        kind, _, identifier = ref.partition(":")
        row = session.get(TaskEvent if kind == "task_event" else VentureDocument, identifier)
        if (not row or (kind == "task_event" and row.task_id != task.id)
                or (kind != "task_event" and row.venture_id != task.venture_id)
                or (kind == "device_order" and row.kind != "device_work_order_v1")):
            raise PermissionError("task_device_evidence_scope_mismatch")
    return list(dict.fromkeys(refs))


def checkpoint_step(session, task_id, step_id, device_id, lease_token, lease_version, *,
                    evidence_refs=(), effect_outcome="none", now=None):
    if effect_outcome not in {"none", "calling", "completed", "uncertain"}:
        raise ValueError("task_device_effect_outcome_invalid")
    moment = naive_utc(now)
    task, run, step = _locked(session, task_id, step_id, device_id)
    if not _fenced(session, task, run, step, device_id, lease_token, lease_version, moment):
        return _result(step, accepted=False, reason="lease_expired")
    refs = _refs(session, task, evidence_refs)
    evidence = _evidence(step)
    if effect_outcome == "completed" or (evidence.get("effect_outcome") == "calling" and effect_outcome == "none"):
        order_id = json.loads(step.inputs_json).get("device_order_id")
        if not order_id or evidence.get("order_ref") != "device_order:" + order_id or _order_unknown(
                _order(session, task, step, device_id, order_id)):
            effect_outcome = "uncertain"
    evidence.update(refs=list(dict.fromkeys([*evidence.get("refs", []), *refs]))[-16:], effect_outcome=effect_outcome)
    step.evidence_json = json.dumps(evidence)
    service.event(session, task, "device_checkpoint", "Your Mac saved progress for this step.",
                  run_id=run.id, step_id=step.id, data={"refs": refs, "effect_outcome": effect_outcome})
    if effect_outcome == "uncertain":
        _reconciliation(session, task, run, step)
    return _result(step, accepted=True)


def sweep_stale_leases(session, *, now=None):
    """Trusted scheduler seam; expire leases across tenants, never dispatch work."""
    moment = naive_utc(now)
    from app.tasks.locks import lock_step
    rows = session.execute(select(TaskStep.id, TaskRun.task_id).join(TaskRun, TaskRun.id == TaskStep.run_id).where(TaskStep.state == "working",
        TaskStep.lease_token.is_not(None), TaskStep.lease_expires_at <= moment)
        .order_by(TaskStep.lease_expires_at).limit(100)).all()
    expired = []
    for step_id, task_id in rows:
        task, run, step = lock_step(session, step_id, skip_locked=True)
        if (not task or not run or not step or task.id != task_id or step.state != "working"
                or not step.lease_token or not step.lease_expires_at or naive_utc(step.lease_expires_at) > moment):
            continue
        _serialize(session, "task-device:" + task_id)
        if task.state not in service.TERMINAL and run.state not in service.TERMINAL:
            _stale(session, task, run, step)
        else:
            _invalidate(step)
        expired.append(step.id)
    return expired


def _order(session, task, step, device_id, order_id):
    from app.build import device_orders
    if json.loads(step.inputs_json).get("device_order_id") != order_id or not task.venture_id:
        raise PermissionError("task_device_order_unbound")
    _, body = device_orders._saved_order(session, task.venture_id, order_id, task.workspace_id,
                                        task.owner_person_id, device_id, lock=True)
    if (body.get("id"), body.get("venture_id")) != (order_id, task.venture_id):
        raise PermissionError("task_device_order_unbound")
    try:
        valid = body.get("review_digest") == device_orders._digest(body)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("task_device_order_review_invalid")
    return body


def _order_unknown(body):
    return (body.get("stop_reconciliation_required") or body.get("inference_reconciliation_required") or any(
        invocation.get("state") in {"calling", "uncertain"}
        or (invocation.get("effect") or {}).get("state") in {"admitted", "calling", "uncertain"}
        or invocation.get("task_usage_projection") in {"deferred", "uncertain"}
        for invocation in body.get("invocations", {}).values()))


def attach_order(session, task_id, step_id, device_id, order_id):
    """Project an existing approved order; native admission stays in S07."""
    task, run, step = _locked(session, task_id, step_id, device_id)
    body = _order(session, task, step, device_id, order_id)
    if body.get("state") not in {"approved", "claimed", "stopped"}:
        raise ValueError("task_device_order_not_approved")
    evidence = _evidence(step)
    if evidence.get("order_ref") not in {None, "device_order:" + order_id}:
        raise ValueError("task_device_order_changed")
    evidence.update(order_ref="device_order:" + order_id)
    if body.get("execution_id"):
        if evidence.get("execution_id") not in {None, body["execution_id"]}:
            raise ValueError("task_device_execution_changed")
        evidence["execution_id"] = body["execution_id"]
    step.evidence_json = json.dumps(evidence)
    if _order_unknown(body):
        evidence["effect_outcome"] = "uncertain"
        step.evidence_json = json.dumps(evidence)
        _reconciliation(session, task, run, step)
    return {"order_ref": evidence["order_ref"], "state": body["state"]}


def attach_stop_receipt(session, task_id, step_id, device_id, order_id):
    """Complete only from the existing settled and verified native stop receipt."""
    from app.build.device_stop import _verification_evidence, _verification_kind
    task, run, step = _locked(session, task_id, step_id, device_id)
    body = _order(session, task, step, device_id, order_id)
    evidence = _evidence(step)
    stop = body.get("native_stop") or {}
    if (evidence.get("order_ref") != "device_order:" + order_id
            or evidence.get("execution_id") not in {None, body.get("execution_id")}
            or not body.get("execution_id") or stop.get("execution_id") != body["execution_id"]):
        raise PermissionError("task_device_stop_unbound")
    hold = session.get(CreditReservation, body.get("reservation_id"))
    verified = (body.get("state") == "stopped" and body.get("closed_at") and stop.get("processes_stopped") is True
        and stop.get("task_verified") is True and _verification_kind(body.get("manifest") or {})
        and stop.get("verification_kind") == _verification_kind(body.get("manifest") or {})
        and _verification_evidence(body)
        and re.fullmatch(r"[a-f0-9]{64}", str(stop.get("evidence_digest", "")))
        and hold and (hold.status, hold.workspace_id, hold.request_id, hold.purpose)
            == ("settled", task.workspace_id, order_id, "device.work_order")
        and not _order_unknown(body))
    if not verified:
        _reconciliation(session, task, run, step)
        return _result(step, accepted=False, reason="reconciliation_required")
    evidence.update(execution_id=body["execution_id"], effect_outcome="completed", stop_evidence_digest=stop["evidence_digest"])
    step.evidence_json = json.dumps(evidence)
    if step.state != "completed":
        step.state, step.finished_at = "completed", naive_utc()
        _invalidate(step)
        service.event(session, task, "step_finished", "Verified: " + step.title, run_id=run.id, step_id=step.id,
                      data={"refs": [evidence["order_ref"]]})
        waiting = json.loads(task.blocker_json or "{}")
        active = {"working", "submitted"}
        can_continue = (task.state in active and run.state in active) or (
            task.state in active | {"input_required"} and run.state in active | {"input_required"}
            and (waiting.get("class") == "needs_device_online" or waiting.get("reconciliation_required") is True))
        if can_continue:
            run.state = task.state = "working"
            task.blocker_json = None
            service.complete_run(session, task, run)
    return _result(step, accepted=True)
