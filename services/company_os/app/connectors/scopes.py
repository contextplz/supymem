"""Progressive scope requests (39 §5).

One scope per request, asked at the moment a feature needs it, always naming
the feature and always stating what the scope does *not* allow. A decline is
remembered so the same session does not ask twice, and it leaves the caller a
resumable reason instead of a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.connectors.protocol import ConnectionView, ScopeSpec


@dataclass(frozen=True)
class ScopeRequest:
    connection_id: str
    provider: str
    external_account: str
    scope: str
    feature: str
    reason: str = ""
    allows: str = ""
    excludes: str = ""
    declined_before: bool = False
    ask: str = ""

    def sentence(self) -> str:
        parts = [self.ask, self.allows, self.excludes]
        return " ".join(p.strip() for p in parts if p and p.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "external_account": self.external_account,
            "scope": self.scope,
            "feature": self.feature,
            "reason": self.reason,
            "allows": self.allows,
            "excludes": self.excludes,
            "declined_before": self.declined_before,
            "sentence": self.sentence(),
        }


class ScopeNotGranted(PermissionError):
    """Raised at the moment of need, carrying everything the prompt requires."""

    def __init__(self, request: ScopeRequest) -> None:
        super().__init__(request.sentence() or f"scope {request.scope} not granted")
        self.request = request
        self.scope = request.scope
        self.declined_before = request.declined_before

    @property
    def resumable_reason(self) -> str:
        """What blocks the engagement, in one line the founder can act on."""
        return f"needs {self.scope} on {self.request.external_account}"


@dataclass(frozen=True)
class GrantedScope:
    scope: str
    granted_at: str
    granted_by: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "granted_at": self.granted_at,
            "granted_by": self.granted_by,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ScopeCatalogEntry:
    spec: ScopeSpec
    features: tuple[str, ...] = field(default_factory=tuple)


def granted(con: ConnectionView) -> frozenset[str]:
    return frozenset(con.scopes)


def declined(con: ConnectionView) -> frozenset[str]:
    return frozenset(con.declined_scopes)


def build_request(
    con: ConnectionView,
    scope: str,
    *,
    feature: str,
    spec: ScopeSpec | None = None,
    ask: str = "",
) -> ScopeRequest:
    return ScopeRequest(
        connection_id=con.id,
        provider=con.provider,
        external_account=con.external_account,
        scope=scope,
        feature=feature,
        reason=spec.reason if spec else "",
        allows=spec.allows if spec else "",
        excludes=spec.excludes if spec else "",
        declined_before=scope in declined(con),
        ask=ask,
    )


def require_scope(
    con: ConnectionView,
    scope: str,
    *,
    feature: str = "",
    spec: ScopeSpec | None = None,
    ask: str = "",
) -> None:
    """Raise :class:`ScopeNotGranted` unless this exact scope is already granted."""
    if scope in granted(con):
        return
    raise ScopeNotGranted(
        build_request(con, scope, feature=feature, spec=spec, ask=ask)
    )


def should_ask_again(con: ConnectionView, scope: str) -> bool:
    """False once declined: re-asking inside a session is how consent rots."""
    return scope not in declined(con)


def next_scope_to_request(
    con: ConnectionView,
    needed: tuple[str, ...],
) -> str | None:
    """The single next ungranted scope, in the order the provider declared them."""
    for scope in needed:
        if scope not in granted(con):
            return scope
    return None
