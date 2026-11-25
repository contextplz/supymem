"""Private saved-message task commands; router precedence stays with chat owners."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.chat.conversations import current_conversation
from app.core.local_mode import is_local
from app.db.models import ChatConversation, ChatMessage, Person, Venture, Workspace
from app.db.session import serialize_writes
from app.db.task_models import Task, TaskEvent
from app.state import db
from app.tasks import intake, planner, service
from app.tasks.contracts import Contract, consequential_blocker, requires_reviewed_action, timezone_name, trigger_from_words
from app.tenancy import current_or_none
from core.clock import as_utc, utcnow
from core.copy import strip_emdash
from core.time_context import timezone_context

_STATUS = re.compile(r"(?:what are you working on|what are you running(?: for me)?|what(?:'s| is) running for me|(?:list|show)(?: me)? (?:my |the )?(?:active |standing )?tasks)[.!?]*", re.I)
_STOP = re.compile(r"(?:please\s+)?(?:stop|cancel)\s+(?:the\s+)?task\s+(.+)", re.I | re.S)
_CREATE = re.compile(r"(?:please\s+)?do\s+(?!you\b|we\b|i\b|they\b|not\b|n't\b)(.+)|"
    r"(?:every|each)\s+(?:morning|day|week|month|hour|night|quarter|year|weekday|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b.+|"
    r"(?:daily|weekly|monthly|hourly|nightly)\b.+|"
    r"when\s+(?:(?:someone|a customer|a person)\s+replies|(?:a |someone )?(?:sign[ -]?up|signup)\s+(?:arrives|signs up))\b.+", re.I | re.S)


def classify(text: str) -> str | None:
    """Call after secret, memory, pending question, product and browser routing."""
    raw = (text or "").strip()
    if _STATUS.fullmatch(raw):
        return "status"
    if "?" not in raw:
        if _STOP.fullmatch(raw):
            return "cancel"
        if _CREATE.fullmatch(raw):
            return "change"
        from app.tasks import goals
        if re.fullmatch(r"(?:please\s+)?book\s+\d{1,4}\s+customer\s+calls\s+this\s+week[.!]?", raw, re.I) and goals.enabled():
            return "change"
    return None


def recognizes(text: str) -> bool:
    return classify(text) is not None


class TaskReply(dict):
    """Only a server-created reply carries authority for its private write."""
    def __init__(self, reply, *, assistant_key, output_authority):
        super().__init__(reply)
        self.assistant_key = assistant_key
        self.output_authority = output_authority


class TaskPaused(PermissionError):
    pass


def _reply(venture_id, body, *, kind="task_handoff", now=False, task_id=None):
    from app.tasks import goals
    from app.secrets.detect import looks_like_secret
    if looks_like_secret(body):
        body, kind = "Sensitive task text was withheld. Review the saved task through its secure settings.", "needs_you"
    body = strip_emdash(body)
    body = re.sub(r"\b(?:tsk|trn|tst|tev|job|msg|cnv|ven|wsp|usr|doc)_[A-Za-z0-9_-]+\b", "saved item", body)
    body = re.sub(r"(?i)\b[\d,.]+\s+credits?\b", "the agreed limit", body)
    return {"kind": kind, "body_md": body, "citations": [],
            "actions": [{"label": "Open Now" if now else "Open tasks", "href": f"/v/{venture_id}/{'now' if now else 'tasks'}", "style": "primary",
                         **({"task_id": task_id} if goals.enabled() and task_id else {})}]}


def _source(session, venture_id, text, source_message_id, *, new_work=False):
    from app.authz.workspace import subject_person_id
    scope, conversation = current_or_none(), current_conversation()
    venture = session.get(Venture, venture_id)
    workspace = session.get(Workspace, venture.workspace_id) if venture else None
    actor = subject_person_id(scope.actor) if scope else None
    # These rows may already be cached from an earlier read in the apply
    # transaction. Prove their current database fields without replacing the
    # caller's mapped identities or flushing unrelated pending writes.
    with session.no_autoflush:
        source = session.execute(select(ChatMessage.id, ChatMessage.venture_id, ChatMessage.workspace_id,
            ChatMessage.conversation_id, ChatMessage.author_person_id, ChatMessage.role, ChatMessage.body_md)
            .where(ChatMessage.id == source_message_id)).one_or_none() if source_message_id else None
        saved_conversation = session.execute(select(ChatConversation.venture_id, ChatConversation.workspace_id,
            ChatConversation.creator_person_id, ChatConversation.visibility)
            .where(ChatConversation.id == conversation.id)).one_or_none() if conversation else None
    if (not scope or not conversation or not venture or not workspace or not source or not saved_conversation
            or workspace.owner_id != actor or workspace.archived_at or venture.archived_at
            or scope.workspace_id != workspace.id or scope.venture_id not in {"", venture_id}
            or (conversation.venture_id, conversation.workspace_id, conversation.actor_id, conversation.visibility)
            != (venture_id, workspace.id, actor, "private")
            or (saved_conversation.venture_id, saved_conversation.workspace_id, saved_conversation.creator_person_id, saved_conversation.visibility)
            != (venture_id, workspace.id, actor, "private")
            or (source.venture_id, source.workspace_id, source.conversation_id, source.author_person_id, source.role, source.body_md)
            != (venture_id, workspace.id, conversation.id, actor, "user", text)
            or not service._active_custody(session, workspace.id, venture_id, actor)):
        raise PermissionError("task_chat_source_invalid")
    person = session.get(Person, actor)
    if person is None:
        raise PermissionError("task_chat_source_invalid")
    if new_work:
        settings_raw = session.scalar(select(Workspace.settings_json).where(Workspace.id == workspace.id))
        settings = json.loads(settings_raw or "{}")
        pause = settings.get("venture_pause")
        if isinstance(pause, dict) and pause.get("paused"):
            raise TaskPaused("venture_paused")
    identity = [workspace.id, venture_id, actor, conversation.id, source.id]
    key = "task-chat:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return workspace.id, actor, person.timezone, key


def _deadline(value, zone):
    return "being checked" if not value else "by " + as_utc(value).astimezone(ZoneInfo(zone)).strftime("%a %d %b %Y %H:%M %Z")


def _summary(session, task, zone, *, replay=False):
    view = service.view(session, task, timeline=False)
    planning = service.planning_pending(task)
    states = {"submitted": "Waiting for its next scheduled run" if task.kind == "scheduled" else "Waiting for its trigger event" if task.kind == "event" else "Waiting to start",
        "working": "Working", "input_required": "Needs your input", "auth_required": "Needs a connection",
        "approval_required": "Needs your approval", "paused": "Paused", "completed": "Completed", "failed": "Needs attention",
        "canceled": "Stopped", "rejected": "Cannot continue"}
    deadline = as_utc(datetime.fromisoformat(view["deadline_at"])) if view["deadline_at"] and not planning else None
    lines = ["I've already saved your task." if replay else "Your task is saved.", f"**{task.title}**", task.goal,
             f"Status: {'Plan awaiting confirmation' if planning else states.get(task.state, 'Checking saved progress')}.", f"{'Deadline estimate' if task.deadline_is_estimate else 'Deadline'}: {_deadline(deadline, zone)}."]
    if len(task.goal) > 1000:
        lines[2] = task.goal[:1000] + "… Open Tasks for the complete goal."
    if view["blocker"]:
        lines += [view["blocker"]["reason"], view["blocker"]["unblock_action"]]
    return "\n\n".join(lines)


def _skills(task) -> set[str]:
    try:
        steps = json.loads(task.scope_json or "{}").get("steps") or []
    except (TypeError, ValueError, AttributeError):
        return set()
    return {str(step.get("skill")) for step in steps if isinstance(step, dict)}


def stalled_work(venture_id: str) -> tuple[list[str], list[dict]]:
    """Plain words plus in-chat actions for the latest research and prospect tasks; never a page link."""
    lines: list[str] = []
    actions: list[dict] = []
    try:
        with db()() as session:
            rows = session.scalars(select(Task).where(Task.venture_id == venture_id)
                                   .order_by(Task.updated_at.desc(), Task.id.desc()).limit(30)).all()
            research = next((task for task in rows if "research.deep" in _skills(task)), None)
            prospects = next((task for task in rows if "gtm.journey.prepare" in _skills(task)), None)
            blocker = json.loads(prospects.blocker_json or "{}") if prospects is not None else {}
    except Exception:  # noqa: BLE001 - a status answer never fails on this extra note
        return [], []
    if research is not None and research.state == "working":
        lines.append("**Research your idea** is running. I post what it finds here when it finishes; nothing is needed from you.")
    elif research is not None and research.state in {"input_required", "failed", "canceled"}:
        lines.append("**Research your idea** stopped before it finished. Any sources it already saved are kept. "
                     "You can run it again, or skip research and move on to finding customers.")
        actions += [{"label": "Run research again", "action": "chat.reply", "text": "Run research again", "style": "primary"},
                    {"label": "Skip research", "action": "chat.reply", "text": "Skip research", "style": "ghost"}]
    if (prospects is not None and prospects.state == "input_required"
            and "audience" in str((blocker or {}).get("reason") or "")):
        lines.append("Finding prospects is waiting for a clearer audience. I can suggest three customer groups to pick from.")
        actions.append({"label": "Suggest who to reach", "action": "chat.reply", "text": "Who should I reach out to?", "style": "primary"})
    return lines, actions


def reply_authority(venture_id: str, text: str, *, source_message_id: str) -> TaskReply:
    """Capture the server write fence without planning or regenerating a reply."""
    from app.authz.workspace import subject_person_id
    from fastapi import HTTPException
    captured = current_conversation()
    scope = current_or_none()
    actor = subject_person_id(scope.actor) if scope else None
    destination = (captured.id, captured.venture_id, captured.workspace_id, captured.actor_id, captured.visibility) if captured else None

    def output_authority(session, resolved_scope):
        resolved = tuple(getattr(resolved_scope, key, None) for key in ("id", "venture_id", "workspace_id", "actor_id", "visibility"))
        if destination is None or destination != resolved or destination[-1] != "private":
            raise HTTPException(404, detail={"error": {"code": "chat_request_scope_unavailable"}})
        try:
            workspace_id, live_actor, _, _ = _source(session, venture_id, text, source_message_id)
            if (workspace_id, live_actor) != (destination[2], actor):
                raise PermissionError("task_chat_source_invalid")
        except (PermissionError, ValueError):
            raise HTTPException(404, detail={"error": {"code": "chat_request_scope_unavailable"}}) from None

    key = "task-reply:" + hashlib.sha256(json.dumps([destination, venture_id, actor, source_message_id], separators=(",", ":")).encode()).hexdigest()
    return TaskReply({}, assistant_key=key, output_authority=output_authority)


async def handle_message(venture_id: str, text: str, *, source_message_id: str) -> dict | None:
    """A handled reply is persisted only through its server-only write fence."""
    if not recognizes(text):
        return None
    reply = reply_authority(venture_id, text, source_message_id=source_message_id)
    result = await _handle_message(venture_id, text, source_message_id=source_message_id)
    if result is None:
        return None
    reply.update(result)
    return reply


async def _handle_message(venture_id: str, text: str, *, source_message_id: str) -> dict | None:
    """Caller saves the exact user row once, then persists the returned reply."""
    if not recognizes(text):
        return None
    raw = text.strip()
    try:
        with db()() as session:
            workspace_id, actor, saved_zone, key = _source(session, venture_id, text, source_message_id)
            rows = session.scalars(select(Task).where(Task.workspace_id == workspace_id, Task.venture_id == venture_id,
                Task.owner_person_id == actor, Task.state.not_in(service.TERMINAL))
                .order_by(Task.updated_at.desc(), Task.id.desc())).all()
            try:
                zone, confirmed = timezone_name(saved_zone), bool(saved_zone)
            except ValueError:
                zone, confirmed = "UTC", False
            if _STATUS.fullmatch(raw):
                active = [task for task in rows if task.state not in service.TERMINAL]
                parts = []
                for task in active[:10]:
                    summary = _summary(session, task, zone)
                    if len(("\n\n".join(parts) + summary).encode()) > 12_000:
                        break
                    parts.append(summary)
                body = "\n\n".join(parts) if active else "I have no active saved tasks. Saved results remain available in Tasks."
                if len(active) > len(parts):
                    body += "\n\nOpen Tasks for the rest of your saved tasks."
                notes, chips = stalled_work(venture_id)
                reply = _reply(venture_id, "\n\n".join([body, *notes]))
                reply["actions"] = chips + reply["actions"]
                return reply
            if stop := _STOP.fullmatch(raw):
                serialize_writes(session, key=key)
                receipt = session.scalar(select(TaskEvent).join(Task, Task.id == TaskEvent.task_id).where(
                    Task.workspace_id == workspace_id, Task.venture_id == venture_id, Task.owner_person_id == actor,
                    TaskEvent.kind == "chat_stop", TaskEvent.data_json.contains(json.dumps({"source_key": key})[1:-1], autoescape=True)).limit(1))
                if receipt:
                    return _reply(venture_id, "That stop is already saved. Its results remain available.")
                title = stop[1].strip().strip('"').strip("'").rstrip(".! ")
                matches = [task for task in rows if task.state not in service.TERMINAL and task.title.strip().casefold().rstrip(".! ") == title.casefold()]
                if len(matches) != 1:
                    return _reply(venture_id, "Which saved task should I stop? Open Tasks and use Stop on the matching full title, goal and schedule.", kind="needs_you")
                task = matches[0]
                service.cancel(session, task)
                service.event(session, task, "chat_stop", "You stopped this task from your private conversation. Saved results remain available.", data={"source_key": key})
                session.commit()
                return _reply(venture_id, f"Stopped **{task.title}**. Saved results remain available. Calls already in progress may finish.")
            existing = session.scalar(select(Task).where(Task.create_key == key))
            if existing:
                if (existing.workspace_id, existing.venture_id, existing.owner_person_id) != (workspace_id, venture_id, actor):
                    raise PermissionError("task_chat_source_invalid")
                if intake.KEY not in json.loads(existing.scope_json):
                    return _reply(venture_id, _summary(session, existing, zone, replay=True), now=existing.state == "working", task_id=existing.id)
        goal = re.sub(r"^(?:please\s+)?do\s+", "", raw, flags=re.I).strip()
        from app.secrets.detect import looks_like_secret
        if looks_like_secret(goal):
            return _reply(venture_id, "Keep credentials out of task text. Use the secure fields on Connections.", kind="needs_you")
        kind, _ = trigger_from_words(goal, zone, utcnow())
        if kind == "scheduled" and not confirmed and existing is None:
            return _reply(venture_id, "Which timezone should I use for this schedule? Confirm your timezone in Settings, then send this request again. UTC is the unconfirmed default.", kind="needs_you")
        with db()() as session:
            if _source(session, venture_id, text, source_message_id, new_work=True)[:2] != (workspace_id, actor):
                raise PermissionError("task_chat_source_invalid")
            if existing is not None:
                zone = json.loads(existing.scope_json)[intake.KEY]["accepted_zone"]
            task, should_plan = intake.begin(session, workspace_id=workspace_id, venture_id=venture_id,
                owner_person_id=actor, create_key=key, body_input={"goal": goal, "timezone": zone, "start": False}, accepted_zone=zone)
            journal = json.loads(task.scope_json)[intake.KEY]
            task_id, request_id = task.id, journal["request_id"]
            session.commit()  # Private source-bound intent precedes the one paid call.
        if should_plan:
            def save_response(contract, usage):
                with db()() as session:
                    intake.respond(session, task_id=task_id, workspace_id=workspace_id, venture_id=venture_id,
                        owner_person_id=actor, request_id=request_id, contract=contract, usage=usage)
                    session.commit()
            try:
                with timezone_context(journal["accepted_zone"]):
                    await planner.preview(goal, timezone=journal["accepted_zone"],
                        now=datetime.fromisoformat(journal["accepted_at"]), workspace_id=workspace_id,
                        venture_id=venture_id, request_id=request_id, before_meter_exit=save_response)
            except Exception:
                with db()() as session:
                    intake.fail(session, task_id=task_id, workspace_id=workspace_id, venture_id=venture_id,
                        owner_person_id=actor, request_id=request_id, uncertain=True)
                    session.commit()
        from app.credits.errors import CreditError
        confirmed_billing = None
        try:
            confirmed_billing = intake.reconcile(task_id=task_id, workspace_id=workspace_id,
                venture_id=venture_id, owner_person_id=actor)
        except (CreditError, ValueError):
            pass
        with db()() as session:
            from app.tasks.locks import lock_task
            lock_task(session, task_id)
            if _source(session, venture_id, text, source_message_id, new_work=True)[:2] != (workspace_id, actor):
                raise PermissionError("task_chat_source_invalid")
            task = intake.recover(session, task_id=task_id, workspace_id=workspace_id,
                venture_id=venture_id, owner_person_id=actor, confirmed_billing=confirmed_billing)
            journal = json.loads(task.scope_json)[intake.KEY]
            zone = journal["accepted_zone"]
            if journal["state"] != "complete":
                body = _summary(session, task, zone, replay=not should_plan)
                body += "\n\nThe saved plan has not been confirmed. No work has started, and its earlier planner call will not be repeated automatically."
                session.commit()
                return _reply(venture_id, body, kind="needs_you")
            # Complete replays also prove the saved contract before admitting a run.
            contract = Contract.model_validate(journal["contract"])
            intake.respond(session, task_id=task_id, workspace_id=workspace_id, venture_id=venture_id,
                owner_person_id=actor, request_id=request_id, contract=contract)
            from app.tasks.skills import get_skill
            work_goal = planner.work_goal(goal)
            consequential = (requires_reviewed_action(work_goal) or bool(re.search(r"(?i)^(?:email|message|contact|call)\b", work_goal))
                or any(get_skill(step.skill).consequential for step in contract.steps))
            from app.tasks import goals
            per_step_review = all(step.subgoal for step in contract.steps) and goals.enabled()
            if consequential and not per_step_review and task.state not in service.TERMINAL | service.WAITING:
                skill = next((step.skill for step in contract.steps if get_skill(step.skill).consequential), None)
                service.block(session, task, consequential_blocker(skill))
            elif (not consequential or per_step_review) and task.kind == "one_time" and task.state == "submitted":
                service.start_run(session, task, "initial")
            body = _summary(session, task, zone, replay=not should_plan)
            body += "\n\nPlan:\n" + "\n".join(f"{index}. {step.title}" for index, step in enumerate(contract.steps, 1))
            body += "\n\nNothing will be sent, posted, bought or deleted without your approval." if consequential and not is_local() else f'\n\nSay "stop task {task.title}" to stop safely, or use Stop in Now. Calls already in progress may finish.'
            if per_step_review and consequential and not is_local():
                body += f'\n\nSay "stop task {task.title}" to stop safely, or use Stop in Now.'
            if not confirmed:
                body += "\n\nTimes use UTC until you confirm your timezone in Settings."
            session.commit()
            return _reply(venture_id, body, now=task.state == "working" and not service.planning_pending(task), task_id=task.id)
    except TaskPaused:
        from app.chat.guard import paused_reply
        return paused_reply(venture_id)
    except PermissionError:
        return _reply(venture_id, "I could not verify this instruction in your current private conversation. Reopen your private venture conversation and try again.", kind="needs_you")
    except ValueError as error:
        questions = {"What exact frequency or event should start this task?": "What exact frequency or event should start this task?",
            "That time has passed. What future date and time should we use?": "That time has passed. What future date and time should we use?"}
        return _reply(venture_id, questions.get(str(error), "The task plan could not be confirmed. What exact result should this task produce?"), kind="needs_you")
