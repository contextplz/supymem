"""One connector protocol; every provider is a thin adapter (39 §3).

Two rules are structural here rather than a review convention:

* A provider has no write surface until it overrides ``_perform_write`` in
  :mod:`app.connectors.base`. Read-only is a connector's default state.
* Every adapter call carries a :class:`Ticket`, and a ticket can only be minted
  by :mod:`app.connectors.access`, which is the module that consults the gate.
  An adapter therefore cannot be driven without the observe ladder having run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Direction = Literal["read", "write", "webhook"]

# Target classes from app.authz.effective.TARGET_MIN_OBSERVE. Named here so a
# provider declares the access it needs and the gate resolves the level.
METADATA = "connection:metadata"
READ = "connection:read"
SANDBOX_WRITE = "connection:sandbox_write"
WRITE = "connection:write"


class ConnectorError(RuntimeError):
    """A provider call failed. Message is safe to show a founder."""


class RateLimited(ConnectorError):
    def __init__(self, retry_after_s: float, *, provider: str = "") -> None:
        super().__init__(f"rate limited, retry after {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s
        self.provider = provider


class AuthExpired(ConnectorError):
    """401/403 from the provider. Never retried in a loop (39 §6)."""

    def __init__(self, provider: str, status: int = 401) -> None:
        super().__init__(f"{provider} access expired")
        self.provider = provider
        self.status = status


class WriteNotSupported(ConnectorError):
    """The provider declares no such write capability."""


class TicketRequired(PermissionError):
    """An adapter was called without a ticket minted by the access chokepoint."""


@dataclass(frozen=True)
class ScopeSpec:
    """One provider scope. Copy comes from the manifest, never from Python."""

    scope: str
    writes: bool
    required_for: tuple[str, ...] = ()
    reason: str = ""
    allows: str = ""
    excludes: str = ""


@dataclass(frozen=True)
class ReadSpec:
    operation: str
    kind: str
    required_scope: str
    target_class: str = READ


@dataclass(frozen=True)
class Capability:
    """A write a provider can perform, and the single scope that permits it."""

    id: str
    operation: str
    required_scope: str
    target_class: str = WRITE


@dataclass(frozen=True)
class ConnectionView:
    """What an adapter sees. Deliberately carries a ref, never a secret."""

    id: str
    workspace_id: str
    provider: str
    external_account: str
    status: str
    auth_kind: str
    secret_ref: str | None
    scopes: tuple[str, ...] = ()
    declined_scopes: tuple[str, ...] = ()
    resource_grants: tuple[str, ...] = ()
    cursors: dict[str, str] = field(default_factory=dict)
    simulated: bool = True
    health: str = "unknown"
    webhook_ref: str | None = None
    last_error: str | None = None
    last_sync_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "provider": self.provider,
            "external_account": self.external_account,
            "status": self.status,
            "auth_kind": self.auth_kind,
            "scopes": list(self.scopes),
            "declined_scopes": list(self.declined_scopes),
            "resource_grants": list(self.resource_grants),
            "simulated": self.simulated,
            "health": self.health,
            "last_error": self.last_error,
            "last_sync_at": self.last_sync_at,
        }


@dataclass(frozen=True)
class Resource:
    kind: str
    ref: str
    display: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "display": self.display,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ResourceRef:
    kind: str
    ref: str


@dataclass(frozen=True)
class Page:
    """One page of a cursor-checkpointed list read (39 §6)."""

    items: tuple[Resource, ...]
    cursor: str | None = None
    exhausted: bool = True
    bytes_in: int = 0


@dataclass(frozen=True)
class ReadResult:
    ref: str
    kind: str
    content: str | None = None
    items: tuple[Resource, ...] = ()
    bytes_in: int = 0


@dataclass(frozen=True)
class Action:
    """A requested write. ``content_hash`` feeds the idempotency key (39 §6)."""

    capability: str
    target: str
    payload: dict[str, Any] = field(default_factory=dict)
    engagement_id: str | None = None
    content_hash: str = ""
    sandbox: bool = False


@dataclass(frozen=True)
class ActionResult:
    external_id: str
    display: str
    url: str | None = None
    bytes_out: int = 0
    simulated: bool = True
    reused: bool = False


@dataclass(frozen=True)
class AuthChallenge:
    kind: str
    requested_scopes: tuple[str, ...]
    url: str | None = None
    instructions_key: str = ""
    concierge_task: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "requested_scopes": list(self.requested_scopes),
            "url": self.url,
            "instructions_key": self.instructions_key,
            "concierge_task": self.concierge_task,
        }


@dataclass(frozen=True)
class Health:
    state: str
    detail: str = ""


@dataclass(frozen=True)
class WebhookRef:
    id: str
    secret_ref: str
    events: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebhookEvent:
    provider: str
    delivery_id: str
    event: str
    payload: dict[str, Any]
    target: str | None = None


# Only app.connectors.access may mint a ticket. Adapters receive one and
# validate it; nothing else in the tree can construct a valid instance.
_MINT = object()


@dataclass(frozen=True)
class Ticket:
    connection_id: str
    direction: Direction
    operation: str
    target: str | None
    scope: str
    target_class: str
    actor: str
    engagement_id: str | None = None
    proof: Any = None

    def __post_init__(self) -> None:
        if self.proof is not _MINT:
            raise TicketRequired(
                "tickets are minted by app.connectors.access, not by callers"
            )


def mint_ticket(
    *,
    connection_id: str,
    direction: Direction,
    operation: str,
    target: str | None,
    scope: str,
    target_class: str,
    actor: str,
    engagement_id: str | None = None,
) -> Ticket:
    """Internal to :mod:`app.connectors.access`; do not call from an adapter."""
    return Ticket(
        connection_id=connection_id,
        direction=direction,
        operation=operation,
        target=target,
        scope=scope,
        target_class=target_class,
        actor=actor,
        engagement_id=engagement_id,
        proof=_MINT,
    )


@runtime_checkable
class Connector(Protocol):
    provider: str
    scope_catalog: dict[str, ScopeSpec]

    def auth_start(
        self, workspace_id: str, scopes: list[str]
    ) -> AuthChallenge: ...

    def auth_complete(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def health(self, con: ConnectionView) -> Health: ...

    def reads(self) -> list[ReadSpec]: ...

    def capabilities(self) -> list[Capability]: ...

    async def list_resources(
        self, con: ConnectionView, kind: str, ticket: Ticket, cursor: str | None
    ) -> Page: ...

    async def read(
        self, con: ConnectionView, ref: ResourceRef, ticket: Ticket
    ) -> ReadResult: ...

    async def execute(
        self, con: ConnectionView, action: Action, ticket: Ticket
    ) -> ActionResult: ...

    async def register_webhook(
        self,
        con: ConnectionView,
        events: list[str],
        ticket: Ticket,
        *,
        secret: str,
        callback_url: str,
    ) -> WebhookRef | None: ...

    def verify_webhook(
        self, raw: bytes, headers: dict[str, str], secret: str
    ) -> WebhookEvent | None: ...

    def revoke(self, con: ConnectionView) -> None: ...
