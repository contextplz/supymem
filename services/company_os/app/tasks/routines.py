"""Founder-owned recurring tasks on the leased jobs outbox."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from app.credits._txn import naive_utc
from app.core.local_mode import is_local
from app.core.local_capabilities import enabled as capability_enabled, blocked_message
from app.db.models import Job, VentureDocument
from app.db.task_models import Task, TaskRun, TaskStep
from app.state import db
from app.tasks import planner, service
from app.tasks.contracts import Contract, next_fire, timezone_name, trigger_from_words
from core.clock import as_utc
from core.ids import new_id
from core.time_context import timezone_context

PREFIX = "routine:"
SAFE_ACTIONS = {
    "research.deep": ("research.start",),
    "gtm.find": ("browse.public",),
    "gtm.journey.prepare": ("browse.public", "outreach.draft"),
    "outreach.draft": ("outreach.draft",),
    "tasks.reply_draft": ("outreach.draft",),
    "tasks.waitlist_summary": ("documents.read",),
}
LOCAL_CAPABILITIES = {
    "research.deep": ("research",),
    "gtm.find": ("find_customers",),
    "gtm.journey.prepare": ("find_customers", "content"),
    "outreach.draft": ("content",),
    "tasks.reply_draft": ("content",),
    "tasks.waitlist_summary": (),
}


def enabled():
    if is_local():
        return capability_enabled("routines")
    from app.credits.policy import get_policy
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("routines_v1") is True


def is_routine(task):
    return bool(task.create_key and task.create_key.startswith(PREFIX))


def require_enabled():
    if not enabled():
        if is_local():
            raise HTTPException(403, blocked_message("routines"))
        raise HTTPException(404, "Not found.")


def quiet_hours(value):
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ValueError("Choose a quiet-hours start and end.")
    for field in ("start", "end"):
        if not isinstance(value[field], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value[field]):
            raise ValueError("Use HH:MM for quiet hours.")
    if value["start"] == value["end"]:
        raise ValueError("Quiet hours must leave time for your routine.")
    return value


def outside_quiet(moment, trigger):
    value = quiet_hours(trigger["quiet_hours"])
    zone = ZoneInfo(timezone_name(trigger["tz"]))
    local = as_utc(moment).astimezone(zone)
    start, end = time.fromisoformat(value["start"]), time.fromisoformat(value["end"])
    clock = local.time().replace(tzinfo=None)
    inside = start <= clock < end if start < end else clock >= start or clock < end
    if not inside:
        return naive_utc(moment)
    day = local.date() + timedelta(days=int(start > end and clock >= start))
    candidate = datetime.combine(day, end, zone)
    # A quiet-hours end in a DST gap moves forward to the first real wall time.
    for _ in range(180):
        choices = [as_utc(candidate.replace(fold=fold)) for fold in (0, 1)]
        valid = [utc for utc in choices if utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None)
                 and utc > as_utc(moment)]
        if valid:
            return naive_utc(min(valid))
        candidate += timedelta(minutes=1)
    raise ValueError("Choose quiet hours with a valid upcoming end.")


def upcoming(trigger, after):
    due = next_fire(trigger, after)
    if due is None:
        raise ValueError("Choose a repeating schedule.")
    return outside_quiet(due, trigger)


def work_text(text):
    prefix = re.match(r"^(?:every|each)\s+(?:morning|day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
                      r"(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?[\s,:]+(.+)$", text, re.I | re.S)
    return prefix[1].strip() if prefix else planner.work_goal(text)


def view(task):
    trigger = json.loads(task.trigger_json)
    return {"id": task.id, "title": task.title, "goal": task.goal, "state": task.state,
            "timezone": trigger["tz"], "quiet_hours": trigger["quiet_hours"],
            "schedule": trigger["rrule"], "approval_digest": approval_digest(task),
            "next_fire_at": as_utc(task.next_fire_at).isoformat() if task.next_fire_at else None}


def approval_digest(task):
    definition = {"title": task.title, "goal": task.goal, "steps": json.loads(task.scope_json)["steps"],
                  "budget": json.loads(task.budget_json)}
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def local_blocker(task, parent=None):
    if parent is not None and approval_digest(task) != approval_digest(parent):
        return "Your routine changed. In your private chat, delete this routine and save your updated request again."
    for step in json.loads(task.scope_json)["steps"]:
        if step["skill"] not in SAFE_ACTIONS:
            return "Use your private chat to request this action separately. Your routines can research, prepare leads, draft messages or summarize saved signups and replies."
        for name in LOCAL_CAPABILITIES[step["skill"]]:
            if not capability_enabled(name):
                return blocked_message(name)
    return None


def approval_card(item):
    from app.chat.confirm import ConfirmationCard
    return ConfirmationCard(action="routines.approve", capability="cap.write.work_start",
        interpretation=f"Allow eligible preparation for your routine: {item['title']}",
        consequences=["Only eligible preparation is covered. Sending, spending, publishing and deletion still need your review."],
        resulting_state={"routine_id": item["id"], "approval_digest": item["approval_digest"]},
        confirm_label="Allow eligible preparation").as_action()


def owned(session, venture_id, identifier):
    from app.api.handoffs_api import _owner
    from app.api.handoffs_api import _locked_task
    workspace, person = _owner(session, venture_id)
    if not isinstance(identifier, str) or not identifier:
        raise HTTPException(404, "Choose one routine from your saved list.")
    query = select(Task).where(Task.workspace_id == workspace.id, Task.venture_id == venture_id,
        Task.owner_person_id == person.id, Task.create_key.startswith(PREFIX), Task.state != "canceled")
    matches = session.scalars(query.where((Task.id == identifier) | (Task.title == identifier))).all()
    if not matches and identifier != identifier.rstrip(".!?"):
        identifier = identifier.rstrip(".!?")
        matches = session.scalars(query.where((Task.id == identifier) | (Task.title == identifier))).all()
    if len(matches) != 1:
        raise HTTPException(404, "Choose one routine from your saved list.")
    identifier = matches[0].id
    task = _locked_task(session, venture_id, identifier)
    if not is_routine(task) or task.state == "canceled":
        raise HTTPException(404, "Open your venture's Routines screen, then choose an active routine from your saved routines." if is_local() else "Not found.")
    return task


def schedule(session, task):
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    trigger = json.loads(task.trigger_json)
    with bind(workspace_id=task.workspace_id, venture_id=task.venture_id, actor=task.owner_person_id), bind_conversation(
            task.venture_id, json.loads(task.scope_json)["routine_conversation_id"], session=session):
        identifier = service.enqueue_owned(session, task, "routines.fire", {
            "routine_id": task.id, "due_at": as_utc(task.next_fire_at).isoformat(),
            "generation": trigger["generation"],
        }, run_after=task.next_fire_at, dedupe_key=f"routine-fire:{task.id}:{trigger['generation']}:{task.next_fire_at.isoformat()}")
    trigger["fire_job_id"] = identifier
    task.trigger_json = json.dumps(trigger)


async def create_from_settings(venture_id, goal, *, timezone=None, quiet=None):
    require_enabled()
    from app.chat import conversations
    from app.tenancy import bind

    with db()() as session:
        workspace, person = service.handoff_owner(session, venture_id)
        workspace_id, actor = workspace.id, person.id
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=actor):
        active = conversations.current_conversation()
        conversation_id = active.id if active else conversations.create(
            venture_id, idempotency_key="routines", title="Your routines")["id"]
        with conversations.bind_conversation(venture_id, conversation_id):
            return await create(venture_id, goal, timezone=timezone, quiet=quiet)


async def create(venture_id, goal, *, timezone=None, quiet=None):
    require_enabled()
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 8000:
        raise ValueError("Describe your routine in 1 to 8000 characters.")
    if timezone is not None and (not isinstance(timezone, str) or not 1 <= len(timezone) <= 80):
        raise ValueError("Choose a valid timezone.")
    from app.api.handoffs_api import _owner
    from app.chat.conversations import resolve
    with db()() as session:
        workspace, person = _owner(session, venture_id)
        zone = timezone_name(timezone or person.timezone)
        wid, actor = workspace.id, person.id
    conversation = resolve(venture_id)
    if conversation.visibility != "private" or conversation.actor_id != actor:
        raise PermissionError("routine_private_conversation_required")
    moment = naive_utc()
    kind, trigger = trigger_from_words(goal, zone, moment)
    if kind != "scheduled":
        raise ValueError("Choose a repeating day and time for your routine.")
    trigger["quiet_hours"] = quiet_hours(quiet if quiet is not None else {"start": "22:00", "end": "08:00"})
    trigger["generation"] = new_id("rtg")
    work = work_text(goal)
    if not work:
        raise ValueError("What should your routine do?")
    with timezone_context(zone):
        contract = await planner.preview(work, timezone=zone, now=moment, workspace_id=wid, venture_id=venture_id)
    contract.kind, contract.trigger = "one_time", {"tz": zone}
    with db()() as session:
        _owner(session, venture_id)
        require_enabled()
        task = service.create(session, workspace_id=wid, venture_id=venture_id, owner_person_id=actor,
                              contract=contract, start=False, create_key=PREFIX + new_id("rtc"))
        task.kind, task.trigger_json, task.deadline_is_estimate = "scheduled", json.dumps(trigger), True
        scope = json.loads(task.scope_json)
        scope["routine_conversation_id"] = conversation.id
        task.scope_json = json.dumps(scope)
        task.goal = work
        if is_local() and (reason := local_blocker(task)):
            raise ValueError(reason)
        task.next_fire_at = upcoming(trigger, moment)
        schedule(session, task)
        session.commit()
        return view(task)


def list_routines(venture_id):
    if not is_local():
        require_enabled()
    from app.api.handoffs_api import _owner
    with db()() as session:
        workspace, person = _owner(session, venture_id)
        rows = session.scalars(select(Task).where(Task.workspace_id == workspace.id, Task.venture_id == venture_id,
            Task.owner_person_id == person.id, Task.create_key.startswith(PREFIX), Task.state != "canceled")
            .order_by(Task.created_at)).all()
        return [view(task) for task in rows]


def change(venture_id, identifier, *, delete=False):
    if not is_local():
        require_enabled()
    with db()() as session:
        task = owned(session, venture_id, identifier)
        service.cancel(session, task)
        task.state = "canceled" if delete else "paused"
        children = session.scalars(select(Task).where(Task.create_key.startswith(f"routine-run:{task.id}:"),
            Task.workspace_id == task.workspace_id, Task.venture_id == task.venture_id, Task.owner_person_id == task.owner_person_id,
            Task.state.not_in(service.TERMINAL))).all()
        for child in children:
            service.cancel(session, child)
        from app.core.jobs import ACTIVE_JOB_STATUSES, request_cancel
        jobs = session.scalars(select(Job).where(Job.workspace_id == task.workspace_id,
            Job.venture_id == task.venture_id, Job.status.in_(ACTIVE_JOB_STATUSES),
            (Job.dedupe_key.startswith(f"routine-fire:{task.id}:") | Job.dedupe_key.startswith(f"routine-report:{task.id}:")))).all()
        for job in jobs:
            request_cancel(session, job.id)
        session.commit()
        return view(task)


def settings(venture_id, identifier, *, timezone, quiet):
    require_enabled()
    zone, hours = timezone_name(timezone), quiet_hours(quiet)
    with db()() as session:
        task = owned(session, venture_id, identifier)
        trigger = json.loads(task.trigger_json)
        from app.core.jobs import request_cancel
        if trigger.get("fire_job_id"):
            request_cancel(session, trigger["fire_job_id"])
        trigger.update(tz=zone, quiet_hours=hours, generation=new_id("rtg"))
        task.trigger_json = json.dumps(trigger)
        if task.state == "submitted":
            task.next_fire_at = upcoming(trigger, naive_utc())
            schedule(session, task)
        task.updated_at = naive_utc()
        session.commit()
        return view(task)


def action_for(task, skill, action_type):
    from app.approvals.standing import card_action
    budget = json.loads(task.budget_json)["cost_micro"]
    scope = json.loads(task.scope_json)
    parent = scope.get("routine_id", task.id)
    return card_action(action_type, {"routine_id": parent, "goal": task.goal, "skill": skill,
        "steps": scope["steps"], "budget": json.loads(task.budget_json),
        "ceiling_micro": 0 if skill == "tasks.waitlist_summary" else budget, "task_allowance_micro": budget})


def approve(venture_id, identifier, expected_digest):
    require_enabled()
    from app.approvals import standing
    if not is_local() and not standing.enabled():
        raise ValueError("Standing approvals are not enabled. Your actions still need an exact review.")
    with db()() as session:
        task = owned(session, venture_id, identifier)
        if expected_digest != approval_digest(task):
            raise ValueError("Your routine changed. List your routines to review its current preparation.")
        if task.state != "submitted":
            raise ValueError("Your routine must be active before you approve it.")
        specs = json.loads(task.scope_json)["steps"]
        grants = [(spec["skill"], action) for spec in specs for action in SAFE_ACTIONS.get(spec["skill"], ())]
        if not grants:
            raise ValueError("Your routine's actions need an exact review each time. No standing approval was saved.")
    for skill, action in grants:
        standing.grant(workspace_id=task.workspace_id, venture_id=venture_id, founder_id=task.owner_person_id,
            action=action_for(task, skill, action), label=task.title)
    with db()() as session:
        owned(session, venture_id, identifier)
        for child in session.scalars(select(Task).where(Task.create_key.startswith(f"routine-run:{task.id}:"),
                Task.workspace_id == task.workspace_id, Task.venture_id == task.venture_id, Task.owner_person_id == task.owner_person_id,
                Task.state == "approval_required")):
            service.cancel(session, child)
        session.commit()
    return "Your eligible preparation has a standing approval. Actions outside that approval still need your review."


def hold_step(session, task, run):
    identifier = json.loads(task.scope_json).get("routine_id")
    if not identifier:
        return False
    parent = session.get(Task, identifier)
    if (not parent or parent.state != "submitted" or not service.active_scope(session, parent)
            or (task.workspace_id, task.venture_id, task.owner_person_id) !=
            (parent.workspace_id, parent.venture_id, parent.owner_person_id)):
        return False
    if is_local() and (reason := local_blocker(task, parent)):
        service.block(session, task, {"class": "needs_input", "reason": "Your routine is waiting for you.",
            "unblock_action": reason}, run=run)
        return True
    moment = naive_utc()
    held = outside_quiet(moment, json.loads(parent.trigger_json))
    if held <= moment:
        return False
    run.control_job_id = service.enqueue_owned(session, task, "tasks.execute", {"task_run_id": run.id},
        run_after=held, dedupe_key=f"routine-quiet:{run.id}:{held.isoformat()}", max_attempts=2)
    return True


def admit_step(session, task, run, step):
    scope = json.loads(task.scope_json)
    identifier = scope.get("routine_id")
    if not identifier:
        return True
    if not enabled():
        service.cancel(session, task)
        return False
    parent = session.get(Task, identifier)
    if (not parent or parent.state != "submitted" or not service.active_scope(session, parent)
            or (task.workspace_id, task.venture_id, task.owner_person_id) !=
            (parent.workspace_id, parent.venture_id, parent.owner_person_id)):
        return False
    actions = SAFE_ACTIONS.get(step.skill, ())
    if not actions:
        return False
    if is_local():
        return local_blocker(task, parent) is None
    from app.approvals import standing
    for action in actions:
        decision = standing.auto_approve(workspace_id=task.workspace_id, venture_id=task.venture_id,
            founder_id=task.owner_person_id, action=action_for(task, step.skill, action), kind="workflow_write",
            subject_ref=step.id, summary=step.title, requested_by="routine", session=session)
        if decision is None:
            return False
    return True


def reviewed_card_action(session, venture_id, actor, name, payload):
    from app.authz.workspace import subject_person_id
    binding = payload.get("_goal_step")
    if not enabled() or name not in {"gtm.find", "outreach.draft"} or not isinstance(binding, dict):
        return None
    if set(binding) != {"task_id", "run_id", "step_id"} or any(not isinstance(value, str) or not value for value in binding.values()):
        return None
    task, run, step = (session.get(model, binding.get(key)) for model, key in
                       ((Task, "task_id"), (TaskRun, "run_id"), (TaskStep, "step_id")))
    if (not task or not run or not step or run.task_id != task.id or step.run_id != run.id
            or step.skill != name or task.venture_id != venture_id or task.owner_person_id != subject_person_id(actor)
            or (task.state, run.state, step.state) != ("approval_required",) * 3):
        return None
    parent = session.get(Task, json.loads(task.scope_json).get("routine_id"))
    if (not parent or parent.state != "submitted" or not service.active_scope(session, parent)
            or (parent.workspace_id, parent.venture_id, parent.owner_person_id) !=
            (task.workspace_id, task.venture_id, task.owner_person_id)):
        return None
    moment = naive_utc()
    if outside_quiet(moment, json.loads(parent.trigger_json)) > moment:
        return None
    saved = json.loads(step.inputs_json).get("_goal_approval", {})
    ceiling = payload.get("ceiling_micro")
    if (saved.get("action") != name or saved.get("payload") != payload or type(ceiling) is not int
            or not 0 <= ceiling <= json.loads(task.budget_json)["cost_micro"]):
        return None
    return action_for(task, name, SAFE_ACTIONS[name][0])


def matching_card_grant(workspace_id, venture_id, actor, name, payload):
    from app.approvals import standing
    with db()() as session:
        candidate = reviewed_card_action(session, venture_id, actor, name, payload)
    if candidate is None:
        return None
    return standing.matching_grant(workspace_id=workspace_id, venture_id=venture_id,
        founder_id=actor, action=candidate)


def _parent(session, payload):
    from app.tasks.locks import lock_task
    task = lock_task(session, payload["routine_id"])
    if (not task or not is_routine(task) or (task.workspace_id, task.venture_id, task.owner_person_id) !=
            (payload.get("workspace_id"), payload.get("venture_id"), payload.get("_actor_subject"))):
        raise PermissionError("routine_scope_mismatch")
    return task


async def fire(payload):
    with db()() as session:
        task = _parent(session, payload)
        due, moment = naive_utc(datetime.fromisoformat(payload["due_at"])), naive_utc()
        if task.state != "submitted" or task.next_fire_at != due or moment < due:
            return
        if not service.active_scope(session, task):
            service.cancel(session, task)
            session.commit()
            return
        trigger = json.loads(task.trigger_json)
        if payload.get("generation") != trigger["generation"]:
            return
        if not enabled():
            task.next_fire_at = upcoming(trigger, moment)
            schedule(session, task)
            session.commit()
            return
        held = outside_quiet(moment, trigger)
        if held > moment:
            task.next_fire_at = held
            schedule(session, task)
            session.commit()
            return
        task.next_fire_at = upcoming(trigger, moment)
        task.last_run_at = moment
        schedule(session, task)
        goal, zone, wid, vid, actor = task.goal, trigger["tz"], task.workspace_id, task.venture_id, task.owner_person_id
        scope = json.loads(task.scope_json)
        identifier, conversation = task.id, scope.get("routine_conversation_id")
        definition = {"title": task.title, "goal": goal, "kind": "one_time", "trigger": {"tz": zone},
            "done_test": json.loads(task.done_test_json), "tolerance": task.tolerance,
            "budget": json.loads(task.budget_json), "estimate_seconds": scope["estimate_seconds"], "steps": scope["steps"]}
        active = session.scalar(select(Task.id).where(Task.create_key.startswith(f"routine-run:{task.id}:"),
            Task.workspace_id == task.workspace_id, Task.venture_id == task.venture_id, Task.owner_person_id == task.owner_person_id,
            Task.state.not_in(service.TERMINAL)))
        if active:
            session.commit()
            return
        if is_local() and (reason := local_blocker(task)):
            session.commit()
            post(payload, reason, f"routine-blocked:{identifier}:{due.isoformat()}", conversation)
            return
        try:
            contract = Contract.model_validate(definition)
            if not enabled() or not service.active_scope(session, task):
                session.commit()
                return
            child = service.create(session, workspace_id=wid, venture_id=vid, owner_person_id=actor,
                contract=contract, start=False, create_key=f"routine-run:{identifier}:{due.isoformat()}")
            scope = json.loads(child.scope_json)
            scope.update(routine_id=identifier, routine_conversation_id=conversation)
            child.scope_json = json.dumps(scope)
            service.start_run(session, child, "initial")
            service.enqueue_owned(session, task, "routines.report", {"routine_id": identifier, "task_id": child.id,
                "conversation_id": conversation}, run_after=moment + timedelta(seconds=30),
                dedupe_key=f"routine-report:{identifier}:{child.id}:initial")
            session.commit()
        except Exception:
            session.rollback()
            post(payload, "Your routine could not start yet. Your saved work is unchanged. Open Settings > Keys and connections to check your AI provider and monthly spending limit, then retry your routine in your private chat.",
                 f"routine-error:{identifier}:{due.isoformat()}", conversation)
            raise


def post(payload, body, key, conversation, actions=None):
    from app.chat.store import append_message
    with db()() as session:
        parent = _parent(session, payload)
        if not enabled() or parent.state != "submitted" or not service.active_scope(session, parent):
            return
    def authority(session, scope):
        parent = _parent(session, payload)
        if (not enabled() or parent.state != "submitted" or not service.active_scope(session, parent)
                or scope.visibility != "private" or scope.id != conversation or scope.actor_id != parent.owner_person_id):
            raise ValueError("routine_output_stopped")
    try:
        append_message(payload["venture_id"], role="assistant", body_md=body, idempotency_key=key,
                       conversation_id=conversation, output_authority=authority, actions=actions or [])
    except ValueError as exc:
        if str(exc) != "routine_output_stopped":
            raise


def report(payload):
    if not enabled():
        return
    with db()() as session:
        parent = _parent(session, payload)
        if parent.state != "submitted" or not service.active_scope(session, parent):
            return
        moment = naive_utc()
        held = outside_quiet(moment, json.loads(parent.trigger_json))
        if held > moment:
            service.enqueue_owned(session, parent, "routines.report", {key: payload[key] for key in
                ("routine_id", "task_id", "conversation_id")}, run_after=held,
                dedupe_key=f"routine-report:{parent.id}:{payload['task_id']}:{held.isoformat()}")
            session.commit()
            return
        child = session.get(Task, payload["task_id"])
        if (not child or child.create_key is None or not child.create_key.startswith(f"routine-run:{parent.id}:")
                or (child.workspace_id, child.venture_id, child.owner_person_id) !=
                (parent.workspace_id, parent.venture_id, parent.owner_person_id)):
            raise PermissionError("routine_result_scope_mismatch")
        if child.state not in service.TERMINAL | service.WAITING:
            moment = naive_utc() + timedelta(seconds=30)
            service.enqueue_owned(session, parent, "routines.report", {key: payload[key] for key in
                ("routine_id", "task_id", "conversation_id")}, run_after=moment,
                dedupe_key=f"routine-report:{parent.id}:{child.id}:{moment.isoformat()}")
            session.commit()
            return
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == child.id))
        artifact = (session.get(VentureDocument, "task-report:" + run.id)
                    or session.get(VentureDocument, "task-draft:" + run.id)) if run else None
        reason = json.loads(child.blocker_json) if child.blocker_json else None
        body = artifact.body_md if artifact and artifact.venture_id == child.venture_id else (
            " ".join(filter(None, (reason["reason"], reason.get("unblock_action")))) if reason else (
                run.summary if run else "Your work did not start. Retry your routine in your private chat."))
        body = f"Your routine: {child.title}\n\n{body}\n\nReview your saved work in Now."
        review = service.action(child, session=session)
        actions = [review]
        if child.state == "approval_required" and not is_local():
            actions.append(approval_card(view(parent)))
    post(payload, body, f"routine-result:{child.id}", payload.get("conversation_id"), actions)


def command(text):
    if not enabled():
        return None
    phrase = text.strip()
    if re.fullmatch(r"(?:show|list) (?:my |the )?routines[.!?]?|(?:my )?routines[.!?]?", phrase, re.I):
        return "routines.list", {}
    match = re.fullmatch(r"(pause|delete|remove) (?:my )?routine\s+(.+)", phrase, re.I)
    if match:
        return "routines.pause" if match[1].lower() == "pause" else "routines.delete", {"routine_id": match[2]}
    if re.match(r"^(?:every|each)\s+(?:morning|day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", phrase, re.I):
        return "routines.create", {"goal": phrase}
    return None


async def action(venture_id, name, payload):
    require_enabled()
    from app.chat.conversations import resolve
    if resolve(venture_id).visibility != "private":
        raise HTTPException(403, "Manage your routines in your private chat.")
    if name == "routines.create":
        item = await create(venture_id, payload["goal"], timezone=payload.get("timezone"), quiet=payload.get("quiet_hours"))
        if is_local():
            return {"kind": "text", "body_md": f"Your routine is saved: {item['title']} ({item['id']}).\nNext run: {item['next_fire_at']} ({item['timezone']}).\nYou can pause it in your private chat.", "actions": []}
        return {"kind": "text", "body_md": f"Your routine is saved: {item['title']} ({item['id']}).\nNext run: {item['next_fire_at']} ({item['timezone']}).\nYou will be asked before work without a standing approval.", "actions": [approval_card(item)]}
    if name == "routines.list":
        items = list_routines(venture_id)
        body = "\n".join(f"- {item['title']} ({item['id']}): {item['state']}, {item['next_fire_at'] or 'not scheduled'}" for item in items) or "You have no saved routines."
        return {"kind": "text", "body_md": body, "actions": [] if is_local() else [approval_card(item) for item in items if item["state"] == "submitted"]}
    elif name == "routines.approve":
        body = approve(venture_id, payload["routine_id"], payload.get("approval_digest"))
    else:
        change(venture_id, payload["routine_id"], delete=name == "routines.delete")
        body = "Your routine is deleted." if name == "routines.delete" else "Your routine is paused. No further steps will start."
    return {"kind": "text", "body_md": body, "actions": []}
