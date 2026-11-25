"""Local domain reservation (*.preview.localhost)."""

from __future__ import annotations

from typing import Any

from app.intake.naming import slug_for_email
from app.provisioning.protocol import Preflight, ProvisionResult


class DomainLocal:
    resource = "domain"
    provider = "local"
    capability = None
    required_credentials: list[str] = []

    async def preflight(self, ctx: Any) -> Preflight:
        return Preflight(ok=True)

    async def provision(self, ctx: Any) -> ProvisionResult:
        slug = slug_for_email(ctx.company)
        host = f"{slug}.preview.localhost"
        return ProvisionResult(
            resource="domain",
            simulated=True,
            handle=f"local:domain:{host}",
            display=host,
            provider="local",
            url=f"http://{host}",
            detail=[host],
        )

    async def teardown(self, handle: str, data: dict) -> None:
        return None
