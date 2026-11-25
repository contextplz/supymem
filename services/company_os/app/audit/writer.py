"""Append-only audit rows that answer five questions (42 §5).

Who, on whose authority, what, to what, and what happened. If a call site
cannot supply all five, the call site is wrong.
"""

from __future__ import annotations

import contextvars
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Iterator

from app.db.models import EventLog
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

ACTOR_KINDS = frozenset({"person", "agent", "token", "system"})
SURFACES = frozenset({"ui", "api", "mcp", "cli", "sdk", "widget", "webhook", "internal"})


@dataclass(frozen=True)
class AuditActor:
    actor_id: str
    actor_kind: str = "person"
    # The person whose grant authorized an agent or token action. "The system
    # did it" is not an acceptable answer to a founder (42 §1).
    on_behalf_of: str | None = None
    surface: str = "internal"

    def __post_init__(self) -> None:
        if self.actor_kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor_kind: {self.actor_kind}")


_SYSTEM = AuditActor(actor_id="system", actor_kind="system", surface="internal")
_CURRENT: contextvars.ContextVar[AuditActor] = contextvars.ContextVar(
    "audit_actor", default=_SYSTEM
)


def current_actor() -> AuditActor:
    return _CURRENT.get()


@contextmanager
def use_actor(actor: AuditActor) -> Iterator[AuditActor]:
    """Bind the acting identity for everything inside the block."""
    token = _CURRENT.set(actor)
    try:
        yield actor
    finally:
        _CURRENT.reset(token)


def audit(
    topic: str,
    *,
    workspace_id: str | None = None,
    venture_id: str | None = None,
    actor_id: str | None = None,
    actor_kind: str | None = None,
    on_behalf_of: str | None = None,
    surface: str | None = None,
    target_ref: str | None = None,
    outcome: str = "ok",
    denied_reason: str | None = None,
    approval_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Write one audit row. Never raises: audit failure must not break the action."""
    actor = current_actor()
    if actor_id or actor_kind:
        actor = replace(
            actor,
            actor_id=actor_id or actor.actor_id,
            actor_kind=actor_kind or actor.actor_kind,
        )
    resolved_surface = surface or actor.surface
    if resolved_surface not in SURFACES:
        resolved_surface = "internal"

    row_id = new_id("evt")
    try:
        factory = db()
        with factory() as session:
            session.add(
                EventLog(
                    id=row_id,
                    topic=topic,
                    venture_id=venture_id,
                    workspace_id=workspace_id,
                    actor=actor.actor_id,
                    actor_id=actor.actor_id,
                    actor_kind=actor.actor_kind,
                    on_behalf_of=on_behalf_of or actor.on_behalf_of,
                    surface=resolved_surface,
                    approval_id=approval_id,
                    target_ref=target_ref,
                    outcome=outcome,
                    denied_reason=denied_reason,
                    payload_json=json.dumps(payload or {}, default=str),
                    ts=utcnow(),
                )
            )
            session.commit()
        return row_id
    except Exception:  # noqa: BLE001 — an audit write must never break the action
        log.exception("audit write failed topic=%s", topic)
        return None
