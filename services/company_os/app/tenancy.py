"""Venture/workspace scope — the single source of tenancy for store calls.

Doc 81 §5 / S1.1. Named `tenancy` (not `boundary`) because `app/boundary/` already
owns the data-classification taxonomy (doc 43).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class Scope:
    workspace_id: str
    venture_id: str
    actor: str = "system"


_CURRENT: ContextVar[Scope | None] = ContextVar("company_os_scope", default=None)


class ScopeRequired(RuntimeError):
    """Raised when a tenancy-scoped call runs with no Scope set."""


def current() -> Scope:
    scope = _CURRENT.get()
    if scope is None:
        raise ScopeRequired(
            "No tenancy Scope is set. Middleware or the caller must bind "
            "workspace_id + venture_id before touching memory, money, or actuation."
        )
    return scope


def current_or_none() -> Scope | None:
    return _CURRENT.get()


def set_scope(scope: Scope) -> Token[Scope | None]:
    return _CURRENT.set(scope)


def reset_scope(token: Token[Scope | None]) -> None:
    _CURRENT.reset(token)


@contextmanager
def bind(
    *,
    workspace_id: str,
    venture_id: str,
    actor: str = "system",
) -> Iterator[Scope]:
    scope = Scope(workspace_id=workspace_id, venture_id=venture_id, actor=actor)
    token = set_scope(scope)
    try:
        yield scope
    finally:
        reset_scope(token)


ROUTE_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "/health",
    "/ready",
    "/internal",
    "/webhooks",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def is_allowlisted(path: str) -> bool:
    if path in {"/health", "/ready", "/openapi.json", "/docs", "/redoc"}:
        return True
    return path.startswith(("/internal/", "/webhooks/", "/docs/"))


@contextmanager
def scoped(venture_id: str, *, actor: str = "system") -> Iterator[Scope]:
    """Bind Scope when missing (jobs / gbrain). Honour an existing Scope."""
    existing = current_or_none()
    if existing is None:
        with bind(workspace_id="none", venture_id=venture_id, actor=actor) as scope:
            yield scope
        return
    if existing.venture_id and existing.venture_id != venture_id:
        raise PermissionError(
            f"tenancy violation: scope venture={existing.venture_id} != requested={venture_id}"
        )
    yield existing


def assert_venture(venture_id: str) -> None:
    """Hard tenancy check — never a no-op. Missing Scope is a programming error."""
    scope = current_or_none()
    if scope is None:
        raise ScopeRequired(
            "No tenancy Scope is set. Middleware or the caller must bind "
            "workspace_id + venture_id before touching memory, money, or actuation."
        )
    if scope.venture_id and scope.venture_id != venture_id:
        raise PermissionError(
            f"tenancy violation: scope venture={scope.venture_id} != requested={venture_id}"
        )


__all__ = [
    "Scope",
    "ScopeRequired",
    "assert_venture",
    "bind",
    "current",
    "current_or_none",
    "reset_scope",
    "set_scope",
    "scoped",
    "is_allowlisted",
    "ROUTE_ALLOWLIST_PREFIXES",
]
