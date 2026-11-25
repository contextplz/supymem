"""Project a saved native order onto the existing fenced task step.

Native callers resolve and lock an existing parent *before* locking the order.
For a first legacy projection, the new parent is invisible outside this same
order transaction. Callers must flush the canonical claim before ensuring it,
and roll back the claim if the subsequent task lease is refused.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import select

from app.credits._txn import naive_utc
from app.db.supymem_models import DesktopDevice
from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
from app.tasks import service
from app.tasks.contracts import Contract
from app.tasks.locks import lock_run, lock_step
from app.tenancy import current
from core.ids import new_id

_IDS = ("task_id", "run_id", "step_id")


def _profile_binding(saved):
    return {"order_id": saved["id"], **{key: saved[key] for key in
        ("review_digest", "execution_id", "device_id", "workspace_id", "venture_id", "actor_id", "claimed_at")}}


def _capture_profile(saved):
    """Freeze source-owned native limits once, with no transport or credentials."""
    from app.build import device_ai_step, device_orders

    manifest = saved["manifest"]
    desktop = manifest.get("desktop_verification") or {}
    browser = manifest.get("general_browser_verification") or {}
    calls = 400 if desktop else 128
    try:
        captured = device_ai_step.native_model_limits(manifest)
    except (AttributeError, ValueError):
        raise ValueError("task_device_model_limits_unavailable") from None
    from app.tasks.budgets import _model_limits
    captured = _model_limits(captured)
    if desktop:
        tools = desktop.get("max_steps")
        if type(tools) is not int or not 1 <= tools <= 48:
            raise ValueError("task_device_profile_invalid")
    elif browser:
        # This profile is part of the exact reviewed manifest, not a new flag.
        profile = device_orders.BrowserAIProfile.model_validate(browser.get("execution_profile"))
        calls = min(calls, profile.max_provider_calls)
        tools = browser.get("max_steps")
        if type(tools) is not int or not 1 <= tools <= 64:
            raise ValueError("task_device_profile_invalid")
        tools = min(tools, calls)
    else:
        # A reviewed CLI operation has no API-native inference allowance.
        coding = manifest.get("coding_loop_verification")
        if coding:
            tools = coding.get("max_iterations")
            if type(tools) is not int or not 1 <= tools <= 10:
                raise ValueError("task_device_profile_invalid")
        elif any(manifest.get(key) for key in ("coding_verification", "deploy_verification",
                "terminal_verification", "inspection_verification", "app_navigation_verification")):
            tools = 1
        else:
            raise ValueError("task_device_model_limits_unavailable")
        calls = 0
    if bool(captured["roles"]) != bool(calls):
        raise ValueError("task_device_model_limits_unavailable")
    content = {"format": "native_task_budget_v1", "binding": _profile_binding(saved),
        "limits": {"cost_micro": manifest["credit_ceiling_micro"], "wall_seconds": manifest["duration_seconds"],
            "provider_calls": calls, "tool_calls": tools, "input_tokens": captured["input_tokens"],
            "output_tokens": captured["max_output_tokens"]}, "model_limits": captured}
    return {**content, "sha256": hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def _legacy_contract(saved):
    manifest = saved["manifest"]
    title = manifest.get("title")
    goal = (manifest.get("desktop_verification") or {}).get("founder_goal") or manifest.get("instruction")
    if not isinstance(title, str) or not 1 <= len(title) <= 160:
        raise ValueError("Review the original Mac task title before continuing.")
    if not isinstance(goal, str) or not goal:
        raise ValueError("Review the original Mac task goal before continuing.")
    if len(goal) > 8000:
        raise ValueError("The reviewed goal is too long for a task. Shorten it in the Mac review screen.")
    return Contract(title=title, goal=goal, estimate_seconds=max(60, manifest["duration_seconds"]),
        steps=[{"title": title, "skill": "device.work_order", "inputs": {"device_order_id": saved["id"]}, "runs_on": "device"}])


def _saved(session, body, *, lock=False, receipt_only=False, preclaim=False):
    from app.authz.workspace import subject_person_id
    from app.build import device_orders

    if not isinstance(body, dict) or any(not isinstance(body.get(key), str) or not body[key]
            for key in ("id", "workspace_id", "venture_id", "actor_id", "device_id")):
        raise ValueError("task_device_order_invalid")
    if not receipt_only:
        scope = current()
        owner = subject_person_id(scope.actor)
        if ((body["workspace_id"], body["venture_id"], body["actor_id"])
                != (scope.workspace_id, scope.venture_id, owner)
                or not service._active_custody(session, scope.workspace_id, scope.venture_id, owner)):
            raise PermissionError("task_device_scope_mismatch")
        device = session.execute(select(DesktopDevice.person_id, DesktopDevice.revoked_at)
            .where(DesktopDevice.id == body["device_id"])).one_or_none()
        if not device or device.person_id != owner or device.revoked_at:
            raise PermissionError("task_device_scope_mismatch")
    _, saved = device_orders._saved_order(session, body["venture_id"], body["id"],
        body["workspace_id"], body["actor_id"], body["device_id"], lock=lock)
    if saved != body or saved.get("id") != body["id"] or saved.get("venture_id") != body["venture_id"]:
        raise ValueError("task_device_order_changed")
    states = {"pending"} if preclaim else {"approved", "claimed"}
    if not receipt_only and saved.get("state") not in states:
        raise ValueError("task_device_order_not_ready")
    if preclaim:
        used = ("claimed_at", "execution_id", "invocations", "iterations", "approved_at", "reservation_id",
            "native_stop", "stop_reconciliation_required", "inference_reconciliation_required")
        try:
            fresh = not any(key in saved for key in used) and naive_utc(datetime.fromisoformat(saved["expires_at"])) > naive_utc()
        except (KeyError, TypeError, ValueError):
            fresh = False
        if not fresh:
            raise ValueError("task_device_preclaim_not_fresh")
    if receipt_only and (not isinstance(saved.get("execution_id"), str) or not saved["execution_id"].startswith("dex_")):
        raise ValueError("task_device_order_not_claimed")
    try:
        digest = device_orders._digest(saved)
    except (KeyError, TypeError, ValueError):
        raise ValueError("task_device_order_invalid") from None
    if saved.get("review_digest") != digest:
        raise ValueError("task_device_order_changed")
    return saved


def _resolve(session, body, *, receipt_only=False):
    matches = []
    # Scalar discovery takes no child/order locks and does not trust public IDs.
    rows = session.execute(select(Task.id, Task.workspace_id, Task.venture_id, Task.owner_person_id,
        TaskRun.id.label("run_id"), TaskStep.id.label("step_id"), TaskStep.inputs_json,
        TaskStep.skill, TaskStep.runs_on).join(TaskRun, TaskRun.task_id == Task.id)
        .join(TaskStep, TaskStep.run_id == TaskRun.id)
        .where(TaskStep.inputs_json.contains(body["id"], autoescape=True))).mappings()
    for row in rows:
        inputs = json.loads(row["inputs_json"])
        if not isinstance(inputs, dict) or inputs.get("device_order_id") != body["id"]:
            continue
        if ((row["workspace_id"], row["venture_id"], row["owner_person_id"])
                != (body["workspace_id"], body["venture_id"], body["actor_id"])
                or row["skill"] != "device.work_order" or row["runs_on"] not in {"device", "either"}
                or inputs.get("review_digest") not in ({body["review_digest"]} if receipt_only else {None, body["review_digest"]})
                or inputs.get("execution_id") not in ({body.get("execution_id")} if receipt_only else {None, body.get("execution_id")})):
            raise PermissionError("task_device_binding_mismatch")
        matches.append(dict(zip(_IDS, (row["id"], row["run_id"], row["step_id"]))))
    if len(matches) > 1:
        raise ValueError("task_device_binding_ambiguous")
    result = matches[0] if matches else None
    if any(key in body for key in _IDS) and (result is None or any(body.get(key) != result[key] for key in _IDS)):
        raise ValueError("task_device_binding_mismatch")
    if result is None and session.scalar(select(Task.id).where(Task.create_key == "device_order:" + body["id"])):
        raise ValueError("task_device_binding_incomplete")
    return result


def resolve_order_step(session, body, *, preclaim=False, receipt_only=False):
    """Discover before locks, never admission. INTERNAL preclaim is fresh pending.

Only an independently authenticated original signed receipt may use cleanup
discovery after custody/revocation/stop. Financial receipt settlement must not
depend on discovery succeeding; defer an unqualified task projection instead.
"""
    with session.no_autoflush:
        if type(preclaim) is not bool or type(receipt_only) is not bool or preclaim and receipt_only:
            raise ValueError("task_device_discovery_mode_invalid")
        saved = _saved(session, body, receipt_only=receipt_only, preclaim=preclaim)
        return _resolve(session, saved, receipt_only=True) if receipt_only else _resolve(session, saved)


def _unsettled(session, task, run):
    if run.reserved_cost_micro or run.reserved_tokens:
        return True
    reserved, settled = set(), set()
    for row in session.scalars(select(TaskEvent).where(TaskEvent.run_id == run.id,
            TaskEvent.kind.in_(("call_reserved", "call_uncertain", "call_settled")))):
        value = json.loads(row.data_json or "{}")
        invocation = value.get("invocation") if isinstance(value, dict) else None
        if row.task_id != task.id or not isinstance(invocation, str) or not invocation:
            return True
        (settled if row.kind == "call_settled" else reserved).add(invocation)
    return not reserved <= settled


def _claimed(session, saved):
    from app.build import device_orders
    try:
        claimed_at = naive_utc(datetime.fromisoformat(saved["claimed_at"]))
        expires_at = naive_utc(datetime.fromisoformat(saved["expires_at"]))
    except (KeyError, TypeError, ValueError):
        raise ValueError("task_device_order_not_claimed") from None
    execution = saved.get("execution_id")
    if (saved["state"] != "claimed" or not isinstance(execution, str) or not execution.startswith("dex_")
            or len(execution) > 100 or claimed_at > naive_utc() or expires_at < claimed_at):
        raise ValueError("task_device_order_not_claimed")
    if device_orders._review_task_snapshot(session, saved["workspace_id"], saved["venture_id"], saved["manifest"],
            actor=saved["actor_id"], device=saved["device_id"]) != saved["task_snapshot"]:
        raise ValueError("task_device_order_changed")
    return claimed_at, execution


def seal_first_order_step(session, body):
    """Trusted fresh producer only; caller already owns parent then order locks."""
    from app.tasks.budgets import NATIVE_STEP_SEAL, _typed_native_roles
    from app.tasks.locks import _held, TaskLockRequired

    with session.no_autoflush:
        binding = resolve_order_step(session, body)
        if binding is not None and any((model, binding[key]) not in _held(session)
                for model, key in ((Task, "task_id"), (TaskRun, "run_id"), (TaskStep, "step_id"))):
            raise TaskLockRequired("task_device_parent_lock_required")
        if binding is not None:
            task, run, step = lock_step(session, binding["step_id"])
        saved = _saved(session, body, lock=True)
        _claimed(session, saved)
        if _resolve(session, saved) != binding:
            raise TaskLockRequired("task_device_parent_lock_required")
        if binding is None:
            return None
        if (isinstance(task.create_key, str) and task.create_key.startswith("device_order:")
                or "_native_legacy" in json.loads(task.scope_json or "{}")):
            raise ValueError("task_device_legacy_reseal_refused")
        if (any(key in saved for key in ("invocations", "iterations", "native_stop", "stop_reconciliation_required", "inference_reconciliation_required"))
                or step.state not in {"submitted", "approval_required"} or step.job_id or step.started_at or step.finished_at
                or step.lease_owner or step.lease_token or step.lease_expires_at
                or type(step.lease_version) is not int or step.lease_version != 0
                or type(step.attempt) is not int or step.attempt != 0 or type(step.cost_micro) is not int or step.cost_micro != 0
                or any(row.kind != "blocker" for row in session.scalars(select(TaskEvent).where(TaskEvent.step_id == step.id)))
                or any(isinstance(row, TaskEvent) and row.step_id == step.id and row.kind != "blocker" for row in session.new)):
            raise ValueError("task_device_step_already_used")
        inputs, evidence = json.loads(step.inputs_json), json.loads(step.evidence_json)
        if isinstance(evidence, dict) and NATIVE_STEP_SEAL in evidence:
            _typed_native_roles(task, run, step)
            if set(evidence) != {NATIVE_STEP_SEAL} or evidence[NATIVE_STEP_SEAL]["binding"] != {**binding, **_profile_binding(saved)}:
                raise ValueError("task_device_binding_mismatch")
            return binding
        if (not isinstance(inputs, dict) or set(inputs) - {"device_order_id", "review_digest"}
                or inputs.get("review_digest") not in {None, saved["review_digest"]} or evidence not in ({}, [])):
            raise ValueError("task_device_binding_unsealed")
        captured = _capture_profile(saved)["model_limits"]
        content = {"format": "native_task_step_v1", "binding": {**binding, **_profile_binding(saved)}, "model_limits": captured}
        seal = {**content, "sha256": hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        inputs.update(review_digest=saved["review_digest"], execution_id=saved["execution_id"])
        step.inputs_json, step.evidence_json = json.dumps(inputs), json.dumps({NATIVE_STEP_SEAL: seal})
        return binding


def ensure_order_step(session, body):
    """Bind one saved claim; no job, lease, provider or native effect is admitted."""
    from app.tasks.device import _evidence, _order_unknown

    with session.no_autoflush:
        binding = resolve_order_step(session, body)
        if binding:
            task, run, step = lock_step(session, binding["step_id"])
        saved = _saved(session, body, lock=True)
        current_binding = _resolve(session, saved)
        if current_binding != binding:
            # Another transaction projected this order while its lock was held.
            # Retry discovery first; never acquire its parent after the order.
            raise ValueError("task_device_parent_lock_required")
        claimed_at, execution = _claimed(session, saved)
        if binding:
            inputs = json.loads(step.inputs_json or "{}")
            if (not isinstance(inputs, dict) or inputs.get("review_digest") != saved["review_digest"]
                    or inputs.get("execution_id") != execution):
                raise ValueError("task_device_binding_unsealed")
        if _order_unknown(saved) or binding and (_unsettled(session, task, run)
                or _evidence(step).get("effect_outcome") in {"calling", "uncertain"}):
            raise ValueError("task_device_reconciliation_required")
        if binding and task.create_key == "device_order:" + saved["id"]:
            from app.tasks.budgets import effective_limits
            effective_limits(task, run)
            if json.loads(task.scope_json)["_native_legacy"]["binding"] != _profile_binding(saved):
                raise ValueError("task_device_binding_mismatch")
        if not binding:
            if saved.get("invocations") or saved.get("iterations"):
                raise ValueError("task_device_history_requires_reconciliation")
            contract = _legacy_contract(saved)
            profile = _capture_profile(saved)
            task = service.create(session, workspace_id=saved["workspace_id"], venture_id=saved["venture_id"],
                owner_person_id=saved["actor_id"], contract=contract, start=False)
            # The canonical order lock serializes this first projection. Its
            # unique key supplies the final fence without a second SQLite BEGIN.
            task.create_key = "device_order:" + saved["id"]
            scope = json.loads(task.scope_json)
            scope["_native_legacy"] = profile
            task.scope_json = json.dumps(scope)
            due = claimed_at + timedelta(seconds=profile["limits"]["wall_seconds"])
            run = TaskRun(id=new_id("trn"), task_id=task.id, run_key=execution, state="submitted",
                started_at=claimed_at, deadline_at=due)
            session.add(run)
            session.flush()
            lock_run(session, run.id)
            step = TaskStep(id=new_id("tst"), run_id=run.id, position=0, title=contract.title,
                skill="device.work_order", runs_on="device", state="submitted", deadline_at=due)
            session.add(step)
            session.flush()
            lock_step(session, step.id)
            task.state, task.last_run_at, task.deadline_at = "working", claimed_at, due
            binding = dict(zip(_IDS, (task.id, run.id, step.id)))
        inputs = json.loads(step.inputs_json or "{}")
        evidence = _evidence(step)
        if (evidence.get("order_ref") not in {None, "device_order:" + saved["id"]}
                or evidence.get("execution_id") not in {None, execution}
                or step.lease_owner not in {None, saved["device_id"]}):
            raise ValueError("task_device_binding_mismatch")
        inputs.update(device_order_id=saved["id"], review_digest=saved["review_digest"], execution_id=execution)
        evidence.update(order_ref="device_order:" + saved["id"], execution_id=execution)
        step.inputs_json, step.evidence_json = json.dumps(inputs), json.dumps(evidence)
        reason = json.loads(task.blocker_json or "{}")
        if (task.state == run.state == step.state == "approval_required" and reason.get("class") == "needs_approval"
                and not reason.get("reconciliation_required") and not session.scalar(select(TaskStep.id)
                    .join(TaskRun, TaskRun.id == TaskStep.run_id).where(TaskRun.task_id == task.id,
                        TaskStep.state == "approval_required", TaskStep.id != step.id))):
            task.state, run.state, step.state, task.blocker_json = "working", "working", "submitted", None
        return binding
