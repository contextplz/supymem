"""Local workspace + sandbox adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.provisioning.protocol import Preflight, ProvisionResult


def _root(venture_id: str) -> Path:
    return Path(__file__).resolve().parents[5] / "data" / "ventures" / venture_id


class WorkspaceLocal:
    resource = "workspace"
    provider = "local"
    capability = "fs:write"
    required_credentials: list[str] = []

    async def preflight(self, ctx: Any) -> Preflight:
        return Preflight(ok=True)

    async def provision(self, ctx: Any) -> ProvisionResult:
        path = _root(ctx.venture.id) / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return ProvisionResult(
            resource="workspace",
            simulated=True,
            handle=f"local:workspace:{ctx.venture.id}",
            display="Local workspace",
            provider="local",
            url=None,
            detail=[str(path)],
            teardown={"path": str(path)},
        )

    async def teardown(self, handle: str, data: dict) -> None:
        return None


class SandboxLocal:
    resource = "sandbox"
    provider = "local"
    capability = "proc:spawn"
    required_credentials: list[str] = []

    async def preflight(self, ctx: Any) -> Preflight:
        return Preflight(ok=True)

    async def provision(self, ctx: Any) -> ProvisionResult:
        path = _root(ctx.venture.id) / "sandbox"
        path.mkdir(parents=True, exist_ok=True)
        return ProvisionResult(
            resource="sandbox",
            simulated=True,
            handle=f"local:sandbox:{ctx.venture.id}",
            display="Isolated workspace",
            provider="local",
            detail=[str(path)],
            teardown={"path": str(path)},
        )

    async def teardown(self, handle: str, data: dict) -> None:
        return None
