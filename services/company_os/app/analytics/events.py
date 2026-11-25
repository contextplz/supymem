"""Append-only product events. Failures never break the caller."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.analytics.funnel import FUNNEL_EVENT_NAMES
from app.core.local_mode import is_local
from app.db.models import AnalyticsEvent
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger("app.analytics")

ALLOWED = frozenset(
    {
        "signup",
        "venture.created",
        "chat.message",
        "voice.session_started",
        "entry.chat_confirmed",
        "chat.tool_invoked",
        "chat.reply_error",
        "harness.research",
        "credits.topup",
        "coding.session",
        "billing.started",
        "billing.canceled",
        "invite.accepted",
        "session.active",
        "idea.confirmed",
        "research.completed",
        "plan.approved",
        "build.succeeded",
        "site.published",
        "ui.click",
        "ui.screen_view",
        "ui.error",
        "feature.used",
        "request.received",
        "request.classified",
        "request.outcome",
        "workflow.started",
        "workflow.completed",
        "idea.started",
        "idea.abandoned",
        "plan.compiled",
        "plan.replanned",
        "build.started",
        "build.finished",
        "qa.finished",
        "deploy.finished",
        "app.live",
        "app.slept",
        "app.scaled",
        "credits.exhausted",
        "credits.grace_started",
        "plan.subscribed",
        "plan.churned",
        "venture.first_visitor",
        "venture.first_signup",
        "venture.first_payment",
        "venture.paused",
        "venture.archived",
        "guard.flag",
        "security.event",
        "agent.run_finished",
        "journey.step_entered",
        "journey.step_completed",
    }
    | FUNNEL_EVENT_NAMES
)

_PREFIXES = (
    "ui.",
    "feature.",
    "request.",
    "workflow.",
    "idea.",
    "plan.",
    "build.",
    "deploy.",
    "credits.",
    "outcome.",
    "guard.",
    "security.",
    "agent.",
    "job.",
    "model.",
    "server.",
    "recovery.",
    "hosting.",
    "email.",
    "needs_you.",
    "god_mode.",
)


def _allowed(name: str) -> bool:
    if name in ALLOWED:
        return True
    return any(name.startswith(p) for p in _PREFIXES)


def emit(
    event_name: str,
    *,
    person_id: str | None = None,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    props: dict[str, Any] | None = None,
) -> str | None:
    """Write one analytics row. Never raises — product paths must stay up."""
    if is_local():
        return None
    name = (event_name or "").strip()
    if not _allowed(name):
        log.warning("rejected analytics event %r", event_name)
        return None
    try:
        factory = db()
        with factory() as session:
            row = AnalyticsEvent(
                id=new_id("ane"),
                person_id=person_id,
                workspace_id=workspace_id,
                venture_id=venture_id,
                event_name=name,
                props_json=json.dumps(props or {}, sort_keys=True),
                occurred_at=utcnow(),
            )
            session.add(row)
            session.commit()
            return row.id
    except Exception:
        log.exception("analytics emit failed for %s", name)
        return None


def emit_once(
    event_name: str,
    *,
    event_key: str,
    person_id: str | None = None,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    props: dict[str, Any] | None = None,
) -> str | None:
    """Record a closed event once for an already durable workflow identity.

    A lost response may retry the workflow; it must not double-count a chat
    request or create a second response-time pair for the same saved review.
    """
    if is_local():
        return None
    name = (event_name or "").strip()
    if not _allowed(name) or not re.fullmatch(r"[a-z0-9_:-]{1,160}", event_key or ""):
        return None
    body = json.dumps(props or {}, sort_keys=True)
    identity = json.dumps(["analytics-once-v1", name, event_key, person_id,
                           workspace_id, venture_id], separators=(",", ":"))
    event_id = "ane_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    try:
        factory = db()
        with factory() as session:
            existing = session.get(AnalyticsEvent, event_id)
            if existing is not None:
                if (existing.person_id, existing.workspace_id, existing.venture_id,
                        existing.event_name) != (
                        person_id, workspace_id, venture_id, name):
                    log.error("analytics once identity conflict for %s", name)
                    return None
                # The first saved closed classification is canonical. A retry
                # after a classifier release must not strand its outcome pair.
                return event_id
            session.add(AnalyticsEvent(id=event_id, person_id=person_id,
                workspace_id=workspace_id, venture_id=venture_id,
                event_name=name, props_json=body, occurred_at=utcnow()))
            try:
                session.commit()
                return event_id
            except IntegrityError:
                session.rollback()
                existing = session.get(AnalyticsEvent, event_id)
                if existing is not None and (existing.person_id, existing.workspace_id,
                        existing.venture_id, existing.event_name) == (
                        person_id, workspace_id, venture_id, name):
                    return event_id
                log.error("analytics once identity conflict for %s", name)
                return None
    except Exception:
        log.exception("analytics emit-once failed for %s", name)
        return None


def emit_in_session(
    session: Any,
    event_name: str,
    *,
    event_key: str,
    person_id: str | None = None,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    props: dict[str, Any] | None = None,
    flush: bool = True,
) -> None:
    """Stage a closed event in the caller's transaction, once per ``event_key``.

    ``flush=False`` stages without touching the database's write path, for callers
    (FSM transitions) that may run before a later ``serialize_writes`` lock.

    For paths that already hold an open session (job failure, build finish): the
    event commits or rolls back with the state change it describes, and a retry
    of the same workflow step cannot double count. Never raises.
    """
    if is_local():
        return
    name = (event_name or "").strip()
    event_key = (event_key or "").lower()
    if not _allowed(name) or not re.fullmatch(r"[a-z0-9_:.-]{1,160}", event_key):
        return
    identity = json.dumps(["analytics-once-v1", name, event_key, person_id, workspace_id, venture_id],
                          separators=(",", ":"))
    event_id = "ane_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    try:
        with session.no_autoflush:
            missing = session.get(AnalyticsEvent, event_id) is None and not any(
                isinstance(row, AnalyticsEvent) and row.id == event_id for row in session.new)
        if missing:
            session.add(AnalyticsEvent(id=event_id, person_id=person_id, workspace_id=workspace_id,
                venture_id=venture_id, event_name=name, props_json=json.dumps(props or {}, sort_keys=True),
                occurred_at=utcnow()))
            if flush:
                session.flush()  # so a second stage of the same key in this transaction sees it
    except Exception:
        log.exception("analytics stage failed for %s", name)
