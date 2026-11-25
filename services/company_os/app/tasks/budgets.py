"""Founder-approved fixed continuation slices, scoped to one existing run."""
from datetime import datetime, timedelta
import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import select

from app.credits._txn import naive_utc
from app.db.models import Job
from app.db.task_models import TaskStep
from app.tasks.contracts import Budget

MAX_CONTINUATIONS = 2
SLICE = {"tokens": 25_000, "cost_micro": 100_000, "attempts": 3, "tool_calls": 8, "wall_seconds": 900}
MAXIMA = {name: Budget.model_json_schema()["properties"][name]["maximum"] for name in SLICE}


def _allowance(run):
    try:
        value = json.loads(getattr(run, "allowance_json", None) or "{}")
        if not isinstance(value, dict):
            raise ValueError()
        if not value:
            return {"count": 0, "approvals": []}
        count, approvals = value["count"], value["approvals"]
        if (type(count) is not int or not 0 <= count <= MAX_CONTINUATIONS or not isinstance(approvals, list)
                or len(approvals) != count or [entry["epoch"] for entry in approvals] != list(range(1, count + 1))
                or any(type(entry["epoch"]) is not int or not isinstance(entry["key"], str)
                    or len(entry["key"]) != 64 or any(char not in "0123456789abcdef" for char in entry["key"]) for entry in approvals)
                or len({entry["key"] for entry in approvals}) != count):
            raise ValueError()
        for entry in approvals:
            datetime.fromisoformat(entry["at"])
        if count and naive_utc(datetime.fromisoformat(value["wall_until"])) != (
                naive_utc(datetime.fromisoformat(approvals[-1]["at"])) + timedelta(seconds=SLICE["wall_seconds"])):
            raise ValueError()
        return value
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("The saved continuation allowance needs review before this task can continue.") from None


def effective_limits(task, run):
    base = Budget.model_validate_json(task.budget_json)
    allowance = _allowance(run)
    legacy = _legacy_profile(task, run)
    if legacy is not None:
        if allowance["count"]:
            raise ValueError("The reviewed Mac task limits cannot be extended.")
        return SimpleNamespace(**{**base.model_dump(), **legacy})
    values = base.model_dump()
    for name, amount in SLICE.items():
        values[name] = min(MAXIMA[name], values[name] + allowance["count"] * amount)
    original = naive_utc(run.started_at) + timedelta(seconds=base.wall_seconds)
    extension = naive_utc(datetime.fromisoformat(allowance["wall_until"])) if allowance["count"] else original
    return SimpleNamespace(**values, wall_until=max(original, extension))


def is_legacy_profile(task, run):
    """Pure logical-execution check; malformed/extended profiles fail closed."""
    if _legacy_profile(task, run) is None:
        return False
    effective_limits(task, run)
    return True


NATIVE_STEP_SEAL = "_native_model_binding"


def _typed_native_roles(task, run, step):
    """Validate the server's original typed step seal without live resolution."""
    try:
        inputs, evidence = json.loads(step.inputs_json), json.loads(step.evidence_json)
        seal = evidence[NATIVE_STEP_SEAL]
        binding = seal["binding"]
        content = {key: value for key, value in seal.items() if key != "sha256"}
        if (set(seal) != {"format", "binding", "model_limits", "sha256"} or seal["format"] != "native_task_step_v1"
                or seal["sha256"] != hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                or set(binding) != {"task_id", "run_id", "step_id", "order_id", "review_digest", "execution_id",
                    "device_id", "workspace_id", "venture_id", "actor_id", "claimed_at"}
                or any(not isinstance(value, str) or not value for value in binding.values())
                or (binding["task_id"], binding["run_id"], binding["step_id"], binding["workspace_id"], binding["venture_id"], binding["actor_id"])
                    != (task.id, run.id, step.id, task.workspace_id, task.venture_id, task.owner_person_id)
                or run.task_id != task.id or step.run_id != run.id or step.skill != "device.work_order"
                or step.runs_on not in {"device", "either"} or not isinstance(inputs, dict)
                or any(inputs.get(key) != binding[name] for key, name in (("device_order_id", "order_id"),
                    ("review_digest", "review_digest"), ("execution_id", "execution_id")))
                or len(binding["review_digest"]) != 64 or any(char not in "0123456789abcdef" for char in binding["review_digest"])
                or not binding["execution_id"].startswith("dex_") or step.lease_owner not in {None, binding["device_id"]}):
            raise ValueError()
        datetime.fromisoformat(binding["claimed_at"])
        return _model_limits(seal["model_limits"])["roles"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("The Mac step needs its original model binding checked.") from None


def require_frozen_native_role(task, run, role, *, step=None):
    """Pure pre-transport match; typed tasks retain their existing strict route."""
    if is_legacy_profile(task, run):
        roles = json.loads(task.scope_json)["_native_legacy"]["model_limits"]["roles"]
    elif step is not None:
        roles = _typed_native_roles(task, run, step)
    else:
        return False
    fields = {"purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host"}
    if (not isinstance(role, dict) or set(role) != fields or type(role["max_output_tokens"]) is not int
            or any(not isinstance(role[key], str) for key in fields - {"max_output_tokens"})
            or sum(role == frozen for frozen in roles) != 1):
        raise ValueError("The model route changed. Review the Mac task before continuing.")
    return True


def _legacy_profile(task, run):
    """Server-only reviewed native caps; the ordinary Budget model stays bounded."""
    scope = json.loads(task.scope_json or "{}")
    profile = scope.get("_native_legacy") if isinstance(scope, dict) else None
    legacy_key = isinstance(task.create_key, str) and task.create_key.startswith("device_order:")
    if profile is None:
        if legacy_key:
            raise ValueError("The reviewed Mac task budget needs its original binding checked.")
        return None
    try:
        binding, limits = profile["binding"], profile["limits"]
        captured = _model_limits(profile["model_limits"])
        content = {key: value for key, value in profile.items() if key != "sha256"}
        if (set(profile) != {"format", "binding", "limits", "model_limits", "sha256"}
                or profile["format"] != "native_task_budget_v1"
                or profile["sha256"] != hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                or set(binding) != {"order_id", "review_digest", "execution_id", "device_id", "workspace_id", "venture_id", "actor_id", "claimed_at"}
                or any(not isinstance(value, str) or not value for value in binding.values())
                or len(binding["review_digest"]) != 64 or any(char not in "0123456789abcdef" for char in binding["review_digest"])
                or task.create_key != "device_order:" + binding["order_id"] or task.kind != "one_time"
                or (task.workspace_id, task.venture_id, task.owner_person_id, run.task_id, run.run_key)
                    != (binding["workspace_id"], binding["venture_id"], binding["actor_id"], task.id, binding["execution_id"])
                or type(run.attempts) is not int or run.attempts not in {0, 1}
                or run.control_job_id is not None or naive_utc(run.started_at) != naive_utc(datetime.fromisoformat(binding["claimed_at"]))
                or set(limits) != {"cost_micro", "wall_seconds", "provider_calls", "tool_calls", "input_tokens", "output_tokens"}
                or any(type(value) is not int for value in limits.values())
                or not 0 <= limits["cost_micro"] <= 1_000_000 or not 1 <= limits["wall_seconds"] <= 5400
                or not 0 <= limits["provider_calls"] <= 400 or not 1 <= limits["tool_calls"] <= 64
                or limits["input_tokens"] != captured["input_tokens"]
                or limits["output_tokens"] != captured["max_output_tokens"]
                or bool(captured["roles"]) != bool(limits["provider_calls"])):
            raise ValueError()
        return {"cost_micro": limits["cost_micro"], "wall_seconds": limits["wall_seconds"], "tool_calls": limits["tool_calls"], "attempts": 1,
            "tokens": limits["provider_calls"] * (limits["input_tokens"] + limits["output_tokens"]),
            "wall_until": naive_utc(run.started_at) + timedelta(seconds=limits["wall_seconds"])}
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("The reviewed Mac task budget needs its original binding checked.") from None


def _model_limits(value):
    """Pure validation of S06's credential-free captured route metadata."""
    try:
        if (not isinstance(value, dict) or set(value) != {"input_tokens", "max_output_tokens", "roles", "route_digest"}
                or type(value["input_tokens"]) is not int or type(value["max_output_tokens"]) is not int
                or not isinstance(value["roles"], list) or len(value["roles"]) > 8):
            raise ValueError()
        roles = value["roles"]
        fields = {"purpose", "provider", "model", "catalog_model", "api_mode", "max_output_tokens", "endpoint_host"}
        if (value["input_tokens"] != (200_000 if roles else 0)
                or any(not isinstance(role, dict) or set(role) != fields
                    or any(not isinstance(role[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", role[key])
                        for key in fields - {"max_output_tokens", "endpoint_host"})
                    or not isinstance(role["endpoint_host"], str)
                    or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", role["endpoint_host"])
                    or type(role["max_output_tokens"]) is not int or not 1 <= role["max_output_tokens"] <= 200_000
                    for role in roles)
                or len({role["purpose"] for role in roles}) != len(roles)
                or value["max_output_tokens"] != max((role["max_output_tokens"] for role in roles), default=0)):
            raise ValueError()
        content = {key: item for key, item in value.items() if key != "route_digest"}
        if value["route_digest"] != hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
            raise ValueError()
        from app.secrets.detect import looks_like_secret
        if looks_like_secret(json.dumps(content)):
            raise ValueError()
        return json.loads(json.dumps(value))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("The captured native model limits need review.") from None


def wall_deadline(task, run):
    return effective_limits(task, run).wall_until


def _key(run, key):
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise ValueError("A fresh continuation action key is required.")
    return hashlib.sha256((run.id + ":" + key).encode()).hexdigest()


def epoch_for_key(run, key):
    target = _key(run, key)
    return next((entry["epoch"] for entry in _allowance(run)["approvals"] if entry["key"] == target), None)


def _journal(raw):
    value = json.loads(raw or "{}")
    if not isinstance(value, (dict, list)):
        raise ValueError()
    return value


def no_call_pause(error):
    """Only the task gate's pre-reservation refusal proves no transport occurred."""
    from app.core.jobs import FatalError
    from app.tasks.usage import before_call
    if not isinstance(error, FatalError) or str(error) not in {"task_paused", "task_waiting"}:
        return False
    trace = error.__traceback__
    while trace:
        if trace.tb_frame.f_code is before_call.__code__:
            return True
        trace = trace.tb_next
    return False


def eligibility(session, task, run):
    """No providers or mutations; None means the bounded Continue action is safe."""
    with session.no_autoflush:
        return _eligibility(session, task, run)


def _eligibility(session, task, run):
    try:
        if _legacy_profile(task, run) is not None:
            return "Review this task through its original Mac workflow. Its approved limits cannot be extended here."
        allowance = _allowance(run)
        if (run.task_id != task.id or task.state != "input_required" or run.state != "input_required"
                or _journal(task.blocker_json).get("class") != "budget_exhausted"):
            return "This task is not waiting for a budget continuation. Review its saved progress."
        if allowance["count"] >= MAX_CONTINUATIONS:
            return "This run has used its continuation allowance. Review the partial result and create a fresh plan."
        if run.reserved_cost_micro or run.reserved_tokens:
            return "An earlier call still needs its usage confirmed. Review the earlier outcome before continuing."
        from app.db.task_models import TaskEvent
        reserved, settled = set(), set()
        for entry in session.scalars(select(TaskEvent).where(TaskEvent.run_id == run.id,
                TaskEvent.kind.in_(("call_reserved", "call_uncertain", "call_settled")))):
            invocation = _journal(entry.data_json).get("invocation")
            if entry.task_id != task.id or not isinstance(invocation, str) or not invocation:
                return "The earlier call needs its usage confirmed before continuing."
            (settled if entry.kind == "call_settled" else reserved).add(invocation)
        if not reserved <= settled:
            return "The earlier call needs its usage confirmed before continuing."
        control = session.get(Job, run.control_job_id) if run.control_job_id else None
        if control and control.status in {"running", "leased"}:
            return "The previous attempt is still finishing. Refresh saved progress before continuing."
        steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()
        all_completed = bool(steps) and all(step.state == "completed" for step in steps)
        task_check = _journal(task.done_test_json)
        complete_only = all_completed and task_check.get("type") != "rubric"
        base = Budget.model_validate_json(task.budget_json)
        next_caps = {name: min(MAXIMA[name], getattr(base, name) + (allowance["count"] + 1) * SLICE[name]) for name in SLICE}
        spent = {"tokens": run.tokens_used, "cost_micro": run.cost_micro, "attempts": run.attempts, "tool_calls": run.tool_calls}
        if any(value > next_caps[name] if complete_only else value >= next_caps[name] for name, value in spent.items()):
            return "This run reached the maximum supported allowance. Review its partial result and create a fresh plan."
        for step in steps:
            journal = _journal(step.evidence_json)
            if isinstance(journal, dict) and (journal.get("state") in {"calling", "uncertain"}
                    or journal.get("effect_outcome") in {"calling", "uncertain"}):
                return "An earlier action needs its result checked. Review it before continuing; it will not repeat automatically."
        evaluation = _journal(run.evaluation_json)
        if not isinstance(evaluation, dict) or evaluation.get("state") in {"calling", "uncertain"}:
            return "The earlier review needs its result checked before this task can continue."
        if all_completed:
            if complete_only:
                if any(step.runs_on != "server" for step in steps):
                    return "Review this result through its saved workflow before continuing."
                from app.tasks.checks import passes
                from app.tasks.digest import _receipt
                evidence = [item for step in steps for item in _receipt(session, task, step)["evidence"]]
                return None if passes(task_check, evidence) else "The saved result did not meet its completion check. Review the partial result."
            if not evaluation or evaluation.get("state") in {"not_called_budget", "budget_ready"} or (
                    evaluation.get("state") in {"responded", "complete"} and evaluation.get("valid") is True):
                return None
            return "The earlier review did not produce a verified answer. Review its outcome before creating a fresh request."
        pending = next((step for step in steps if step.state != "completed"), None)
        if not pending or pending.job_id or pending.skill != "tasks.reply_draft":
            return "Resume this workflow through its saved progress screen. Its completed calls will not be repeated here."
        journal = _journal(pending.evidence_json)
        if not isinstance(journal, dict) or journal and journal.get("state") not in {"not_called_budget", "budget_ready", "responded"}:
            return "The earlier draft outcome needs review before creating a fresh draft request."
        from app.tasks.drafts import FORMAT, ReplyDraft, _digest, _source
        _, _, _, binding, _ = _source(session, task_id=task.id, run_id=run.id, step_id=pending.id,
                workspace_id=task.workspace_id, venture_id=task.venture_id)
        if journal and (journal.get("format") != FORMAT or journal.get("binding") != binding):
            return "The saved draft request changed. Review it before creating a fresh request."
        if journal.get("state") == "responded":
            response = ReplyDraft.model_validate(journal["response"])
            if response.source_ref != binding["source_ref"] or journal.get("response_digest") != _digest(journal["response"]):
                return "The saved draft answer needs review before this task can continue."
        return None
    except (AttributeError, KeyError, TypeError, ValueError, PermissionError):
        return "The saved task or continuation proof needs review before this task can continue."


def grant(session, task, run, key, now=None):
    """Caller locks task/run and owns states, CAS and enqueue; this grants once."""
    from app.authz.workspace import subject_person_id
    from app.tasks import service
    from app.tenancy import current
    scope = current()
    if (run.task_id != task.id or (task.workspace_id, task.venture_id or "", task.owner_person_id)
            != (scope.workspace_id, scope.venture_id, subject_person_id(scope.actor))):
        raise PermissionError("The founder manages this task's continuation.")
    previous = epoch_for_key(run, key)
    if previous is not None:
        return previous
    refused = eligibility(session, task, run)
    if refused is not None:
        raise ValueError(refused)
    allowance, moment = _allowance(run), naive_utc(now)
    epoch = allowance["count"] + 1
    steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()
    target = ("verify" if _journal(task.done_test_json).get("type") == "rubric" else "complete") if all(
        step.state == "completed" for step in steps) else "execute"
    if target == "verify":
        journal = _journal(run.evaluation_json)
        if not journal or journal.get("state") in {"not_called_budget", "budget_ready"}:
            history = journal.get("previous_attempts", [])
            if journal and journal.get("state") != "budget_ready":
                history = [*history, {name: value for name, value in journal.items() if name != "previous_attempts"}][-2:]
            run.evaluation_json = json.dumps({"state": "budget_ready", "continuation_epoch": epoch, "previous_attempts": history})
    elif target == "execute":
        pending = next(step for step in steps if step.state != "completed")
        journal = _journal(pending.evidence_json)
        if journal.get("state") != "responded":
            from app.tasks.drafts import FORMAT, _source
            inputs = _journal(pending.inputs_json)
            inputs["_budget_attempt"] = epoch
            pending.inputs_json = json.dumps(inputs)
            _, _, _, binding, _ = _source(session, task_id=task.id, run_id=run.id, step_id=pending.id,
                    workspace_id=task.workspace_id, venture_id=task.venture_id)
            history = journal.get("previous_attempts", [])
            if journal and journal.get("state") != "budget_ready":
                history = [*history, {name: value for name, value in journal.items() if name != "previous_attempts"}][-2:]
            pending.evidence_json = json.dumps({"format": FORMAT, "state": "budget_ready", "binding": binding,
                                              "previous_attempts": history})
    run.allowance_json = json.dumps({"count": epoch, "wall_until": (moment + timedelta(seconds=SLICE["wall_seconds"])).isoformat(),
        "approvals": [*allowance["approvals"], {"key": _key(run, key), "epoch": epoch, "at": moment.isoformat(), "target": target}]})
    service.event(session, task, "budget_granted", "You approved a bounded continuation from saved progress.", run_id=run.id)
    return epoch


def resume_eligibility(session, task, run):
    """Pure proof for Resume; no new allowance, journal reset or workflow replay."""
    from app.authz.workspace import subject_person_id
    from app.core.jobs import _call_in_flight
    from app.db.models import Venture, Workspace
    from app.db.task_models import TaskEvent, TaskRun
    from app.tasks import service
    from app.tenancy import current_or_none
    from sqlalchemy.exc import SQLAlchemyError

    review = "Review the earlier outcome before resuming. An uncertain action will not be repeated."
    workflow = "Resume this workflow through its saved progress screen. Its completed calls will not be repeated here."
    try:
        scope = current_or_none()
        if scope is None:
            return "The founder manages this task's recovery."
        with session.no_autoflush:
            workspace = session.get(Workspace, task.workspace_id)
            venture = session.get(Venture, task.venture_id) if task.venture_id else None
            if ((task.workspace_id, task.venture_id or "", task.owner_person_id)
                    != (scope.workspace_id, scope.venture_id, subject_person_id(scope.actor))
                    or not workspace or workspace.owner_id != task.owner_person_id or workspace.archived_at
                    or task.venture_id and (not venture or venture.workspace_id != task.workspace_id or venture.archived_at)):
                return "The founder manages this task's recovery."
            if task.state not in {"paused", "auth_required"} or run and (run.task_id != task.id or run.state in service.TERMINAL):
                return "This task cannot be resumed from this state. Review its saved progress."
            if service.planning_pending(task):
                return "The earlier task plan needs its outcome checked before any work can start."
            reason = _journal(task.blocker_json)
            if not isinstance(reason, dict) or reason.get("class") == "budget_exhausted":
                return "Review the partial result and choose Continue to approve another bounded attempt."
            runs = [run] if run else session.scalars(select(TaskRun).where(TaskRun.task_id == task.id)).all()
            if run is None and (task.state != "paused" or any(row.state not in service.TERMINAL | {"queued"} for row in runs)
                    or task.kind == "one_time" and runs):
                return "Review the existing run before resuming this task."
            for owned in runs:
                _allowance(owned)  # A damaged allowance cannot authorize work.
                if owned.reserved_cost_micro or owned.reserved_tokens:
                    return review
                events = session.scalars(select(TaskEvent).where(TaskEvent.run_id == owned.id,
                    TaskEvent.kind.in_(("call_reserved", "call_uncertain", "call_settled")))).all()
                reserved, settled = set(), set()
                for item in events:
                    invocation = _journal(item.data_json).get("invocation")
                    if item.task_id != task.id or not isinstance(invocation, str) or not invocation:
                        return review
                    (settled if item.kind == "call_settled" else reserved).add(invocation)
                if not reserved <= settled:
                    return review
                steps = session.scalars(select(TaskStep).where(TaskStep.run_id == owned.id).order_by(TaskStep.position)).all()
                evaluation = _journal(owned.evaluation_json)
                if not isinstance(evaluation, dict) or evaluation.get("state") in {"calling", "uncertain"}:
                    return review
                for step in steps:
                    evidence = _journal(step.evidence_json)
                    if isinstance(evidence, dict) and (evidence.get("state") in {"calling", "uncertain"}
                            or evidence.get("effect_outcome") in {"calling", "uncertain"}
                            or evidence.get("_research_progress", {}).get("state") in {"calling", "uncertain"}):
                        return review
                    if step.state == "completed" and run is not None:
                        if step.runs_on != "server":
                            return workflow
                        from app.tasks.digest import _receipt
                        _receipt(session, task, step)  # Recheck saved server sources without attaching receipts.
                job_ids = {step.job_id for step in steps if step.job_id}
                if owned.control_job_id:
                    job_ids.add(owned.control_job_id)
                for identifier in job_ids:
                    job = session.get(Job, identifier)
                    if (not job or (job.workspace_id, job.venture_id) != (task.workspace_id, task.venture_id)
                            or _call_in_flight(session, identifier)):
                        return review
                    if identifier == owned.control_job_id and (job.kind not in {"tasks.execute", "tasks.verify"}
                            or _journal(job.payload_json).get("task_run_id") != owned.id):
                        return review
                    if job.status in {"running", "leased"}:
                        return "The earlier attempt is still finishing. Refresh saved progress before resuming."
                if run is None:
                    continue  # Resume only future/queued standing work, never replay a terminal run.
                pending = next((step for step in steps if step.state != "completed"), None)
                if pending is None:
                    if not steps:
                        return workflow
                    if _journal(task.done_test_json).get("type") != "rubric":
                        return None if task.state == "paused" else workflow
                    if any(step.runs_on != "server" for step in steps):
                        return workflow  # Native receipt attachment is not a pure reader.
                    from app.tasks.verification import artifact_snapshot
                    snapshot = artifact_snapshot(session, task, owned)
                    state = evaluation.get("state")
                    if state in {"responded", "complete", "not_called_account", "not_called_pause"} and (
                            evaluation.get("artifact_digest"), evaluation.get("contract_digest")) != (
                            snapshot["artifact_digest"], snapshot["contract_digest"]):
                        return "The saved result or review criteria changed. Review them before resuming."
                    cached = state in {"responded", "complete"} and evaluation.get("valid") is True
                    if task.state == "auth_required":
                        return None if state == "not_called_account" else workflow
                    paused_no_call = state == "not_called_pause" and evaluation.get("known_no_call") is True
                    return None if cached or paused_no_call or not evaluation or state in {"not_called_account", "budget_ready"} else review
                if pending.job_id:
                    job = session.get(Job, pending.job_id)
                    return None if task.state == "paused" and job.status in {
                        "scheduled", "pending", "retry_wait", "succeeded", "done"} else workflow
                if pending.skill != "tasks.reply_draft":
                    return None if task.state == "paused" and pending.state == "submitted" and not _journal(pending.evidence_json) else workflow
                from app.tasks.drafts import FORMAT, ReplyDraft, _digest, _source
                _, _, _, binding, _ = _source(session, task_id=task.id, run_id=owned.id, step_id=pending.id,
                    workspace_id=task.workspace_id, venture_id=task.venture_id)
                journal = _journal(pending.evidence_json)
                if not isinstance(journal, dict) or journal and (journal.get("format") != FORMAT or journal.get("binding") != binding):
                    return "The saved draft request changed. Review it before resuming."
                state = journal.get("state")
                if task.state == "auth_required" and state != "not_called_account":
                    return workflow
                if state == "responded":
                    response = ReplyDraft.model_validate(journal["response"])
                    if response.source_ref != binding["source_ref"] or journal.get("response_digest") != _digest(journal["response"]):
                        return review
                paused_no_call = state == "not_called_pause" and journal.get("known_no_call") is True
                return None if paused_no_call or not journal or state in {"responded", "not_called_account", "budget_ready"} else review
            return None
    except (AttributeError, KeyError, TypeError, ValueError, PermissionError, SQLAlchemyError):
        return "The saved task recovery proof needs review before resuming."
