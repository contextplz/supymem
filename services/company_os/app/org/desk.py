"""Per-agent work brief: what they did, what they are doing, what is next.

Built from assigned tasks, live heartbeat, today's activity lines, and chat
stamps. We do not invent work — unattributed activity stays off the desk.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db.models import ActivityLine, PlannedTask
from app.org.characters import canonical_role, decode_assignee, parse_speaker
from core.clock import utcnow

_WATCHING = frozenset({"quarantined", "shadow"})
_KIND_ROLES = {
    "research": "researcher",
    "validate": "researcher",
    "demand": "researcher",
    "build": "builder",
    "code": "builder",
    "scaffold": "builder",
    "firstlook": "designer",
    "copy": "copywriter",
    "gtm": "growth",
    "plan": "orchestrator",
    "chat": "orchestrator",
}


def _clip(text: str | None, limit: int = 160) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + "…"


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _start_of_day(now: datetime) -> datetime:
    local = _aware(now) or utcnow()
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _agent_keys(agent: dict[str, Any]) -> set[str]:
    keys = {
        str(agent.get("id") or "").strip().lower(),
        str(agent.get("name") or "").strip().lower(),
        canonical_role(str(agent.get("role") or "")),
    }
    return {k for k in keys if k}


def _task_owner(row: PlannedTask) -> dict[str, str]:
    try:
        tags = json.loads(row.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    return decode_assignee(tags)


def _owns(agent: dict[str, Any], owner: dict[str, str]) -> bool:
    keys = _agent_keys(agent)
    aid = str(owner.get("owner_id") or "").strip().lower()
    name = str(owner.get("owner_name") or "").strip().lower()
    role = canonical_role(str(owner.get("owner_role") or ""))
    if aid and aid in keys:
        return True
    if name and name in keys:
        return True
    if role and role in keys and not aid and not name:
        return True
    return False


def _mentioned(agent: dict[str, Any], text: str) -> bool:
    name = str(agent.get("name") or "").strip()
    if not name or len(name) < 2:
        return False
    return re.search(rf"\b{re.escape(name)}\b", text or "", re.I) is not None


def _role_for_kind(kind: str, phase: str) -> str:
    blob = f"{kind} {phase}".lower()
    for needle, role in _KIND_ROLES.items():
        if needle in blob:
            return role
    return ""


def _idle_doing(agent: dict[str, Any]) -> str:
    status = str(agent.get("status") or "").lower()
    charge = _clip(str(agent.get("charge") or ""), 140)
    if status in _WATCHING:
        return "Watching from shadow, not taking work until activated."
    if status == "paused":
        return "Paused. Waiting for you."
    if status == "retired":
        return "Retired from this venture."
    if charge:
        return charge
    return "Waiting for the next assignment."


def _load_tasks(venture_id: str) -> list[PlannedTask]:
    from app.state import db

    factory = db()
    with factory() as session:
        return list(
            session.scalars(
                select(PlannedTask)
                .where(
                    PlannedTask.venture_id == venture_id,
                    PlannedTask.status.in_(("open", "running", "done")),
                )
                .order_by(PlannedTask.rank.asc(), PlannedTask.updated_at.desc())
            ).all()
        )


def _load_activity(venture_id: str, since: datetime) -> list[ActivityLine]:
    from app.state import db

    factory = db()
    with factory() as session:
        return list(
            session.scalars(
                select(ActivityLine)
                .where(ActivityLine.venture_id == venture_id, ActivityLine.ts >= since)
                .order_by(ActivityLine.seq.desc())
                .limit(200)
            ).all()
        )


# Finished venture jobs that no task or activity line records, credited to the role that does that work.
_JOB_DID = {
    "idea.validate": ("researcher", "Validated the idea against the market."),
    "research.deep": ("researcher", "Ran market research."),
}


def _load_finished_jobs(venture_id: str, since: datetime) -> list[tuple[str, str]]:
    from app.db.models import Job
    from app.state import db

    with db()() as session:
        rows = session.scalars(
            select(Job).where(Job.venture_id == venture_id, Job.kind.in_(tuple(_JOB_DID)), Job.status == "done",
                              Job.finished_at >= since).order_by(Job.finished_at.desc())
        ).all()
    return [_JOB_DID[row.kind] for row in rows]


def _load_chat(venture_id: str, since: datetime) -> list[dict[str, Any]]:
    from app.chat.store import _ser
    from app.db.models import ChatMessage
    from app.state import db

    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.venture_id == venture_id, ChatMessage.created_at >= since)
                .order_by(ChatMessage.created_at.asc())
                .limit(80)
            ).all()
        )
        return [_ser(r) for r in rows]


def _live_map(venture_id: str) -> dict[str, dict[str, Any]]:
    try:
        from app.org.heartbeat import list_live_status

        return {str(row.get("agent_id")): row for row in list_live_status(venture_id)}
    except Exception:
        return {}


def _activity_for_agent(
    agent: dict[str, Any],
    lines: list[ActivityLine],
    agents: list[dict[str, Any]],
) -> list[ActivityLine]:
    role = canonical_role(str(agent.get("role") or ""))
    same_role = [
        a for a in agents if canonical_role(str(a.get("role") or "")) == role
    ]
    unique_role = len(same_role) == 1
    hits: list[ActivityLine] = []
    for line in lines:
        text = line.text or ""
        detail_id = ""
        if line.detail_json:
            try:
                detail = json.loads(line.detail_json)
                detail_id = str((detail or {}).get("agent_id") or "")
            except json.JSONDecodeError:
                detail_id = ""
        if detail_id and detail_id == str(agent.get("id") or ""):
            hits.append(line)
            continue
        if _mentioned(agent, text):
            hits.append(line)
            continue
        mapped = _role_for_kind(line.kind or "", line.phase or "")
        if unique_role and mapped and mapped == role:
            hits.append(line)
    return hits


def work_briefs(
    venture_id: str,
    agents: list[dict[str, Any]] | None,
    *,
    include_log: bool = False,
) -> dict[str, dict[str, Any]]:
    rows = [a for a in (agents or []) if isinstance(a, dict) and a.get("id")]
    if not rows:
        return {}
    now = utcnow()
    since = _start_of_day(now)
    tasks = _load_tasks(venture_id)
    activity = _load_activity(venture_id, since)
    chat = _load_chat(venture_id, since)
    live = _live_map(venture_id)
    jobs = _load_finished_jobs(venture_id, since)
    briefs: dict[str, dict[str, Any]] = {}
    for agent in rows:
        briefs[str(agent["id"])] = _brief_for(
            agent,
            agents=rows,
            tasks=tasks,
            activity=activity,
            chat=chat,
            live=live,
            include_log=include_log,
            since=since,
            jobs=jobs,
        )
    return briefs


def attach_work_briefs(venture_id: str, org: dict[str, Any]) -> dict[str, Any]:
    agents = [a for a in (org.get("agents") or []) if isinstance(a, dict)]
    briefs = work_briefs(venture_id, agents)
    attached = []
    for agent in agents:
        aid = str(agent.get("id") or "")
        attached.append({**agent, "work": briefs.get(aid) or _brief_for(agent, agents=agents, tasks=[], activity=[], chat=[], live={}, include_log=False, since=_start_of_day(utcnow()), jobs=_load_finished_jobs(venture_id, _start_of_day(utcnow())))})
    return {**org, "agents": attached}


def agent_desk(
    venture_id: str,
    agent_id: str,
    agents: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    rows = [a for a in (agents or []) if isinstance(a, dict)]
    agent = next((a for a in rows if str(a.get("id") or "") == agent_id), None)
    if agent is None:
        return None
    brief = work_briefs(venture_id, rows, include_log=True).get(agent_id)
    return {
        "agent": {**agent, "work": brief},
        "work": brief,
    }


def _brief_for(
    agent: dict[str, Any],
    *,
    agents: list[dict[str, Any]],
    tasks: list[PlannedTask],
    activity: list[ActivityLine],
    chat: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    include_log: bool,
    since: datetime,
    jobs: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    aid = str(agent.get("id") or "")
    mine = [t for t in tasks if _owns(agent, _task_owner(t))]
    done = [t for t in mine if t.status == "done"]
    running = [t for t in mine if t.status == "running"]
    open_tasks = [t for t in mine if t.status == "open"]
    if not open_tasks:
        # Orchestrator may not have stamped an assignee yet — pick by role.
        from app.org.characters import owner_for_work

        for task in tasks:
            if task.status != "open":
                continue
            owner = _task_owner(task)
            if owner.get("owner_id") or owner.get("owner_name"):
                continue
            tags = []
            try:
                tags = json.loads(task.tags_json or "[]")
            except json.JSONDecodeError:
                tags = []
            pick = owner_for_work(task.title, tags, agents)
            if pick and str(pick.get("id") or "") == aid:
                open_tasks.append(task)

    lines = _activity_for_agent(agent, activity, agents)
    spoken = []
    for msg in chat:
        speaker, rest = parse_speaker(msg.get("body_md"))
        if not speaker:
            continue
        if speaker.get("id") == aid or (
            speaker.get("name")
            and speaker["name"].lower() == str(agent.get("name") or "").lower()
        ):
            spoken.append({**msg, "text": rest.strip() or msg.get("body_md")})

    did = None
    if done:
        latest = max(done, key=lambda t: _aware(t.finished_at or t.updated_at) or since)
        did = f"Finished {latest.title}."
    elif spoken:
        did = _clip(spoken[-1].get("text"), 160)
    elif lines:
        finished = [ln for ln in lines if str(ln.status or "").lower() in {"done", "ok", "success"}]
        pick = finished[0] if finished else lines[0]
        if str(pick.status or "").lower() in {"done", "ok", "success"}:
            did = _clip(pick.text, 160)

    if not did:
        role = canonical_role(str(agent.get("role") or ""))
        did = next((text for job_role, text in (jobs or []) if job_role == role), None)

    live_row = live.get(aid) or {}
    live_state = str(live_row.get("state") or "")
    live_task = str(live_row.get("current_task_desc") or "").strip()
    doing = None
    if live_state in {"working", "blocked"} and live_task:
        doing = live_task if live_state == "working" else f"Stuck: {live_task}"
    elif running:
        doing = f"Working on {running[0].title}."
    elif live_state == "working" and live_task:
        doing = live_task
    if not doing:
        doing = _idle_doing(agent)

    nxt = None
    next_task_id = None
    if open_tasks:
        nxt = open_tasks[0].title
        next_task_id = open_tasks[0].id
    elif running and len(mine) > 1:
        follow = [t for t in mine if t.status == "open"]
        if follow:
            nxt = follow[0].title
            next_task_id = follow[0].id

    log: list[dict[str, Any]] = []
    if include_log:
        for t in mine:
            ts = _aware(t.finished_at or t.updated_at)
            if ts and ts >= since:
                log.append(
                    {
                        "at": ts.isoformat(),
                        "kind": "task",
                        "status": t.status,
                        "text": t.title,
                    }
                )
        for ln in lines:
            ts = _aware(ln.ts)
            log.append(
                {
                    "at": ts.isoformat() if ts else None,
                    "kind": "activity",
                    "status": ln.status,
                    "text": ln.text,
                    "phase": ln.phase,
                }
            )
        for msg in spoken:
            log.append(
                {
                    "at": msg.get("created_at"),
                    "kind": "chat",
                    "status": msg.get("kind") or "text",
                    "text": _clip(str(msg.get("text") or ""), 200),
                }
            )
        log.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
        log = log[:40]

    return {
        "did": did or "Nothing logged for them yet today.",
        "doing": doing,
        "next": nxt or "No queued task yet, the orchestrator may assign one.",
        "next_task_id": next_task_id,
        "next_provisional": True,
        "live_state": live_state or None,
        "today": log,
    }
