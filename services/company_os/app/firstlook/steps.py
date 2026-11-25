"""Onboarding steps for the first_look phase (45, 35 §6)."""

from __future__ import annotations

from llm_gateway.errors import ProviderCallError
from fastapi import HTTPException

from app.core.local_mode import is_local
from app.credits.errors import CreditError
from app.firstlook import service as firstlook_service
from app.onboarding.registry import PauseRequest, StepDef, StepResult
from app.onboarding.state import Ctx
from app.state import MEM


def _idea(ctx: Ctx) -> str:
    return (
        (ctx.out.get("orient.read_brief") or {}).get("normalized")
        or ctx.venture.raw_idea
        or ""
    )


def _archetype(ctx: Ctx) -> str:
    """Prefer build_archetype — business_model often defaults to saas→tool for everything."""
    classification = ctx.out.get("understand.classify") or {}
    idea = (_idea(ctx) or "").lower()
    build = str(classification.get("build_archetype") or "").strip()
    bm = str(classification.get("business_model") or "").strip()
    if build and build.lower() not in {"saas", "product", "unknown"}:
        return build
    # Light heuristic from the idea text when the classifier is bland.
    if any(k in idea for k in ("marketplace", "two-sided", "buyers and sellers", "buy and sell")):
        return "marketplace"
    if any(k in idea for k in ("newsletter", "podcast", "content", "media")):
        return "content"
    if any(k in idea for k in ("local", "neighbourhood", "neighborhood", "near me", "farm")):
        return "local"
    if any(k in idea for k in ("hardware", "device", "physical product")):
        return "physical"
    if any(k in idea for k in ("app for", "consumer", "dating", "habit")):
        return "consumer"
    if bm and bm.lower() not in {"saas", "unknown"}:
        return bm
    if build:
        return build
    return "generic"


def _audience(ctx: Ctx) -> str | None:
    classification = ctx.out.get("understand.classify") or {}
    aud = classification.get("audience")
    return str(aud) if aud else None


def _workspace_id(ctx: Ctx) -> str:
    ws = getattr(ctx.venture, "workspace_id", None)
    if ws:
        return str(ws)
    # Legacy ventures without a workspace row: use the venture id as the key.
    return str(ctx.venture.id)


async def generate(ctx: Ctx) -> StepResult:
    """Build the instant demo page from the raw idea (LD-59)."""
    idea = _idea(ctx)
    research = ctx.out.get("understand.research_market") or {}
    from app.org.heartbeat import mark_idle, mark_working

    try:
        mark_working("designer", ctx.venture.id, "Planning the first landing page.")
        mark_working("builder", ctx.venture.id, "Waiting on the page plan.")
    except Exception:  # noqa: BLE001
        pass
    try:
        view = await firstlook_service.generate_first_look(
            _workspace_id(ctx),
            idea,
            venture_id=ctx.venture.id,
            venture_name=ctx.venture.name,
            archetype=_archetype(ctx),
            audience=_audience(ctx),
            stage="first_look",
            research=research if isinstance(research, dict) else None,
        )
    except (ProviderCallError, CreditError):
        # A paid unknown or credit refusal is a pause, never a successful free rerun.
        raise
    except Exception as exc:  # noqa: BLE001
        if is_local():
            message = "Your coding tool did not finish your page. Open Builds to check the failed run, then ask Charlie to retry your page."
            if isinstance(exc, firstlook_service.FirstLookError):
                message = exc.message
            elif isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
                message = exc.detail.get("message") or (exc.detail.get("error") or {}).get("message") or message
            return StepResult(output={"message": message}, partial=True,
                pause=PauseRequest(reason=message, founder_action="/settings/local" if "Coding tools" in message else f"/v/{ctx.venture.id}/builds"))
        view = await firstlook_service.generate_first_look(
            _workspace_id(ctx),
            idea,
            venture_id=ctx.venture.id,
            venture_name=ctx.venture.name,
            archetype=_archetype(ctx),
            audience=_audience(ctx),
            stage="first_look",
            research=research if isinstance(research, dict) else None,
            allow_model=False,
        )
    if is_local() and isinstance(view, dict):
        from urllib.parse import quote

        path = view.get("review_path") or f"/v/{quote(ctx.venture.id, safe='')}/device-work-orders/{quote(view['id'], safe='')}"
        return StepResult(output={"device_order": view}, partial=True,
            pause=PauseRequest(reason="Your page request is with your coding tool. Open Builds to check the result, then resume your setup.",
                               founder_action=path))
    MEM[f"firstlook:{ctx.venture.id}"] = view.to_dict()
    from app.chat.milestone_hooks import first_look_ready
    first_look_ready(ctx.venture.id, view.id, view.copy)
    try:
        mark_idle("designer", ctx.venture.id)
        mark_working("builder", ctx.venture.id, "Wrote the first look into a local git repo.")
        mark_idle("builder", ctx.venture.id)
    except Exception:  # noqa: BLE001
        pass
    detail = [f"Layout {view.template}", f"Colours from {view.brandkit.get('colors', {}).get('family', 'palette')}"]
    return StepResult(
        output={
            "site_version_id": view.id,
            "version": view.version,
            "url": view.url,
            "path": view.path,
            "stage": view.stage,
            "template": view.template,
            "fallback": view.fallback,
            "cost_usd": view.cost_usd,
        },
        detail=detail,
        evidence_url=view.url,
        cost_usd=view.cost_usd,
        tokens={"url": view.url, "template": view.template},
        partial=view.fallback,
        variant_key="firstlook.plain_fallback" if view.fallback else None,
        simulated=not is_local(),
    )


async def narrate_url(ctx: Ctx) -> StepResult:
    """Optional share-line: local tenancy only works on this machine."""
    gen = ctx.out.get("first_look.generate") or MEM.get(f"firstlook:{ctx.venture.id}") or {}
    url = gen.get("url")
    return StepResult(
        output={"url": url, "local_only": True},
        evidence_url=url,
        tokens={"url": url},
        simulated=True,
        variant_key="firstlook.share",
    )


STEPS: list[StepDef] = [
    StepDef(
        "first_look.generate",
        "first_look",
        generate,
        "firstlook.build_page",
        timeout_s=90,
        est_cost_usd=0.015,
    ),
    StepDef(
        "first_look.narrate_url",
        "first_look",
        narrate_url,
        "firstlook.share",
        required=False,
        timeout_s=5,
        est_cost_usd=0.0,
    ),
]
