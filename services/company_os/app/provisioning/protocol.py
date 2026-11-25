"""Adapter protocol (28 §2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Resource = Literal[
    "workspace", "repo", "database", "email", "hosting", "domain", "payments", "sandbox"
]


@dataclass
class Preflight:
    ok: bool
    reason: str | None = None


@dataclass
class ProvisionResult:
    resource: Resource
    simulated: bool
    handle: str
    display: str
    provider: str = "local"
    url: str | None = None
    detail: list[str] = field(default_factory=list)
    secrets_written: list[str] = field(default_factory=list)
    teardown: dict[str, Any] | None = None
    cost_usd: float = 0.0
    note: str | None = None


class Adapter(Protocol):
    resource: Resource
    provider: str
    capability: str | None
    required_credentials: list[str]

    async def preflight(self, ctx: Any) -> Preflight: ...

    async def provision(self, ctx: Any) -> ProvisionResult: ...

    async def teardown(self, handle: str, data: dict) -> None: ...
