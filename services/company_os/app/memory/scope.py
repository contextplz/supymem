"""Bind DB-owned tenant identity for trusted internal memory consumers.

This helper is not HTTP authorization. Requests and workers already authorize
their entrypoints; a supplied workspace never overrides an existing scope.
"""
from contextlib import contextmanager


@contextmanager
def bound_memory_scope(venture_id: str, *, workspace_id: str | None = None):
    from app.db.session import resolve_database_url
    from app.tenancy import bind, current_or_none

    scope = current_or_none()
    if not (resolve_database_url() or "").startswith(("postgres:", "postgresql:", "postgresql+")):
        yield scope
        return
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if (not venture or not venture.workspace_id or workspace_id and venture.workspace_id != workspace_id
                or scope and scope.venture_id and scope.venture_id != venture_id):
            raise PermissionError("memory_consumer_scope_mismatch")
        wid = venture.workspace_id
    unscoped_system = scope and scope.actor == "system" and scope.workspace_id == "none" and not scope.venture_id
    if scope and scope.workspace_id != wid and not unscoped_system:
        # Portfolio reads have a verified actor but no selected workspace. Each
        # projected source row must still be inside that actor's membership.
        from app.authz.workspace import accessible_workspace_ids
        if (scope.workspace_id != "none" or scope.venture_id
                or scope.actor in {"anonymous", "system", "local", "founder"}
                or wid not in accessible_workspace_ids(scope.actor)):
            raise PermissionError("memory_consumer_scope_mismatch")
    with bind(workspace_id=wid, venture_id=venture_id, actor=scope.actor if scope else "system") as exact:
        yield exact
