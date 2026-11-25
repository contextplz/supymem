"""Durable task lifecycle; all work remains on the existing jobs outbox."""
from __future__ import annotations

import json
import logging
import hashlib
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select

from core.clock import as_utc
from core.ids import new_id
from app.authz.workspace import subject_person_id
from app.credits._txn import naive_utc
from app.core.local_mode import is_local
from app.db.models import Job, Person, Venture, Workspace
from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
from app.tasks import planner
from app.tasks.contracts import Budget, Contract, blocker, estimated_deadline, next_fire, timezone_name, trigger_from_words
from app.tenancy import current

TERMINAL = {"completed", "failed", "canceled", "rejected"}
WAITING = {"input_required", "auth_required", "approval_required", "paused"}
_RUN: ContextVar[tuple[str, str] | None] = ContextVar("task_run_step", default=None)


def planning_pending(task):
    scope = json.loads(task.scope_json or "{}")
    journal = scope.get("_planning") if isinstance(scope, dict) else None
    return journal is not None and (not isinstance(journal, dict) or journal.get("state") != "complete")


@contextmanager
def step_context(run_id: str, step_id: str):
    token = _RUN.set((run_id, step_id))
    try:
        yield
    finally:
        _RUN.reset(token)


def event(session, task: Task, kind: str, message: str, *, run_id=None, step_id=None, data=None):
    session.add(TaskEvent(id=new_id("tev"), task_id=task.id, run_id=run_id, step_id=step_id,
                         kind=kind, message=message, data_json=json.dumps(data or {}), created_at=naive_utc()))
    task.updated_at = max(naive_utc(), naive_utc(task.updated_at) + timedelta(microseconds=1))


def block(session, task, reason: dict, *, run=None, step=None):
    from app.tasks.locks import lock_run, lock_step, lock_task
    if step:
        task, run, step = lock_step(session, step.id)
    elif run:
        task, run = lock_run(session, run.id)
    else:
        task = lock_task(session, task.id)
    if task.state in {"canceled", "paused"}:
        return
    state = {"needs_input": "input_required", "needs_auth": "auth_required", "needs_approval": "approval_required",
             "budget_exhausted": "input_required", "policy": "rejected", "impossible": "failed",
             "needs_device_online": "input_required", "transient": "working"}[reason["class"]]
    run_only = bool(reason.get("run_only") and run and task.kind != "one_time" and state in TERMINAL)
    changed = task.blocker_json != json.dumps(reason)
    task.state, task.blocker_json = ("submitted", None) if run_only else (state, json.dumps(reason))
    if run:
        run.state = state
        run.summary = reason["reason"] + " " + reason["unblock_action"]
        if state in TERMINAL:
            run.finished_at = naive_utc()
    if step:
        step.state = state
    if changed:
        event(session, task, "blocker", reason["reason"] + " " + reason["unblock_action"],
              run_id=run.id if run else None, step_id=step.id if step else None, data=reason)
        notice(session, task, "Task run stopped" if run_only else "Task needs your attention", reason["reason"])


def notice(session, task, title, body):
    from app.db.supymem_models import Notice
    if task.venture_id:
        session.add(Notice(id=new_id("not"), workspace_id=task.workspace_id, person_id=task.owner_person_id,
                           kind="task:" + task.id, title=title[:120], body=body[:400], target_path=f"/v/{task.venture_id}/now",
                           created_at=max(naive_utc(), naive_utc(task.updated_at)), read_at=None))


def _active_custody(session, workspace_id, venture_id, owner_person_id):
    # A prior identity-map read must not authorize work after custody changed.
    workspace = session.execute(select(Workspace.owner_id, Workspace.archived_at, Workspace.settings_json).where(Workspace.id == workspace_id)).one_or_none()
    if not workspace or workspace.owner_id != owner_person_id or workspace.archived_at:
        return False
    try:
        settings = json.loads(workspace.settings_json or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(settings, dict) or settings.get("blocked"):
        return False
    if session.scalar(select(Person.id).where(Person.id == owner_person_id)) is None:
        return False
    if venture_id:
        venture = session.execute(select(Venture.workspace_id, Venture.archived_at).where(Venture.id == venture_id)).one_or_none()
        if not venture or venture.workspace_id != workspace_id or venture.archived_at:
            return False
    return True


def active_scope(session, task):
    return _active_custody(session, task.workspace_id, task.venture_id, task.owner_person_id)


def scope_blocker():
    return {"class": "needs_input", "reason": "This task's workspace or ownership changed.",
            "unblock_action": "Review the current workspace and task permissions before continuing."}


def create(session, *, workspace_id, venture_id, owner_person_id, contract: Contract, start=True, create_key=None):
    if not _active_custody(session, workspace_id, venture_id, owner_person_id):
        raise PermissionError("task_scope_mismatch")
    from app.tasks.planner import validate_contract
    contract = validate_contract(contract, goal=contract.goal)
    from app.tasks.skills import get_skill
    from app.tasks.checks import validate
    validate(contract.done_test, whole_task=True)
    for step in contract.steps:
        validate(step.done_test)
        skill = get_skill(step.skill)
        if skill is None:
            raise ValueError("Choose a supported task skill")
        step.runs_on = skill.runs_on
    submitted = contract.model_dump()
    submitted["trigger"] = {key: value for key, value in submitted["trigger"].items() if key != "start_at"}
    digest = hashlib.sha256(json.dumps(submitted, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if create_key:
        from app.db.session import serialize_writes
        serialize_writes(session, key="task-create:" + create_key)
        existing = session.scalar(select(Task).where(Task.create_key == create_key))
        if existing:
            if (existing.workspace_id, existing.venture_id, existing.goal, existing.create_digest) != (workspace_id, venture_id, contract.goal, digest):
                raise ValueError("task_idempotency_conflict")
            return existing
    now = naive_utc()
    person = session.get(Person, owner_person_id)
    zone = contract.trigger.get("tz") or (person.timezone if person else None) or "UTC"
    due = estimated_deadline(contract.goal, zone, now, contract.estimate_seconds)
    from app.tasks import goals
    goal_plan = any(step.subgoal for step in contract.steps) and goals.enabled()
    if goal_plan:
        due = goals.goal_deadline(contract.goal, zone, now, due)
    task = Task(id=new_id("tsk"), workspace_id=workspace_id, venture_id=venture_id,
                owner_person_id=owner_person_id, create_key=create_key, create_digest=digest, title=contract.title, goal=contract.goal, kind=contract.kind,
                trigger_json=json.dumps(contract.trigger), done_test_json=json.dumps(contract.done_test),
                scope_json=json.dumps({"estimate_seconds": contract.estimate_seconds, "steps": [step.model_dump() for step in contract.steps]}),
                budget_json=contract.budget.model_dump_json(), tolerance=contract.tolerance,
                state="submitted", deadline_at=due,
                deadline_is_estimate=not goal_plan, created_at=now, updated_at=now,
                next_fire_at=next_fire(contract.trigger, now) if contract.kind == "scheduled" else None)
    session.add(task)
    session.flush()
    from app.tasks.locks import lock_task
    lock_task(session, task.id)
    event(session, task, "plan", "Your goal plan is ready. Work stops at its deadline or agreed limit." if goal_plan else "Your task plan is ready. Deadlines are estimates.")
    if start and task.kind == "one_time":
        start_run(session, task, "initial")
    return task


def enqueue_owned(session, task, kind, payload, **kwargs):
    """Standing work uses the persisted founder, including public domain events."""
    from app.core.jobs import enqueue
    from app.tenancy import bind
    with bind(workspace_id=task.workspace_id, venture_id=task.venture_id or "", actor=task.owner_person_id):
        return enqueue(session, kind, payload, workspace_id=task.workspace_id, venture_id=task.venture_id, **kwargs)


def start_run(session, task: Task, run_key: str):
    from app.tasks.locks import lock_run, lock_task
    task = lock_task(session, task.id)
    if task.create_key and task.create_key.startswith("routine:"):
        return None
    if planning_pending(task):
        return None
    existing = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.run_key == run_key))
    if existing:
        return existing
    if task.state in {"paused", "canceled", "rejected"}:
        return None
    busy = bool(session.scalar(select(TaskRun.id).where(TaskRun.task_id == task.id, TaskRun.state.in_(("submitted", "working", *WAITING)))))
    if busy and task.kind != "event":
        return None  # Coalesce scheduled fires while the previous run is active.
    queued = busy or task.state in WAITING
    now = naive_utc()
    from app.tasks import goals
    from app.core.local_mode import is_local
    scope = json.loads(task.scope_json)
    goal_plan = any(spec.get("subgoal") or goals.is_action(spec["skill"]) for spec in scope["steps"])
    if goal_plan:
        if session.scalar(select(TaskRun.id).where(TaskRun.task_id == task.id).limit(1)):
            return None
        scope["_goals_v1"] = True
        task.scope_json = json.dumps(scope)
        if not goals.enabled():
            from app.core.local_capabilities import blocked_message
            message = blocked_message("background_work") if is_local() else "Goal planning is not enabled."
            block(session, task, {"class": "needs_input", "reason": message,
                "unblock_action": message if is_local() else "Review your saved plan before continuing."})
            return None
        if is_local():
            queued = False
    duration = json.loads(task.scope_json).get("estimate_seconds", 900)
    person = session.get(Person, task.owner_person_id)
    zone = json.loads(task.trigger_json).get("tz") or (person.timezone if person else None) or "UTC"
    if goal_plan:
        zone = scope.get("_planning", {}).get("accepted_zone", zone)
        scope["_goal_timezone"] = zone
        task.scope_json = json.dumps(scope)
        task.deadline_is_estimate = False
    due = estimated_deadline(task.goal, zone, now, duration)
    if goal_plan:
        due = naive_utc(task.deadline_at)
    run = TaskRun(id=new_id("trn"), task_id=task.id, run_key=run_key, started_at=now,
                  deadline_at=due, state="queued" if queued else "submitted")
    session.add(run)
    session.flush()
    lock_run(session, run.id)
    for position, spec in enumerate(json.loads(task.scope_json)["steps"]):
        session.add(TaskStep(id=new_id("tst"), run_id=run.id, position=position,
                    title=spec["title"], skill=spec["skill"], inputs_json=json.dumps({**spec.get("inputs", {}),
                        **(goals.metadata(spec) if goal_plan else {})}),
                    done_test_json=json.dumps(spec.get("done_test", {})), runs_on=spec.get("runs_on", "server"),
                    state="submitted", deadline_at=run.deadline_at))
    session.flush()
    if queued:
        event(session, task, "trigger_queued", "A new event is saved for the next run.", run_id=run.id)
        return run
    task.state, task.last_run_at, task.deadline_at = "working", now, run.deadline_at
    task.blocker_json = None
    event(session, task, "run_started", "Work is queued for this run.", run_id=run.id)
    run.control_job_id = enqueue_owned(session, task, "tasks.execute", {"task_run_id": run.id},
            dedupe_key=f"task-start:{run.id}", effect_key=f"task-start:{run.id}", max_attempts=2)
    return run


def attach_job(session, job: Job):
    """Project existing admitted workflows into tasks inside the enqueue transaction."""
    from app.tasks.skills import get_skill
    from app.tasks import goals
    context = _RUN.get()
    skill = get_skill(job.kind)
    if (not skill and not (context and goals.is_action(job.kind))) or not job.workspace_id or not job.venture_id:
        return
    if goals.is_action(job.kind) and context is None:
        return
    previous = session.scalar(select(TaskStep).where(TaskStep.job_id == job.id))
    if previous:
        if context and (previous.run_id, previous.id) != context:
            raise ValueError("task_job_already_linked")
        return
    if context:
        from app.tasks.locks import lock_step
        task, run, step = lock_step(session, context[1])
        if not task or not step or step.run_id != run.id or (task.workspace_id, task.venture_id) != (job.workspace_id, job.venture_id):
            raise PermissionError("task_job_scope_mismatch")
        if step.job_id and step.job_id != job.id:
            raise ValueError("task_step_already_queued")
        step.job_id = job.id
        metadata = {key: value for key, value in json.loads(step.inputs_json).items() if key.startswith("_goal_")}
        step.inputs_json = json.dumps({**json.loads(step.inputs_json), **json.loads(job.payload_json), **metadata}) if metadata else job.payload_json
        from app.tasks.budgets import effective_limits
        job.token_budget = max(0, effective_limits(task, run).tokens - run.tokens_used - run.reserved_tokens)
        return
    workspace = session.get(Workspace, job.workspace_id)
    if not workspace:
        return
    now = naive_utc()
    title = getattr(skill, "title", job.kind.replace(".", " ").capitalize())
    goal = title
    if job.kind == "task.run":
        from app.db.models import PlannedTask
        saved = session.get(PlannedTask, json.loads(job.payload_json).get("task_id"))
        if saved and saved.venture_id == job.venture_id and saved.workspace_id in {None, job.workspace_id}:
            goal, title = saved.title, saved.title[:160]
    contract = Contract(title=title, goal=goal, steps=[{"title": title, "skill": job.kind}])
    task = create(session, workspace_id=job.workspace_id, venture_id=job.venture_id,
                  owner_person_id=workspace.owner_id, contract=contract, start=False)
    task.state, task.last_run_at = "working", now
    run = TaskRun(id=new_id("trn"), task_id=task.id, run_key=job.id, started_at=now, deadline_at=task.deadline_at, state="submitted")
    session.add(run)
    session.flush()
    session.add(TaskStep(id=new_id("tst"), run_id=run.id, position=0, title=title, skill=job.kind,
                inputs_json=job.payload_json, job_id=job.id, deadline_at=task.deadline_at, state="submitted"))
    job.token_budget = Budget.model_validate_json(task.budget_json).tokens
    session.flush()


def job_step(session, job_id):
    from app.tasks.locks import lock_for_job
    task, run, step, _ = lock_for_job(session, job_id)
    if not step:
        return None, None, None
    return task, run, step


def guard(session, task, run, *, tokens=0, cost_micro=0, tool_calls=0, attempts=0):
    from app.tasks.budgets import effective_limits
    limits = effective_limits(task, run)
    from app.tasks import goals
    return (goals.guard(session, task, run, tokens=tokens, cost_micro=cost_micro, tool_calls=tool_calls, attempts=attempts)
            and run.task_id == task.id and active_scope(session, task)
            and task.state not in TERMINAL | WAITING and run.state not in TERMINAL | WAITING
            and run.tokens_used + run.reserved_tokens + tokens <= limits.tokens and run.cost_micro + run.reserved_cost_micro + cost_micro <= limits.cost_micro
            and run.tool_calls + tool_calls <= limits.tool_calls and run.attempts + attempts <= limits.attempts
            and naive_utc() < limits.wall_until)


def before_job(session, job_id, attempt):
    task, run, step = job_step(session, job_id)
    if not task:
        return True
    if (run.run_key == job_id and attempt > step.attempt
            and task.state not in {"paused", "canceled"} and run.state not in {"paused", "canceled"}):
        if task.state in TERMINAL | WAITING or run.state in TERMINAL | WAITING:
            return active_scope(session, task)
    if task.state in TERMINAL | WAITING or run.state in TERMINAL | WAITING | {"queued"}:
        return False
    if not active_scope(session, task):
        block(session, task, scope_blocker(), run=run, step=step)
        return False
    if json.loads(task.scope_json).get("_goals_v1") and attempt > 1:
        block(session, task, {"class": "needs_input", "reason": "This goal step already attempted the same action.",
            "unblock_action": "Review its saved result before starting a new goal."}, run=run, step=step)
        return False
    if step.state == "working" and step.attempt == attempt:
        return True
    if not guard(session, task, run, tool_calls=1, attempts=1):
        block(session, task, blocker("budget_exhausted"), run=run, step=step)
        return False
    step.state, step.attempt, step.started_at = "working", attempt, naive_utc()
    run.state = task.state = "working"
    run.attempts += 1
    event(session, task, "step_started", step.title, run_id=run.id, step_id=step.id)
    return True


def complete_run(session, task, run):
    from app.tasks.locks import lock_run
    task, run = lock_run(session, run.id)
    if task.state in TERMINAL | WAITING or run.state in TERMINAL | WAITING:
        return
    from app.tasks import goals
    if not goals.guard(session, task, run):
        block(session, task, blocker("budget_exhausted"), run=run)
        return
    from app.tasks.budgets import effective_limits
    limits = effective_limits(task, run)
    if run.cost_micro > limits.cost_micro or run.tokens_used > limits.tokens:
        block(session, task, blocker("budget_exhausted"), run=run)
        return
    steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)).all()
    from app.tasks.locks import lock_step
    steps = [lock_step(session, step.id)[2] for step in steps]
    if steps and all(step.state == "completed" for step in steps):
        if run.reserved_cost_micro or run.reserved_tokens:
            block(session, task, blocker("usage_reconciliation_required"), run=run)
            return
        from app.tasks.checks import passes
        evidence = []
        for step in steps:
            saved = json.loads(step.evidence_json)
            step_evidence = saved if isinstance(saved, list) else [{"ref": saved["order_ref"]}] if isinstance(saved, dict) and saved.get("order_ref") else []
            if not passes(json.loads(step.done_test_json), step_evidence):
                block(session, task, {"class": "impossible", "reason": "A saved step did not meet its agreed completion check.",
                    "unblock_action": "Review the partial result and adjust the requested check."}, run=run, step=step)
                return
            evidence.extend(step_evidence)
        task_check = json.loads(task.done_test_json)
        if task_check.get("type") == "rubric":
            evaluation = json.loads(run.evaluation_json or "{}")
            if evaluation.get("state") != "complete":
                run.control_job_id = enqueue_owned(session, task, "tasks.verify", {"task_run_id": run.id},
                        dedupe_key=f"task-verify:{run.id}", max_attempts=1)
                event(session, task, "verifying", "The saved result is being checked against your review criteria.", run_id=run.id)
                return
            from app.tasks.verification import artifact_snapshot
            try:
                snapshot = artifact_snapshot(session, task, run)
                artifact_digest, contract_digest = snapshot["artifact_digest"], snapshot["contract_digest"]
            except ValueError:
                block(session, task, blocker("task_result_unverified"), run=run)
                return
            if (evaluation.get("artifact_digest"), evaluation.get("contract_digest")) != (artifact_digest, contract_digest):
                block(session, task, {"class": "needs_input", "reason": "The saved result or review criteria changed after verification.",
                    "unblock_action": "Review the changed result before requesting another check."}, run=run)
                return
            if evaluation.get("score", 0) < task.tolerance:
                block(session, task, {"class": "impossible", "reason": "The saved result did not meet your review criteria.",
                    "unblock_action": "Review the partial result and clarify what should improve."}, run=run)
                return
        elif not passes(task_check, evidence):
            block(session, task, {"class": "impossible", "reason": "The saved result did not meet the agreed completion check.",
                                 "unblock_action": "Review the partial result and adjust the request."}, run=run)
            return
        run.state, run.finished_at, run.summary = "completed", naive_utc(), "All planned result checks passed."
        from app.planning.execute import finalize_research_for_run
        finalize_research_for_run(session, run.id)
        task.state = "completed" if task.kind == "one_time" else "submitted"
        task.blocker_json = None
        quiet = task.kind != "one_time" and all(step.strategy_id == "quiet" for step in steps)
        if not quiet:
            from app.tasks.digest import completed_artifact_digest, completed_contract_digest
            event(session, task, "completed", run.summary, run_id=run.id,
                  data={"contract_digest": completed_contract_digest(task),
                        "artifact_digest": completed_artifact_digest(session, task, run)})
            notice(session, task, "Task finished", task.title)
    elif task.state != "paused" and not any(step.state in WAITING or step.state in {"failed", "rejected", "waiting_for_device", "reconciliation_required"} for step in steps):
        run.control_job_id = enqueue_owned(session, task, "tasks.execute", {"task_run_id": run.id},
                dedupe_key=f"task-next:{run.id}:{sum(step.state == 'completed' for step in steps)}", max_attempts=2)


def after_job(session, job_id, *, error=None, retrying=False):
    task, run, step = job_step(session, job_id)
    if not task or task.state in TERMINAL or step.state == "completed":
        return
    protected = task.state in WAITING - {"paused"} or run.state in WAITING - {"paused"}
    if protected and task.blocker_json:
        # A provider/strategy stop owns its reason and private no-replay journal.
        # The legacy job returning cannot replace that stop or certify completion.
        return
    if error:
        from app.analytics.error_events import closed_code
        logging.getLogger(__name__).warning("task_job_failed", extra={
            "job_id": job_id, "task_id": task.id, "error_code": closed_code(str(error).partition(":")[0]),
            "retrying": retrying})
        if task.state == "paused":
            return
        if retrying:
            step.state = "submitted"
            event(session, task, "retry", "A temporary problem delayed this step. It will retry safely.", run_id=run.id, step_id=step.id)
        else:
            reason = blocker(error)
            if error == "pagebuilder_publication_refused" and step.skill == "pagebuilder.publish" and active_scope(session, task):
                from app.tasks.skills import verify_result
                from app.tenancy import bind
                with bind(workspace_id=task.workspace_id, venture_id=task.venture_id or "", actor=task.owner_person_id):
                    receipt = verify_result(session, step.skill, {**json.loads(step.inputs_json), "_job_id": job_id},
                        workspace_id=task.workspace_id, venture_id=task.venture_id)
                reason = receipt.get("blocker") or reason
            block(session, task, reason, run=run, step=step)
        return
    if not active_scope(session, task):
        if task.state != "paused":
            block(session, task, scope_blocker(), run=run, step=step)
        return
    from app.tenancy import bind
    with bind(workspace_id=task.workspace_id, venture_id=task.venture_id or "", actor=task.owner_person_id):
        _after_job_result(session, job_id, task, run, step)


def reverify_gtm_tasks(session, *, workspace_id, venture_id):
    from app.tasks.locks import lock_step
    from app.tasks.skills import verify_result
    from app.tenancy import bind

    step_ids = session.scalars(select(TaskStep.id).join(TaskRun, TaskRun.id == TaskStep.run_id)
        .join(Task, Task.id == TaskRun.task_id).where(Task.workspace_id == workspace_id,
            Task.venture_id == venture_id, Task.state == "input_required", TaskRun.state == "input_required",
            TaskStep.skill == "gtm.journey.prepare", TaskStep.state == "input_required")
        .order_by(Task.id, TaskStep.position)).all()
    changed = 0
    for step_id in step_ids:
        task, run, step = lock_step(session, step_id)
        reason = json.loads(task.blocker_json or "{}")
        if (task.state != "input_required" or run.state != "input_required" or step.state != "input_required"
                or reason.get("class") != "needs_input"
                or not active_scope(session, task)):
            continue
        with bind(workspace_id=task.workspace_id, venture_id=task.venture_id, actor=task.owner_person_id):
            receipt = verify_result(session, step.skill, {**json.loads(step.inputs_json), "_job_id": step.job_id},
                workspace_id=task.workspace_id, venture_id=task.venture_id)
            if receipt["verified"]:
                task.state, run.state, task.blocker_json = "working", "working", None
                _after_job_result(session, step.job_id, task, run, step)
                changed += 1
    return changed


def _after_job_result(session, job_id, task, run, step):
    # Worker completion and global scheduler recovery both verify the original
    # owned receipts in the same transaction, including existing RLS tables.
    from app.tasks.skills import verify_result
    receipt = verify_result(session, step.skill, {**json.loads(step.inputs_json), "_job_id": job_id},
                            workspace_id=task.workspace_id, venture_id=task.venture_id)
    from app.tasks import goals
    if goals.is_action(step.skill):
        goals.finish(session, task, run, step, receipt)
        return
    if task.state == "paused" and not receipt["verified"]:
        return
    step.evidence_json = json.dumps(receipt.get("evidence", []))
    if receipt["verified"]:
        from app.tasks.checks import passes
        if not passes(json.loads(step.done_test_json), receipt.get("evidence", [])):
            block(session, task, {"class": "impossible", "reason": "This step did not meet its agreed result count.",
                                 "unblock_action": "Review the partial result and adjust the request."}, run=run, step=step)
            return
        step.state, step.finished_at = "completed", naive_utc()
        event(session, task, "step_finished", "Verified: " + step.title, run_id=run.id, step_id=step.id, data={"evidence": receipt.get("evidence", [])})
        complete_run(session, task, run)
    elif receipt.get("pending"):
        step.state = "verifying"
    else:
        block(session, task, receipt.get("blocker") or blocker("result_unverified"), run=run, step=step)


def cancel(session, task):
    from app.tasks.locks import lock_run, lock_task
    task = lock_task(session, task.id)
    task.state, task.blocker_json, task.next_fire_at = "canceled", None, None
    runs = session.scalars(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.state.not_in(TERMINAL))).all()
    for run in runs:
        _, run = lock_run(session, run.id)
        run.state, run.finished_at = "canceled", naive_utc()
        for step in session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)):
            from app.tasks.locks import lock_step
            _, _, step = lock_step(session, step.id)
            if step.state != "completed":
                step.state = "canceled"
            if step.job_id:
                from app.core.jobs import request_cancel
                request_cancel(session, step.job_id)
        if run.control_job_id:
            from app.core.jobs import request_cancel
            request_cancel(session, run.control_job_id)
    event(session, task, "canceled", "Stopped. Calls already in progress may finish; no further steps will start.")


def fire_due(session, *, now):
    from app.tasks.locks import lock_run, lock_step, lock_task
    moment = naive_utc(now)
    fired = []
    tasks = session.scalars(select(Task).where(Task.kind == "scheduled", Task.next_fire_at <= moment,
        or_(Task.create_key.is_(None), ~Task.create_key.startswith("routine:")),
        Task.state.in_(("submitted", "working"))).order_by(Task.next_fire_at).limit(100).with_for_update(skip_locked=True)).all()
    for task in tasks:
        task = lock_task(session, task.id)
        if task.state not in {"submitted", "working"} or task.next_fire_at is None or naive_utc(task.next_fire_at) > moment:
            continue
        try:
            with session.begin_nested():
                due = naive_utc(task.next_fire_at)
                run = start_run(session, task, "schedule:" + due.isoformat())
                task.next_fire_at = next_fire(json.loads(task.trigger_json), moment)
        except Exception:
            block(session, task, {"class": "impossible", "reason": "The saved schedule could not start this task.",
                                 "unblock_action": "Review the task's deadline and schedule before restarting it."})
            task.next_fire_at = None
            session.flush()
            continue
        if run:
            fired.append(run.id)
    for step in session.scalars(select(TaskStep).where(TaskStep.job_id.is_not(None),
            TaskStep.state.in_(("submitted", "working", "verifying"))).limit(100)):
        job = session.get(Job, step.job_id)
        if job and job.status in {"succeeded", "done"}:
            after_job(session, step.job_id)
        elif job and job.status in {"failed_dead", "failed", "poison"}:
            after_job(session, step.job_id, error=job.last_error or "result_unverified")
    for run in session.scalars(select(TaskRun).where(TaskRun.control_job_id.is_not(None),
            TaskRun.state.in_(("submitted", "working"))).limit(100)):
        control = session.get(Job, run.control_job_id)
        if control and control.status in {"failed_dead", "failed", "poison"}:
            block(session, session.get(Task, run.task_id), blocker(control.last_error or "task_result_unverified"), run=run)
    pending = session.scalars(select(TaskRun).join(Task, Task.id == TaskRun.task_id).where(
        TaskRun.state == "queued", Task.state == "submitted").order_by(TaskRun.started_at, TaskRun.id)
        .limit(100)).all()
    for run in pending:
        task, run = lock_run(session, run.id)
        if run.state != "queued" or task.state != "submitted" or session.scalar(select(TaskRun.id).where(
                TaskRun.task_id == task.id, TaskRun.state.in_(("submitted", "working", *WAITING)))):
            continue
        duration = json.loads(task.scope_json).get("estimate_seconds", 900)
        run.state, run.started_at, run.deadline_at = "submitted", moment, moment + timedelta(seconds=duration)
        for step in session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)):
            _, _, step = lock_step(session, step.id)
            step.deadline_at = run.deadline_at
        task.state, task.last_run_at, task.deadline_at = "working", moment, run.deadline_at
        event(session, task, "run_started", "The next saved event is ready to run.", run_id=run.id)
        run.control_job_id = enqueue_owned(session, task, "tasks.execute", {"task_run_id": run.id},
                      dedupe_key=f"task-start:{run.id}", effect_key=f"task-start:{run.id}", max_attempts=2)
        fired.append(run.id)
    from app.tasks.device import sweep_stale_leases
    sweep_stale_leases(session, now=now)
    from app.tasks import goals
    goals.reconcile(session)
    # ETA slip is a single persisted event per run, independent of polling count.
    runs = session.scalars(select(TaskRun).where(TaskRun.state == "working", TaskRun.deadline_at < moment).limit(100)).all()
    for run in runs:
        task, run = lock_run(session, run.id)
        if task.state in TERMINAL | WAITING or run.state != "working":
            continue
        if not session.scalar(select(TaskEvent.id).where(TaskEvent.run_id == run.id, TaskEvent.kind == "eta_slip")):
            event(session, task, "eta_slip", "This run is taking longer than the estimate. The saved progress is still available.", run_id=run.id)
            notice(session, task, "Task estimate changed", "This run is taking longer than its estimate.")
        if not guard(session, task, run):
            block(session, task, blocker("budget_exhausted"), run=run)
    return fired


def emit_domain_event(session, *, workspace_id, venture_id, event_type, event_id, data):
    """Call from verified webhook transactions; filter only DB-owned domain fields."""
    if not event_id or len(event_id) > 200:
        raise ValueError("task_event_id_required")
    runs = []
    rows = session.scalars(select(Task).where(Task.workspace_id == workspace_id, Task.venture_id == venture_id,
                                              Task.kind == "event", Task.state.in_(("submitted", "working", "input_required", "auth_required", "approval_required")))
                           .with_for_update()).all()
    for task in rows:
        from app.tasks.locks import lock_task
        task = lock_task(session, task.id)
        trigger = json.loads(task.trigger_json)
        if trigger.get("event_type") == event_type and all(data.get(key) == value for key, value in trigger.get("filter", {}).items()):
            run = start_run(session, task, f"event:{event_type}:{event_id}")
            if run:
                runs.append(run.id)
    return runs


def action(task, steps=(), *, session=None):
    """One link to the existing workflow that owns the required review."""
    base = f"/v/{task.venture_id}"
    if any(step.skill == "research.deep" for step in steps) and task.state in {"input_required", "failed", "canceled"}:
        return {"label": "Run research again", "href": base + "/tasks#research-" + task.id}
    if task.state == "completed" and (any(step.skill == "gtm.journey.prepare" for step in steps)
            or any(spec.get("skill") == "gtm.journey.prepare" for spec in json.loads(task.scope_json or "{}").get("steps", []) if isinstance(spec, dict))):
        from app.db.models import GtmContact
        from app.gtm.journey import _consumer, _json, _row
        from app.gtm.reachable import visible
        from app.state import db
        with (nullcontext(session) if session is not None else db()()) as reader:
            row = _row(reader, task.venture_id)
            data = _json(row.provenance_json) if row else {}
            prospects = reader.scalar(select(GtmContact.id).where(GtmContact.workspace_id == task.workspace_id,
                GtmContact.source.startswith(f"venture:{task.venture_id}:", autoescape=True)).limit(1))
            consumer = _consumer(data.get("icp") or {})
            leads = visible((data.get("reachable_leads") or []) + (data.get("industry_experts") or []), task.workspace_id,
                b2b=not consumer, venture_id=task.venture_id, session=reader)
        if not (prospects and not consumer or leads):
            return {"label": "Find customers", "href": f"/gtm?venture={task.venture_id}&step=prepare"}
        return {"label": "Review reachable leads", "href": f"/gtm?venture={task.venture_id}&step=review"}
    if task.state == "auth_required":
        if json.loads(task.blocker_json or "{}").get("account_review_required"):
            return {"label": "Review your account", "href": base + "/settings"}
        return {"label": "Connect the required account", "href": base + "/connections"}
    if task.state == "approval_required":
        skills = {step.skill for step in steps}
        if not skills:
            try:
                specs = json.loads(task.scope_json).get("steps", [])
                skills = {spec.get("skill") for spec in specs if isinstance(spec, dict)}
            except (ValueError, TypeError, AttributeError):
                skills = set()
        if any(str(skill).startswith(("pagebuilder.", "deploy.")) for skill in skills):
            return {"label": "Review the website action", "href": base + "/site"}
        return {"label": "Review the required action", "href": base + "/tasks"}
    if task.state == "input_required":
        return {"label": "Review what the task needs", "href": base + "/tasks"}
    return {"label": "Review saved task" if task.state in TERMINAL | {"paused"} else "View task progress",
            "href": base + "/tasks"}


def view(session, task, *, timeline=True):
    run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.state != "queued").order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(1))
    steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all() if run else []
    events = session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc()).limit(30)).all() if timeline else []
    def iso(dt):
        return as_utc(dt).isoformat() if dt else None
    deadline = run.deadline_at if run else task.deadline_at
    if task.state == "submitted" and task.kind in {"scheduled", "event"}:
        estimate_seconds = json.loads(task.scope_json).get("estimate_seconds", 900)
        deadline = task.next_fire_at + timedelta(seconds=estimate_seconds) if task.kind == "scheduled" and task.next_fire_at else None
    reason = json.loads(task.blocker_json) if task.blocker_json else None
    research_step = next((step for step in steps if step.skill == "research.deep"), None)
    research_recovery = None
    if research_step and task.state in {"input_required", "failed", "canceled"}:
        from app.db.supymem_models import ResearchDossier
        from app.research.execution import identifier
        inputs = json.loads(research_step.inputs_json or "{}")
        if inputs.get("revision_id"):
            research_recovery = {"revision_id": inputs["revision_id"], "job_id": research_step.job_id}
            dossier = session.get(ResearchDossier, identifier(research_step.job_id)) if research_step.job_id else None
            if dossier and (dossier.workspace_id, dossier.venture_id) == (task.workspace_id, task.venture_id):
                research_recovery["saved_href"] = f"/v/{task.venture_id}/research?dossier={dossier.id}"
            if reason and reason.get("class") == "needs_input":
                reason = {**reason, "reason": "Research was interrupted. Your saved sources are kept.",
                          "unblock_action": "Run research again, or skip for now."}
    can_continue = False
    can_resume = False
    if task.state in {"paused", "auth_required"}:
        from app.tasks.budgets import resume_eligibility
        can_resume = resume_eligibility(session, task, run if run and run.state not in TERMINAL else None) is None
    if run and reason and reason.get("class") == "budget_exhausted":
        from app.tasks.budgets import eligibility
        try:
            can_continue = eligibility(session, task, run) is None
        except ValueError:
            can_continue = False
    return {"id": task.id, "title": task.title, "goal": task.goal, "kind": task.kind, "state": task.state,
            "updated_at": iso(task.updated_at), "can_edit": can_edit(session, task),
            "can_continue": can_continue,
            "can_resume": can_resume,
            "research_recovery": research_recovery,
            "deadline_at": iso(deadline), "deadline_is_estimate": True,
            "next_fire_at": iso(task.next_fire_at), "blocker": reason,
            "action": action(task, steps, session=session),
            "steps": [{"id": step.id, "title": step.title, "state": step.state, "deadline_at": iso(step.deadline_at)} for step in steps],
            "events": [{"id": item.id, "kind": item.kind,
                        "message": "Research was interrupted. Run research again, or skip for now."
                        if research_recovery and ("reconciliation" in item.message or "saved research outcome" in item.message)
                        else item.message, "created_at": iso(item.created_at)} for item in reversed(events)]}


def can_edit(session, task):
    if task.kind == "one_time" or planning_pending(task):
        return False
    active = session.scalar(select(TaskRun.id).where(TaskRun.task_id == task.id, TaskRun.state.not_in(TERMINAL)))
    if active:
        return False
    linked = session.scalar(select(Job.id).join(TaskStep, TaskStep.job_id == Job.id).join(TaskRun, TaskRun.id == TaskStep.run_id)
        .where(TaskRun.task_id == task.id, Job.status.in_(("leased", "running"))))
    return linked is None


def handoff_not_found():
    return "Open your venture home, refresh Tasks, then choose an available saved task." if is_local() else "Not found."


def handoff_owner(session, venture_id):
    scope = current()
    venture = session.get(Venture, venture_id)
    workspace = session.get(Workspace, venture.workspace_id) if venture else None
    if (not venture or not workspace or venture.archived_at or workspace.archived_at
            or scope.workspace_id != workspace.id or (scope.venture_id and scope.venture_id != venture_id)):
        raise HTTPException(404, handoff_not_found())
    if workspace.owner_id != subject_person_id(scope.actor):
        raise HTTPException(403, "Open a venture you own, then choose your saved task in Tasks." if is_local() else "The founder manages these tasks.")
    if not _active_custody(session, workspace.id, venture_id, subject_person_id(scope.actor)):
        raise HTTPException(404, handoff_not_found())
    person = session.get(Person, workspace.owner_id)
    if person is None:
        raise HTTPException(404, handoff_not_found())
    return workspace, person


def handoff_task(session, venture_id, handoff_id):
    workspace, _ = handoff_owner(session, venture_id)
    task = session.get(Task, handoff_id)
    if not task or (task.workspace_id, task.venture_id, task.owner_person_id) != (workspace.id, venture_id, workspace.owner_id):
        raise HTTPException(404, handoff_not_found())
    return task


def locked_handoff(session, venture_id, handoff_id):
    handoff_task(session, venture_id, handoff_id)
    from app.tasks.locks import lock_task
    task = lock_task(session, handoff_id)
    handoff_task(session, venture_id, handoff_id)
    return task


def handoff_timezone(person, requested):
    try:
        zone = timezone_name(requested or person.timezone)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if requested:
        person.timezone = zone
    return zone


def list_handoffs(venture_id, *, session_factory):
    with session_factory() as session:
        workspace, _ = handoff_owner(session, venture_id)
        rows = session.scalars(select(Task).where(Task.workspace_id == workspace.id, Task.venture_id == venture_id,
            Task.owner_person_id == workspace.owner_id).order_by(Task.updated_at.desc()).limit(50)).all()
        return {"items": [view(session, task) for task in rows]}


async def preview_handoff(venture_id, body, *, session_factory):
    with session_factory() as session:
        workspace, person = handoff_owner(session, venture_id)
        zone = handoff_timezone(person, body.timezone)
        wid = workspace.id
        session.commit()
    from core.time_context import timezone_context
    try:
        with timezone_context(zone):
            contract = await planner.preview(body.goal, timezone=zone, now=naive_utc(), workspace_id=wid, venture_id=venture_id)
        if contract.kind == "scheduled":
            from app.tasks.contracts import next_fire
            from core.clock import as_utc
            fire = next_fire(contract.trigger, naive_utc())
            contract.trigger["first_fire_at"] = as_utc(fire).isoformat() if fire else None
        return contract.model_dump()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def create_handoff(venture_id, body, idempotency_key=None, *, session_factory):
    if not idempotency_key:
        raise HTTPException(422, "A task request key is required.")
    import json
    from datetime import datetime
    from app.tasks import intake
    try:
        with session_factory() as session:
            workspace, person = handoff_owner(session, venture_id)
            zone = body.timezone or person.timezone or "UTC"
            wid, owner = workspace.id, workspace.owner_id
            key = hashlib.sha256(f"{owner}:{wid}:{venture_id}:{idempotency_key}".encode()).hexdigest()
            task, should_plan = intake.begin(session, workspace_id=wid, venture_id=venture_id,
                owner_person_id=owner, create_key=key, body_input=body, accepted_zone=zone)
            from sqlalchemy import inspect
            if body.timezone and inspect(task).attrs.create_key.history.has_changes():
                person.timezone = json.loads(task.scope_json)[intake.KEY]["accepted_zone"]
            journal = json.loads(task.scope_json)[intake.KEY]
            task_id, request_id = task.id, journal["request_id"]
            session.commit()  # Durable one-call intent precedes the provider.
        if should_plan:
            from core.time_context import timezone_context
            def save_response(contract, usage):
                with session_factory() as session:
                    intake.respond(session, task_id=task_id, workspace_id=wid, venture_id=venture_id,
                        owner_person_id=owner, request_id=request_id, contract=contract, usage=usage)
                    session.commit()
            try:
                with timezone_context(journal["accepted_zone"]):
                    await planner.preview(body.goal, timezone=journal["accepted_zone"],
                        now=datetime.fromisoformat(journal["accepted_at"]), workspace_id=wid,
                        venture_id=venture_id, request_id=request_id, before_meter_exit=save_response)
            except Exception:
                with session_factory() as session:
                    intake.fail(session, task_id=task_id, workspace_id=wid, venture_id=venture_id,
                        owner_person_id=owner, request_id=request_id, uncertain=True)
                    session.commit()
        from app.credits.errors import CreditError
        confirmed_billing = None
        try:
            confirmed_billing = intake.reconcile(task_id=task_id, workspace_id=wid, venture_id=venture_id,
                owner_person_id=owner)
        except (CreditError, ValueError):
            pass  # The saved response remains pending; recover exposes one review action.
        with session_factory() as session:
            handoff_owner(session, venture_id)
            task = intake.recover(session, task_id=task_id, workspace_id=wid, venture_id=venture_id,
                owner_person_id=owner, confirmed_billing=confirmed_billing)
            result = view(session, task)
            session.commit()
        from app.core.jobs import publish_wake
        publish_wake()
        return result
    except intake.IntakeConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(404, handoff_not_found()) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def read_handoff(venture_id, handoff_id, *, session_factory):
    with session_factory() as session:
        return view(session, handoff_task(session, venture_id, handoff_id))


def handoff_result(venture_id, handoff_id, *, session_factory):
    """Read public saved artifacts only after current source receipts pass."""
    import json
    import re
    from app.db.models import Job, PlannedTask, VentureDocument
    from app.secrets.detect import looks_like_secret
    from app.tasks import checks, skills

    def load_json(value, default):
        try:
            loaded = json.loads(value or "null")
            return loaded if isinstance(loaded, type(default)) else default
        except (TypeError, ValueError):
            return default

    def public_text(value):
        if not isinstance(value, str) or looks_like_secret(value):
            raise ValueError("result_withheld")
        lines = [line for line in value.splitlines() if not re.match(
            r"(?i)^\s*(?:task(?: id)?|job(?: id)?|run(?: id)?|artifact|credits?|tokens?|cost|usage|error(?: code)?|status)\s*:", line)]
        text = re.sub(r"\b(?:tsk|trn|tst|tev|job|doc|ven|wsp|ws|usr|pln|rev|rsd|dwo|agn|not|acc|res)_[A-Za-z0-9_-]{6,}\b", "saved item", "\n".join(lines))
        return text.replace("\u2014", ", ")

    with session_factory() as session:
        task = handoff_task(session, venture_id, handoff_id)
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.state != "queued")
                             .order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(1))
        result = {"title": "Saved task result", "body_md": "No saved result is available for this run yet.",
                  "status": "pending", "partial": True, "artifacts": []}
        if not run:
            return result
        steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all()
        bodies, links, evidence, artifacts = [], [], [], {}
        verified = bool(steps)
        for step in steps:
            receipt = None
            if step.skill == "tasks.waitlist_summary":
                from app.tasks.reports import verify_report
                receipt = verify_report(session, task_id=task.id, run_id=run.id, workspace_id=task.workspace_id, venture_id=venture_id)
            elif step.skill == "tasks.reply_draft":
                from app.tasks.drafts import verify_draft
                receipt = verify_draft(session, task_id=task.id, run_id=run.id, step_id=step.id,
                                       workspace_id=task.workspace_id, venture_id=venture_id)
            elif step.job_id:
                job = session.get(Job, step.job_id)
                inputs = load_json(step.inputs_json, {})
                if isinstance(inputs, dict):
                    receipt = skills.verify_result(session, step.skill, {**inputs, "_job_id": step.job_id},
                        workspace_id=task.workspace_id, venture_id=venture_id)
            saved = load_json(step.evidence_json, [])
            saved_refs = {item.get("ref") for item in saved if isinstance(item, dict)} if isinstance(saved, list) else set()
            current_evidence = receipt.get("evidence", []) if receipt and receipt.get("verified") else []
            refs = {item.get("ref") for item in current_evidence if isinstance(item, dict)}
            source_verified = bool(refs) and all(isinstance(ref, str) and 0 < len(ref) <= 220 for ref in refs)
            current = source_verified and refs == saved_refs and step.state == "completed"
            try:
                current = current and checks.passes(load_json(step.done_test_json, {}), current_evidence)
            except (TypeError, ValueError):
                current = False
            verified &= current
            if source_verified:
                evidence.extend(current_evidence)
                for ref in sorted(refs):
                    if not isinstance(ref, str):
                        continue
                    kind, _, identifier = ref.partition(":")
                    if kind == "venture_document":
                        row = session.get(VentureDocument, identifier)
                        if row and row.venture_id == venture_id and row.kind in {"task_note", "task_report", "task_reply_draft"}:
                            bodies.append(row.body_md)
                    from app.tasks.verification import _artifact
                    try:
                        artifacts[ref] = _artifact(session, task, step, ref)
                    except (KeyError, TypeError, ValueError):
                        verified = False
                route, label = {"research.deep": ("research", "Read saved research"),
                                "pagebuilder.build": ("site", "Review website preview"),
                                "pagebuilder.publish": ("site", "Review published website")}.get(step.skill, (None, None))
                if route:
                    links.append({"label": label, "href": f"/v/{venture_id}/{route}"})
                elif step.skill == "gtm.journey.prepare":
                    links.append({"label": "Review prospects and drafts", "href": f"/gtm?venture={venture_id}"})
            elif step.skill == "task.run" and step.job_id:
                # A blocked generic workflow may have useful unfinished prose.
                # Its authoritative job chooses the note, never a caller's ref.
                job = session.get(Job, step.job_id)
                payload = load_json(job.payload_json, {}) if job else {}
                planned = session.get(PlannedTask, payload.get("task_id")) if isinstance(payload, dict) and isinstance(payload.get("task_id"), str) else None
                note = session.get(VentureDocument, "task-note:" + planned.id) if planned else None
                if (job and (job.kind, job.workspace_id, job.venture_id) == ("task.run", task.workspace_id, venture_id)
                        and planned and planned.venture_id == venture_id and planned.workspace_id in {None, task.workspace_id}
                        and note and (note.kind, note.venture_id, note.title) == ("task_note", venture_id, planned.title)
                        and load_json(note.provenance_json, {}) == {"task_id": planned.id, "source": "task.run"}):
                    bodies.append(note.body_md)
        try:
            receipts_verified = verified
            verified &= checks.passes(load_json(task.done_test_json, {}), evidence)
            if load_json(task.done_test_json, {}).get("type") == "rubric":
                from app.tasks.verification import _criteria, _digest
                evaluation = load_json(run.evaluation_json, {})
                verified = receipts_verified and bool(artifacts) and (
                    evaluation.get("state") == "complete" and evaluation.get("valid") is True
                    and type(evaluation.get("score")) in {int, float} and evaluation["score"] >= task.tolerance
                    and evaluation.get("artifact_digest") == _digest(sorted(artifacts.items()))
                    and evaluation.get("contract_digest") == _digest({"goal": task.goal, "criteria": _criteria(task), "tolerance": task.tolerance}))
            body = "\n\n".join(public_text(text) for text in bodies if text)
            if len(body.encode("utf-8")) > 16_000 or any(looks_like_secret(text) for text in artifacts.values()):
                raise ValueError("result_withheld")
        except (TypeError, ValueError, KeyError):
            return {**result, "status": "withheld", "body_md": "The saved result needs review before it can be displayed. Open the task's saved progress."}
        finished = verified and run.state == "completed"
        if body or links:
            return {"title": "Saved task result", "body_md": ("Saved partial work. This result has not passed the current task's completion check.\n\n" if not finished else "")
                    + (body or "The saved workflow result is available through the links below."),
                    "status": "verified" if finished else "partial", "partial": not finished, "artifacts": links}
        return {**result, "status": "unconfirmed" if run.state in TERMINAL else "pending"}


def cancel_handoff(venture_id, handoff_id, *, session_factory):
    with session_factory() as session:
        task = locked_handoff(session, venture_id, handoff_id)
        if task.state != "canceled":
            cancel(session, task)
        result = view(session, task)
        session.commit()
        return result


def cancel_handoff_task(task_id, *, session_factory):
    with session_factory() as session:
        task = session.get(Task, task_id)
        if not task or not task.venture_id:
            raise HTTPException(404, handoff_not_found())
        venture_id = task.venture_id
        handoff_task(session, venture_id, task_id)
    return cancel_handoff(venture_id, task_id, session_factory=session_factory)


def pause_handoff(venture_id, handoff_id, *, session_factory):
    with session_factory() as session:
        task = locked_handoff(session, venture_id, handoff_id)
        if task.state in {"submitted", "working"}:
            task.state = "paused"
            event(session, task, "paused", "Paused. Calls already in progress may finish; no further step will start.")
        elif task.state not in TERMINAL | {"paused"}:
            raise HTTPException(409, "This task is already waiting for your attention. Review its saved requirement.")
        result = view(session, task)
        session.commit()
        return result


def resume_handoff(venture_id, handoff_id, body=None, idempotency_key=None, *, session_factory):
    import json
    from datetime import datetime, timedelta
    from core.clock import as_utc
    from app.db.session import serialize_writes
    with session_factory() as session:
        task = locked_handoff(session, venture_id, handoff_id)
        serialize_writes(session, key="task-budget:" + task.id)
        if planning_pending(task):
            raise HTTPException(409, "The earlier task plan needs its outcome checked before any work can start.")
        if body and body.continue_budget and isinstance(idempotency_key, str) and idempotency_key:
            from app.tasks.budgets import epoch_for_key
            candidates = session.scalars(select(TaskRun).where(TaskRun.task_id == task.id)
                .order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(100)).all()
            try:
                if any(epoch_for_key(candidate, idempotency_key) is not None for candidate in candidates):
                    return view(session, task)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        if task.state in TERMINAL:
            raise HTTPException(409, "This task has ended. Create a new task to run it again.")
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.state.not_in(TERMINAL), TaskRun.state != "queued").with_for_update())
        if run:
            from app.tasks.locks import lock_run
            _, run = lock_run(session, run.id)
        reason = json.loads(task.blocker_json or "{}")
        epoch = 0
        if body and body.continue_budget:
            from app.tasks.budgets import epoch_for_key, grant
            if not run or not isinstance(idempotency_key, str) or not idempotency_key:
                raise HTTPException(422, "Refresh this task before choosing Continue.")
            try:
                if epoch_for_key(run, idempotency_key) is not None:
                    return view(session, task)
                expected = as_utc(datetime.fromisoformat((body.expected_updated_at or "").replace("Z", "+00:00")))
                if expected != as_utc(task.updated_at):
                    raise HTTPException(409, "This task changed. Refresh it before choosing Continue.")
                if reason.get("class") != "budget_exhausted":
                    raise HTTPException(409, "Review the current task requirement before continuing.")
                epoch = grant(session, task, run, idempotency_key)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            run.deadline_at = naive_utc() + timedelta(minutes=15)
            task.deadline_at = run.deadline_at
        elif reason.get("class") == "budget_exhausted":
            raise HTTPException(409, "Review the partial result and choose Continue to approve another bounded attempt.")
        elif task.state not in WAITING:
            return view(session, task)
        if not epoch:
            from app.tasks.budgets import resume_eligibility
            refused = resume_eligibility(session, task, run)
            if refused is not None:
                raise HTTPException(409, refused)
        task.state, task.blocker_json = "submitted", None
        if run:
            run.state, task.state = "working", "working"
            steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)).all()
            from app.tasks.locks import lock_step
            steps = [lock_step(session, step.id)[2] for step in steps]
            for step in steps:
                if step.state in WAITING:
                    step.state = "verifying" if step.job_id else "submitted"
                elif not step.job_id and step.skill == "tasks.reply_draft" and step.state == "admitting":
                    step.state = "submitted"  # Resume proof excludes live/unknown calls.
            if steps and all(step.state == "completed" for step in steps) and json.loads(task.done_test_json).get("type") != "rubric":
                complete_run(session, task, run)
            if run.state in TERMINAL | WAITING:
                result = view(session, task)
                session.commit()
                return result
            kind = "tasks.verify" if steps and all(step.state == "completed" for step in steps) and json.loads(task.done_test_json).get("type") == "rubric" else "tasks.execute"
            key = f"task-continue:{run.id}:{epoch}" if epoch else f"task-resume:{run.id}:{naive_utc().isoformat()}"
            run.control_job_id = enqueue_owned(session, task, kind, {"task_run_id": run.id},
                    dedupe_key=key, effect_key=key, max_attempts=1 if kind == "tasks.verify" else 2)
        elif task.kind == "one_time":
            start_run(session, task, "resume:" + naive_utc().isoformat())
        elif task.kind == "scheduled" and task.next_fire_at is None:
            from app.tasks.contracts import next_fire
            try:
                task.next_fire_at = next_fire(json.loads(task.trigger_json), naive_utc())
            except ValueError as exc:
                raise HTTPException(409, "Review the saved schedule before resuming this task.") from exc
        event(session, task, "resumed", "Resumed from the saved checkpoint.")
        result = view(session, task)
        session.commit()
        return result


def edit_handoff(venture_id, handoff_id, body, *, session_factory):
    from core.clock import as_utc
    from app.tasks.planner import validate_contract
    from app.tasks.contracts import next_fire
    from datetime import datetime, timedelta
    import json
    with session_factory() as session:
        task = locked_handoff(session, venture_id, handoff_id)
        try:
            expected = as_utc(datetime.fromisoformat(body.expected_updated_at.replace("Z", "+00:00")))
        except ValueError as exc:
            raise HTTPException(422, "Refresh this task before editing its future runs.") from exc
        if expected != as_utc(task.updated_at):
            raise HTTPException(409, "This task changed. Refresh it before saving.")
        if not can_edit(session, task):
            raise HTTPException(409, "Stop the current run and wait for its calls to finish before editing future runs.")
        if body.contract is None:
            raise HTTPException(422, "Preview this change before saving it.")
        _, person = handoff_owner(session, venture_id)
        zone = handoff_timezone(person, body.timezone)
        try:
            kind, trigger = trigger_from_words(body.goal, zone, naive_utc())
            if kind == "one_time":
                raise ValueError("Choose a frequency or event for future runs.")
            contract = validate_contract(body.contract.model_copy(update={"goal": body.goal, "kind": kind, "trigger": trigger}), goal=body.goal)
        except ValueError as exc:
            raise HTTPException(422, "Choose a supported goal, frequency and completion check.") from exc
        task.title, task.goal, task.kind = contract.title, body.goal, kind
        task.trigger_json, task.done_test_json = json.dumps(trigger), json.dumps(contract.done_test)
        task.scope_json = json.dumps({"estimate_seconds": contract.estimate_seconds, "steps": [step.model_dump() for step in contract.steps]})
        # Editing future work never silently increases the agreed run budget.
        task.tolerance = contract.tolerance
        task.state, task.blocker_json = "paused", None
        task.deadline_at = naive_utc() + timedelta(seconds=contract.estimate_seconds)
        task.next_fire_at = next_fire(trigger, naive_utc()) if kind == "scheduled" else None
        event(session, task, "edited", "Future runs were updated. Resume when you are ready.")
        result = view(session, task)
        session.commit()
        return result


def handoff_session_factory():
    from app.state import db
    return db()
