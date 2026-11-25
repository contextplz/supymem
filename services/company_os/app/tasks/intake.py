"""Durable, owner-scoped task planning intent before one metered call."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from sqlalchemy import select

from app.credits._txn import naive_utc
from app.db.session import serialize_writes
from app.db.task_models import Task, TaskEvent, TaskRun
from app.secrets.detect import looks_like_secret
from app.tasks import service
from app.tasks.contracts import Contract, estimated_deadline, next_fire, timezone_name, trigger_from_words
from app.tasks.locks import lock_task
from app.tasks.planner import validate_contract

KEY = "_planning"
FORMAT = "task_intake_v1"
BINDING_FIELDS = ("format", "task_id", "workspace_id", "venture_id", "owner_person_id", "source_key",
                  "request_digest", "request_id", "goal_hash", "accepted_zone", "accepted_at", "start", "billing_required")
BILLING_BLOCKER = {"class": "needs_input", "reason": "Your plan is saved, but its usage needs confirmation before work can start.",
                   "unblock_action": "Review the saved plan's earlier outcome; the planner call will not be repeated."}


class IntakeConflict(ValueError):
    """The HTTP boundary maps this to 409 before any further transport."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json(raw):
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise IntakeConflict("The saved task request needs review.")
    return value


def _owner(session, workspace_id, venture_id, owner_person_id):
    if not service._active_custody(session, workspace_id, venture_id, owner_person_id):
        raise PermissionError("task_scope_mismatch")


def _binding(task, journal):
    if (journal.get("format") != FORMAT or journal.get("task_id") != task.id
            or journal.get("workspace_id") != task.workspace_id or journal.get("venture_id") != task.venture_id
            or journal.get("owner_person_id") != task.owner_person_id or journal.get("source_key") != task.create_key
            or journal.get("request_digest") != task.create_digest or journal.get("goal_hash") != _hash(task.goal)
            or journal.get("request_id") != "task-plan:" + task.id + ":1"
            or journal.get("binding_hash") != _hash({key: journal.get(key) for key in BINDING_FIELDS})
            or journal.get("state") not in {"calling", "responded", "complete", "uncertain", "failed"}):
        raise IntakeConflict("The saved task request no longer matches this plan.")


def _rows(session, *, task_id, workspace_id, venture_id, owner_person_id, request_id=None, active_owner=True):
    task = lock_task(session, task_id)
    if not task or (task.workspace_id, task.venture_id, task.owner_person_id) != (workspace_id, venture_id, owner_person_id):
        raise PermissionError("task_scope_mismatch")
    if active_owner:
        _owner(session, workspace_id, venture_id, owner_person_id)
    scope = _json(task.scope_json)
    journal = scope.get(KEY)
    if not isinstance(journal, dict):
        raise IntakeConflict("The saved task has no planning checkpoint.")
    _binding(task, journal)
    if request_id is not None and request_id != journal["request_id"]:
        raise IntakeConflict("The planner response belongs to a different request.")
    return task, scope, journal


def _normalized(contract, *, goal, zone, accepted_at):
    selected = Contract.model_validate(contract.model_dump() if isinstance(contract, Contract) else contract)
    if selected.goal != goal:
        raise IntakeConflict("The planner response changed the requested goal.")
    if looks_like_secret(json.dumps(selected.model_dump(), sort_keys=True)):
        raise ValueError("Keep credentials out of task text. Use the Connections page instead.")
    kind, trigger = trigger_from_words(goal, zone, accepted_at)
    selected = selected.model_copy(update={"kind": kind, "trigger": trigger})
    return validate_contract(selected, goal=goal)


def begin(session, *, workspace_id, venture_id, owner_person_id, create_key, body_input, accepted_zone):
    """Caller commits this intent before awaiting the planner. False never redials."""
    if not isinstance(create_key, str) or not 1 <= len(create_key) <= 128:
        raise ValueError("A task request key is required.")
    body = body_input.model_dump(mode="json") if hasattr(body_input, "model_dump") else dict(body_input)
    if set(body) - {"goal", "timezone", "start", "contract"}:
        raise ValueError("This task request contains unsupported fields.")
    body = {"goal": body.get("goal"), "timezone": body.get("timezone"), "start": body.get("start", True), "contract": body.get("contract")}
    if (not isinstance(body["goal"], str) or not 1 <= len(body["goal"].strip()) <= 8000 or type(body["start"]) is not bool
            or body["timezone"] is not None and (not isinstance(body["timezone"], str) or len(body["timezone"]) > 80)):
        raise ValueError("Choose a goal and valid task settings.")
    digest = _hash(body)
    _owner(session, workspace_id, venture_id, owner_person_id)
    existing_id = session.scalar(select(Task.id).where(Task.create_key == create_key))
    if not existing_id:
        # Only creation needs the source-key lock. Existing rows use the task
        # lock, so replay cannot invert Task → response-advisory lock ordering.
        serialize_writes(session, key="task-intake:" + create_key)
        existing_id = session.scalar(select(Task.id).where(Task.create_key == create_key))
    existing = lock_task(session, existing_id) if existing_id else None
    if existing:
        if (existing.workspace_id, existing.venture_id, existing.owner_person_id, existing.create_digest) != (
                workspace_id, venture_id, owner_person_id, digest):
            raise IntakeConflict("This request key was already used for different task input.")
        _rows(session, task_id=existing.id, workspace_id=workspace_id, venture_id=venture_id, owner_person_id=owner_person_id)
        return existing, False
    zone, now = timezone_name(accepted_zone), naive_utc()
    trigger_from_words(body["goal"], zone, now)  # Unknown recurrence asks before transport.
    supplied = _normalized(body["contract"], goal=body["goal"], zone=zone, accepted_at=now) if body["contract"] is not None else None
    stub = Contract(title=body["goal"].strip()[:160], goal=body["goal"], steps=[{"title": "Prepare the requested work", "skill": "task.run"}])
    task = service.create(session, workspace_id=workspace_id, venture_id=venture_id,
        owner_person_id=owner_person_id, contract=stub, start=False)
    task.create_key, task.create_digest, task.state = create_key, digest, "working"
    journal = {"format": FORMAT, "state": "responded" if supplied else "calling", "task_id": task.id,
        "workspace_id": workspace_id, "venture_id": venture_id, "owner_person_id": owner_person_id,
        "source_key": create_key, "request_digest": digest, "request_id": "task-plan:" + task.id + ":1",
        "goal_hash": _hash(task.goal), "accepted_zone": zone, "accepted_at": now.isoformat(), "start": body["start"],
        "billing_required": supplied is None}
    journal["binding_hash"] = _hash({key: journal[key] for key in BINDING_FIELDS})
    if supplied:
        journal.update(contract=supplied.model_dump(), contract_hash=_hash(supplied.model_dump()))
    scope = _json(task.scope_json)
    scope[KEY] = journal
    task.scope_json = json.dumps(scope, sort_keys=True)
    pending = next((row for row in session.new if isinstance(row, TaskEvent) and row.task_id == task.id and row.kind == "plan"), None)
    data = {"request_hash": digest, "state": journal["state"]}
    if pending is not None:
        # Only an event not yet INSERTed can change. Persisted events are append-only.
        pending.kind, pending.message, pending.data_json = "planning", "Your task plan is being prepared.", json.dumps(data)
    else:
        service.event(session, task, "planning", "Your task plan is being prepared.", data=data)
    return task, supplied is None


def respond(session, *, task_id, workspace_id, venture_id, owner_person_id, request_id, contract, usage=None):
    """Caller commits this response before applying it or starting work."""
    task, scope, journal = _rows(session, task_id=task_id, workspace_id=workspace_id, venture_id=venture_id,
        owner_person_id=owner_person_id, request_id=request_id, active_owner=usage is None)
    serialize_writes(session, key="task-intake-response:" + task_id)
    selected = _normalized(contract, goal=task.goal, zone=journal["accepted_zone"], accepted_at=datetime.fromisoformat(journal["accepted_at"]))
    data, digest = selected.model_dump(), _hash(selected.model_dump())
    checkpoint = None
    if journal["billing_required"]:
        from app.tasks import billing
        if usage is not None:
            checkpoint = billing.capture(usage, workspace_id=workspace_id, venture_id=venture_id,
                purpose="task.plan", request_id=request_id)
        elif journal["state"] not in {"responded", "complete"}:
            raise IntakeConflict("The planner response needs its original usage receipt.")
    if journal["state"] in {"responded", "complete"}:
        if (journal.get("contract_hash") != digest or journal.get("contract") != data
                or checkpoint is not None and journal.get("billing") != checkpoint):
            raise IntakeConflict("The saved planner response changed.")
        return task
    if journal["state"] != "calling":
        raise IntakeConflict("The earlier planner outcome needs review before continuing.")
    journal.update(state="responded", contract=data, contract_hash=digest)
    if checkpoint is not None:
        journal["billing"] = checkpoint
    scope[KEY] = journal
    task.scope_json = json.dumps(scope, sort_keys=True)
    service.event(session, task, "planning_responded", "The task plan response was saved.", data={"contract_hash": digest})
    return task


def reconcile(*, task_id, workspace_id, venture_id, owner_person_id):
    """Recover saved billing outside the Task transaction; this never starts work."""
    from app.state import db
    from app.tasks import billing
    with db()() as session:
        _, _, journal = _rows(session, task_id=task_id, workspace_id=workspace_id, venture_id=venture_id,
            owner_person_id=owner_person_id, active_owner=False)
        if journal["state"] not in {"responded", "complete"} or not journal["billing_required"]:
            return None
        checkpoint = json.loads(json.dumps(journal.get("billing")))
        request_id = journal["request_id"]
    billing.reconcile(checkpoint, workspace_id=workspace_id, venture_id=venture_id,
        purpose="task.plan", request_id=request_id)
    return checkpoint["sha256"]


def recover(session, *, task_id, workspace_id, venture_id, owner_person_id, confirmed_billing=None):
    """Apply a verified saved response to the same Task, with no model call."""
    task, scope, journal = _rows(session, task_id=task_id, workspace_id=workspace_id, venture_id=venture_id,
        owner_person_id=owner_person_id, active_owner=False)
    serialize_writes(session, key="task-intake-response:" + task_id)
    if journal["state"] != "responded":
        return task
    selected = _normalized(journal["contract"], goal=task.goal, zone=journal["accepted_zone"], accepted_at=datetime.fromisoformat(journal["accepted_at"]))
    if _hash(selected.model_dump()) != journal.get("contract_hash"):
        raise IntakeConflict("The saved planner response changed.")
    if journal["billing_required"]:
        from app.credits.errors import CreditError
        from app.tasks import billing
        try:
            checkpoint = journal.get("billing")
            if (not isinstance(checkpoint, dict) or confirmed_billing != checkpoint.get("sha256")
                    or checkpoint != billing.capture(checkpoint.get("usage"), workspace_id=workspace_id,
                        venture_id=venture_id, purpose="task.plan", request_id=journal["request_id"])):
                raise ValueError("task_plan_billing_pending")
        except (CreditError, ValueError):
            _owner(session, workspace_id, venture_id, owner_person_id)
            journal["billing_pending"] = True
            scope[KEY] = journal
            task.scope_json = json.dumps(scope, sort_keys=True)
            service.block(session, task, BILLING_BLOCKER)
            return task
        if journal.pop("billing_pending", False) and task.state == "input_required" and _json(task.blocker_json) == BILLING_BLOCKER:
            task.state, task.blocker_json = "working", None
    _owner(session, workspace_id, venture_id, owner_person_id)  # Funded cleanup does not authorize new work.
    if session.scalar(select(TaskRun.id).where(TaskRun.task_id == task.id).limit(1)):
        raise IntakeConflict("This task already started before its plan was confirmed.")
    now = naive_utc()
    stopped = task.state in service.TERMINAL | service.WAITING
    task.title, task.kind = selected.title, selected.kind
    task.trigger_json, task.done_test_json = json.dumps(selected.trigger), json.dumps(selected.done_test)
    task.budget_json, task.tolerance = selected.budget.model_dump_json(), selected.tolerance
    task.deadline_at = estimated_deadline(task.goal, journal["accepted_zone"], datetime.fromisoformat(journal["accepted_at"]), selected.estimate_seconds)
    from app.tasks import goals
    if any(step.subgoal for step in selected.steps) and goals.enabled():
        task.deadline_at = goals.goal_deadline(task.goal, journal["accepted_zone"], datetime.fromisoformat(journal["accepted_at"]), task.deadline_at)
        task.deadline_is_estimate = False
    task.next_fire_at = next_fire(selected.trigger, now) if selected.kind == "scheduled" and not stopped else None
    journal["state"] = "complete"
    task.scope_json = json.dumps({"estimate_seconds": selected.estimate_seconds, "steps": [step.model_dump() for step in selected.steps], KEY: journal}, sort_keys=True)
    if not stopped:
        task.state = "submitted"
    service.event(session, task, "plan", "Your task plan is ready. Deadlines are estimates.", data={"contract_hash": journal["contract_hash"]})
    if journal["start"] and selected.kind == "one_time" and not stopped:
        service.start_run(session, task, "initial")
    return task


def fail(session, *, task_id, workspace_id, venture_id, owner_person_id, request_id, uncertain=True):
    """No provider text is persisted. Failed or ambiguous intake never redials."""
    task, scope, journal = _rows(session, task_id=task_id, workspace_id=workspace_id, venture_id=venture_id,
        owner_person_id=owner_person_id, request_id=request_id, active_owner=False)
    serialize_writes(session, key="task-intake-response:" + task_id)
    if journal["state"] != "calling":
        return task
    journal["state"] = "uncertain" if uncertain else "failed"
    scope[KEY] = journal
    task.scope_json = json.dumps(scope, sort_keys=True)
    if task.state not in service.TERMINAL | service.WAITING:
        reason = {"class": "needs_input", "reason": "The earlier task plan needs its outcome checked before we can continue.",
            "unblock_action": "Review the saved request; its planner call will not be repeated automatically."}
        service.block(session, task, reason)
    service.event(session, task, "planning_uncertain" if uncertain else "planning_failed", "The earlier task plan needs review.")
    return task
