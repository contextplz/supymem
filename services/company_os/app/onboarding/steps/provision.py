"""Phase 4 — provision via local adapters."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.db.models import VentureResource
from app.onboarding.registry import StepResult
from app.onboarding.state import Ctx
from app.provisioning.registry import select as select_adapter
from app.state import db
from core.clock import utcnow
from core.ids import new_id


async def _provision_resource(ctx: Ctx, resource: str) -> StepResult:
    adapter = await select_adapter(resource, ctx)
    result = await adapter.provision(ctx)
    factory = db()
    now = utcnow()
    with factory() as session:
        existing = session.scalars(
            select(VentureResource).where(
                VentureResource.venture_id == ctx.venture.id,
                VentureResource.resource == resource,
            )
        ).one_or_none()
        if existing:
            existing.provider = result.provider
            existing.simulated = 1 if result.simulated else 0
            existing.handle = result.handle
            existing.display = result.display
            existing.url = result.url
            existing.detail_json = json.dumps(result.detail)
            existing.teardown_json = json.dumps(result.teardown) if result.teardown else None
            existing.updated_at = now
        else:
            session.add(
                VentureResource(
                    id=new_id("vrs"),
                    venture_id=ctx.venture.id,
                    resource=resource,
                    provider=result.provider,
                    simulated=1 if result.simulated else 0,
                    handle=result.handle,
                    display=result.display,
                    url=result.url,
                    detail_json=json.dumps(result.detail),
                    teardown_json=json.dumps(result.teardown) if result.teardown else None,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    return StepResult(
        output={
            "resource": resource,
            "provider": result.provider,
            "simulated": result.simulated,
            "handle": result.handle,
            "display": result.display,
            "url": result.url,
        },
        simulated=result.simulated,
        detail=result.detail,
        tokens={"email": result.url if resource == "email" else None, "company": ctx.company},
    )


async def workspace(ctx: Ctx) -> StepResult:
    return await _provision_resource(ctx, "workspace")


async def repo(ctx: Ctx) -> StepResult:
    return await _provision_resource(ctx, "repo")


async def database(ctx: Ctx) -> StepResult:
    return await _provision_resource(ctx, "database")


async def email(ctx: Ctx) -> StepResult:
    res = await _provision_resource(ctx, "email")
    email_addr = res.output.get("display") or res.output.get("handle")
    res.tokens = {**(res.tokens or {}), "email": email_addr, "company": ctx.company}
    return res


async def hosting(ctx: Ctx) -> StepResult:
    return await _provision_resource(ctx, "hosting")


async def sandbox(ctx: Ctx) -> StepResult:
    return await _provision_resource(ctx, "sandbox")
