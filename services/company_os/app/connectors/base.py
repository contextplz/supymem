"""Shared adapter machinery: ticket validation and read-only-by-default.

A provider subclasses :class:`ConnectorBase` and implements ``_perform_*``. It
never validates its own ticket and never decides its own scope, so a new
provider cannot forget either check. Omitting ``_perform_write`` leaves the
provider read-only at every observe level, which is the correct default and the
permanent state of some providers (Stripe, 39 §4).
"""

from __future__ import annotations

from typing import Any

from app.connectors.protocol import (
    Action,
    ActionResult,
    Capability,
    ConnectionView,
    ConnectorError,
    Health,
    Page,
    ReadResult,
    ReadSpec,
    ResourceRef,
    ScopeSpec,
    Ticket,
    TicketRequired,
    WebhookEvent,
    WebhookRef,
    WriteNotSupported,
)


class ConnectorBase:
    provider: str = ""
    auth_kind: str = "pat"
    # The scope a connection starts with: enough to name resources, nothing more.
    baseline_scope: str = ""
    scopes: dict[str, ScopeSpec] = {}
    read_specs: tuple[ReadSpec, ...] = ()
    write_capabilities: tuple[Capability, ...] = ()

    def __init__(
        self,
        manifest: dict[str, Any] | None = None,
        *,
        simulated: bool = False,
    ) -> None:
        self.manifest = manifest or {}
        self.simulated = simulated
        self.scope_catalog = self._merge_scope_copy()

    def _merge_scope_copy(self) -> dict[str, ScopeSpec]:
        """Structure from Python, founder-facing copy from the manifest (I6)."""
        copy = (self.manifest.get("scopes") or {}) if self.manifest else {}
        merged: dict[str, ScopeSpec] = {}
        for name, spec in self.scopes.items():
            text = copy.get(name) or {}
            merged[name] = ScopeSpec(
                scope=spec.scope,
                writes=spec.writes,
                required_for=spec.required_for,
                reason=str(text.get("reason", "")),
                allows=str(text.get("allows", "")),
                excludes=str(text.get("excludes", "")),
            )
        return merged

    # ── declarations ────────────────────────────────────────────────────────
    def reads(self) -> list[ReadSpec]:
        return list(self.read_specs)

    def capabilities(self) -> list[Capability]:
        return list(self.write_capabilities)

    def read_spec(self, kind: str) -> ReadSpec:
        for spec in self.read_specs:
            if spec.kind == kind:
                return spec
        raise ConnectorError(f"{self.provider} cannot read '{kind}'")

    def capability(self, capability_id: str) -> Capability:
        for cap in self.write_capabilities:
            if cap.id == capability_id:
                return cap
        raise WriteNotSupported(f"{self.provider} cannot {capability_id}")

    def scope_for(self, capability_id: str) -> str:
        return self.capability(capability_id).required_scope

    def feature(self, operation: str) -> str:
        """The feature to name when asking for a scope (39 §5), from the manifest."""
        features = self.manifest.get("features") or {}
        return str(features.get(operation) or operation.replace("_", " "))

    def rate_limit(self) -> dict[str, Any]:
        return dict(self.manifest.get("rate_limit") or {})

    def poll_interval_s(self) -> int:
        return int((self.manifest.get("sync") or {}).get("min_poll_seconds", 60))

    # ── ticket gate ─────────────────────────────────────────────────────────
    def _require_ticket(
        self,
        ticket: Ticket,
        con: ConnectionView,
        *,
        direction: str,
        operation: str,
        scope: str,
    ) -> None:
        if not isinstance(ticket, Ticket):
            raise TicketRequired(f"{self.provider}.{operation} needs a ticket")
        mismatch = (
            ticket.direction != direction
            or ticket.operation != operation
            or ticket.scope != scope
            or ticket.connection_id != con.id
        )
        if mismatch:
            raise TicketRequired(
                f"ticket does not authorize {direction}:{operation} on {con.id}"
            )

    # ── surfaces ────────────────────────────────────────────────────────────
    async def list_resources(
        self,
        con: ConnectionView,
        kind: str,
        ticket: Ticket,
        cursor: str | None = None,
    ) -> Page:
        spec = self.read_spec(kind)
        self._require_ticket(
            ticket,
            con,
            direction="read",
            operation=spec.operation,
            scope=spec.required_scope,
        )
        return await self._perform_list(con, kind, cursor)

    async def read(
        self, con: ConnectionView, ref: ResourceRef, ticket: Ticket
    ) -> ReadResult:
        spec = self.read_spec(ref.kind)
        self._require_ticket(
            ticket,
            con,
            direction="read",
            operation=spec.operation,
            scope=spec.required_scope,
        )
        return await self._perform_read(con, ref)

    async def execute(
        self, con: ConnectionView, action: Action, ticket: Ticket
    ) -> ActionResult:
        cap = self.capability(action.capability)
        self._require_ticket(
            ticket,
            con,
            direction="write",
            operation=cap.operation,
            scope=cap.required_scope,
        )
        return await self._perform_write(con, action, cap)

    async def register_webhook(
        self,
        con: ConnectionView,
        events: list[str],
        ticket: Ticket,
        *,
        secret: str,
        callback_url: str,
    ) -> WebhookRef | None:
        """Registering a hook is a write; a provider without the capability opts out."""
        try:
            cap = self.capability("register_webhook")
        except WriteNotSupported:
            return None
        self._require_ticket(
            ticket,
            con,
            direction="write",
            operation=cap.operation,
            scope=cap.required_scope,
        )
        return await self._perform_register_webhook(
            con, events, secret=secret, callback_url=callback_url
        )

    def verify_webhook(
        self, raw: bytes, headers: dict[str, str], secret: str
    ) -> WebhookEvent | None:
        return None

    def health(self, con: ConnectionView) -> Health:
        return Health(con.health or "unknown")

    def revoke(self, con: ConnectionView) -> None:
        """Forget our access. Never removes anything the founder owns (39 §1)."""
        return None

    # ── provider hooks ──────────────────────────────────────────────────────
    async def _perform_list(
        self, con: ConnectionView, kind: str, cursor: str | None
    ) -> Page:
        raise ConnectorError(f"{self.provider} has no list surface for {kind}")

    async def _perform_read(
        self, con: ConnectionView, ref: ResourceRef
    ) -> ReadResult:
        raise ConnectorError(f"{self.provider} has no read surface for {ref.kind}")

    async def _perform_write(
        self, con: ConnectionView, action: Action, cap: Capability
    ) -> ActionResult:
        raise WriteNotSupported(f"{self.provider} is read-only")

    async def _perform_register_webhook(
        self,
        con: ConnectionView,
        events: list[str],
        *,
        secret: str,
        callback_url: str,
    ) -> WebhookRef | None:
        return None
