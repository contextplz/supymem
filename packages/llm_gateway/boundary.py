"""The boundary seam inside the model chokepoint (43 §4).

`llm_gateway.sole.transmit` is the only door to a provider, and it may not open
without a decision from a boundary filter. The filter itself lives in
`app.boundary.filter` because it owns the ledger table; this module holds the
contract between them and the fail-closed default.

Nothing here inspects a payload. Nothing here may import the app package at
module scope: the gateway is installed in processes that have no Company OS.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

FILTER_MODULE = "app.boundary.filter"
FILTER_ATTR = "inspect_payload"


class BoundaryViolation(RuntimeError):
    """A payload was refused. Hard failure — never truncate and send anyway.

    `message` is founder-facing and names the class and the file, never the
    value. `classes` and `reason` are for the ledger and the trust panel.
    """

    def __init__(
        self,
        message: str,
        *,
        classes: Sequence[str] = (),
        reason: str = "boundary_violation",
        ledger_id: str | None = None,
        alternative: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.classes = tuple(classes)
        self.reason = reason
        self.ledger_id = ledger_id
        # What we offer instead, when a local computation would answer the same
        # question (43 §3). Absent when there is no honest alternative.
        self.alternative = alternative


@dataclass(frozen=True)
class Decision:
    """What the filter allows out, and the evidence row it already wrote."""

    messages: list[dict[str, Any]]
    classes: tuple[str, ...] = ()
    redactions: int = 0
    bytes_out: int = 0
    provider: str = ""
    ledger_id: str | None = None
    trace_id: str | None = None
    findings: tuple[str, ...] = field(default=())


class BoundaryFilter(Protocol):
    def __call__(
        self,
        messages: list[dict[str, Any]],
        *,
        purpose: str,
        workspace_id: str | None,
        provider: str,
        segments: Sequence[Any] | None = None,
        trace_id: str | None = None,
    ) -> Decision: ...


_registered: BoundaryFilter | None = None


def register_filter(fn: BoundaryFilter | None) -> None:
    """Install a filter explicitly. Hosts without Company OS need this."""
    global _registered
    _registered = fn


def resolve_filter() -> BoundaryFilter:
    """Return the active filter, or fail closed if there is none.

    Deliberately re-imports rather than caching the resolved callable: the test
    harness reloads the Company OS `app` package, and a cached function would
    keep writing to a database session that no longer exists.
    """
    if _registered is not None:
        return _registered
    try:
        module = importlib.import_module(FILTER_MODULE)
    except ImportError as exc:
        raise BoundaryViolation(
            "Nothing can be sent to a model: the data boundary is not installed.",
            classes=("unknown",),
            reason="no_boundary_filter",
        ) from exc
    fn: Callable[..., Decision] | None = getattr(module, FILTER_ATTR, None)
    if fn is None:
        raise BoundaryViolation(
            "Nothing can be sent to a model: the data boundary is not installed.",
            classes=("unknown",),
            reason="no_boundary_filter",
        )
    return fn  # type: ignore[return-value]


def provider_label(tier: str = "pro", *, config: Any = None) -> str:
    """`<provider>:<deployment>` for the ledger, without dialing anything."""
    try:
        from llm_gateway.provider_foundry import (
            deployment_for,
            load_provider_config_from_env,
        )

        cfg = config if config is not None else load_provider_config_from_env()
        prefix = {
            "azure_foundry": "foundry",
            "openai_compatible": "openai_compatible",
            "deepseek_direct": "deepseek",
            "anthropic": "anthropic",
        }.get(cfg.provider, cfg.provider)
        return f"{prefix}:{deployment_for(cfg, tier)}"
    except Exception:  # noqa: BLE001 — an unreadable config must not skip the ledger
        return "unknown:unknown"


__all__ = [
    "BoundaryFilter",
    "BoundaryViolation",
    "Decision",
    "provider_label",
    "register_filter",
    "resolve_filter",
]
