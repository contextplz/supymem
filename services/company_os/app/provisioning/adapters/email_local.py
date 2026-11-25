"""Local email identity (maildir sink)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.intake.naming import slug_for_email
from app.provisioning.protocol import Preflight, ProvisionResult


class EmailLocal:
    resource = "email"
    provider = "local"
    capability = "net:egress"
    required_credentials: list[str] = []

    async def preflight(self, ctx: Any) -> Preflight:
        return Preflight(ok=True)

    async def provision(self, ctx: Any) -> ProvisionResult:
        slug = slug_for_email(ctx.company)
        addr = f"{slug}@companyos.local"
        mail = Path(__file__).resolve().parents[5] / "data" / "ventures" / ctx.venture.id / "mail"
        mail.mkdir(parents=True, exist_ok=True)
        (mail / "address.txt").write_text(addr, encoding="utf-8")
        return ProvisionResult(
            resource="email",
            simulated=True,
            handle=f"local:email:{addr}",
            display=addr,
            provider="local",
            url=addr,
            detail=[addr, str(mail)],
            teardown={"path": str(mail)},
        )

    async def teardown(self, handle: str, data: dict) -> None:
        return None
