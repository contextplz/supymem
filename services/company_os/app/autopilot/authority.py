"""Fresh, persisted founder authority for scheduler and approved-card effects.

A scheduler has no HTTP tenancy context. Resolve the owner from the database,
validate current membership, then bind the same scope job admission consumes.
Never infer identity from an action/model payload or relax worker validation.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from app.authz.workspace import assert_venture_access, subject_person_id
from app.db.models import Person, Venture, Workspace
from app.state import db
from app.tenancy import Scope, bind, current_or_none
from app.worker.handlers import hosted_mode
from app.workspaces.service import role_of


@contextmanager
def founder_scope(venture_id: str, actor: str | None = None, *, autonomous: bool = False) -> Iterator[Scope]:
    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, venture.workspace_id) if venture and venture.workspace_id else None
        if workspace is None or workspace.archived_at is not None:
            raise PermissionError("autopilot_authority_missing")
        wid, owner = workspace.id, workspace.owner_id
        subject = owner if autonomous else actor
        if hosted_mode() and (not subject or subject in {"system", "anonymous", "local", "founder"}):
            raise PermissionError("autopilot_actor_missing")
        person = subject_person_id(subject)
        subject = subject or person
        if session.get(Person, person) is None:
            raise PermissionError("autopilot_actor_missing")
    existing = current_or_none()
    from app.core.scheduled import FOUNDER_JOB
    global_scheduler = autonomous and (existing == Scope("none", "", "system") or (
        FOUNDER_JOB.get() and existing is not None and subject_person_id(existing.actor) == person))
    if existing and not global_scheduler and (existing.workspace_id != wid or existing.venture_id not in {"", venture_id}
                     or subject_person_id(existing.actor) != person):
        raise PermissionError("autopilot_scope_mismatch")
    # Ownership alone is insufficient: the worker requires a current write role.
    if role_of(wid, person) not in {"owner", "admin", "maintainer", "contributor"}:
        raise PermissionError("autopilot_authority_revoked")
    if assert_venture_access(venture_id, subject)[1] != wid:
        raise PermissionError("autopilot_scope_mismatch")
    with bind(workspace_id=wid, venture_id=venture_id, actor=subject) as scope:
        yield scope
