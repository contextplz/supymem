"""The one chokepoint in front of every connected system (39 §2, §3).

Nothing reaches a provider without passing through here, and nothing passes
through here without a ``connection_events`` row — including refusals. The
observe ladder is evaluated by :func:`app.authz.gate.check` and never
re-implemented; this module only decides which target class an operation needs
and turns an allowed decision into a :class:`~app.connectors.protocol.Ticket`.
"""

from __future__ import annotations

from app.authz.gate import Denied, check
from app.connectors import events
from app.connectors.base import ConnectorBase
from app.connectors.narration import line
from app.connectors.protocol import (
    Action,
    Capability,
    ConnectionView,
    ReadSpec,
    SANDBOX_WRITE,
    Ticket,
    WRITE,
    mint_ticket,
)
from app.connectors.scopes import ScopeNotGranted, require_scope

# Reading is a "read" capability; changing something in their system is work.
READ_CAPABILITY = "read"
WRITE_CAPABILITY = "request_work"

USABLE_STATUSES = frozenset({"active"})


class ConnectionUnavailable(PermissionError):
    """The connection cannot be used right now (expired, revoked, pending)."""

    def __init__(self, con: ConnectionView, reason: str) -> None:
        super().__init__(reason)
        self.connection_id = con.id
        self.status = con.status
        self.reason = reason


async def authorize_read(
    con: ConnectionView,
    connector: ConnectorBase,
    kind: str,
    *,
    actor_id: str,
    actor_kind: str = "person",
    surface: str = "internal",
    token_scopes: frozenset[str] | set[str] | None = None,
    engagement_id: str | None = None,
    target: str | None = None,
) -> Ticket:
    spec: ReadSpec = connector.read_spec(kind)
    return await _authorize(
        con,
        connector,
        direction="read",
        capability=READ_CAPABILITY,
        operation=spec.operation,
        scope=spec.required_scope,
        target_class=spec.target_class,
        target=target,
        actor_id=actor_id,
        actor_kind=actor_kind,
        surface=surface,
        token_scopes=token_scopes,
        engagement_id=engagement_id,
    )


async def authorize_write(
    con: ConnectionView,
    connector: ConnectorBase,
    action: Action,
    *,
    actor_id: str,
    actor_kind: str = "person",
    surface: str = "internal",
    token_scopes: frozenset[str] | set[str] | None = None,
) -> Ticket:
    cap: Capability = connector.capability(action.capability)
    target_class = SANDBOX_WRITE if action.sandbox else (cap.target_class or WRITE)
    return await _authorize(
        con,
        connector,
        direction="write",
        capability=WRITE_CAPABILITY,
        operation=cap.operation,
        scope=cap.required_scope,
        target_class=target_class,
        target=action.target,
        actor_id=actor_id,
        actor_kind=actor_kind,
        surface=surface,
        token_scopes=token_scopes,
        engagement_id=action.engagement_id,
    )


async def _authorize(
    con: ConnectionView,
    connector: ConnectorBase,
    *,
    direction: str,
    capability: str,
    operation: str,
    scope: str,
    target_class: str,
    target: str | None,
    actor_id: str,
    actor_kind: str,
    surface: str,
    token_scopes: frozenset[str] | set[str] | None,
    engagement_id: str | None,
) -> Ticket:
    if con.status not in USABLE_STATUSES:
        reason = line(
            "connections.unusable",
            status="blocked",
            tokens={"provider": con.provider},
        )
        _deny(
            con,
            direction=direction,
            operation=operation,
            target=target,
            actor=actor_id,
            engagement_id=engagement_id,
            reason=f"connection is {con.status}",
        )
        raise ConnectionUnavailable(con, reason or f"connection is {con.status}")

    decision = await check(
        capability,
        workspace_id=con.workspace_id,
        target=target or con.external_account,
        target_class=target_class,
        actor_id=actor_id,
        actor_kind=actor_kind,
        token_scopes=token_scopes,
        surface=surface,
    )
    if not decision.allowed:
        _deny(
            con,
            direction=direction,
            operation=operation,
            target=target,
            actor=actor_id,
            engagement_id=engagement_id,
            reason=decision.reason,
        )
        raise Denied(decision)

    try:
        require_scope(
            con,
            scope,
            feature=connector.feature(operation),
            spec=connector.scope_catalog.get(scope),
            ask=line(
                "connections.scope_request",
                status="ask",
                tokens={
                    "feature": connector.feature(operation),
                    "account": con.external_account,
                },
            ),
        )
    except ScopeNotGranted as exc:
        _deny(
            con,
            direction=direction,
            operation=operation,
            target=target,
            actor=actor_id,
            engagement_id=engagement_id,
            reason=f"scope '{scope}' not granted",
        )
        raise exc

    return mint_ticket(
        connection_id=con.id,
        direction=direction,  # type: ignore[arg-type]
        operation=operation,
        target=target,
        scope=scope,
        target_class=target_class,
        actor=actor_id,
        engagement_id=engagement_id,
    )


def _deny(
    con: ConnectionView,
    *,
    direction: str,
    operation: str,
    target: str | None,
    actor: str,
    engagement_id: str | None,
    reason: str,
) -> None:
    events.record(
        connection_id=con.id,
        workspace_id=con.workspace_id,
        direction=direction,
        operation=operation,
        target=target,
        actor=actor,
        engagement_id=engagement_id,
        outcome="denied",
        denied_reason=reason,
        simulated=con.simulated,
    )
