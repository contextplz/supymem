"""Local SQLite database adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.provisioning.protocol import Preflight, ProvisionResult


class DbLocal:
    resource = "database"
    provider = "local"
    capability = "net:egress"
    required_credentials: list[str] = []

    async def preflight(self, ctx: Any) -> Preflight:
        return Preflight(ok=True)

    async def provision(self, ctx: Any) -> ProvisionResult:
        path = (
            Path(__file__).resolve().parents[5]
            / "data"
            / "ventures"
            / ctx.venture.id
            / "app.db"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"")
        return ProvisionResult(
            resource="database",
            simulated=True,
            handle=f"sqlite:{path}",
            display="Local SQLite",
            provider="local",
            url=f"sqlite:///{path}",
            detail=[str(path)],
            teardown={"path": str(path)},
        )

    async def teardown(self, handle: str, data: dict) -> None:
        return None
