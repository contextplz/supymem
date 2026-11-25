"""Phase 6 — handoff (tasks, recap, chat)."""

from __future__ import annotations

import json
from pathlib import Path

from app.chat.store import append_message
from app.db.models import VentureDocument
from app.onboarding.registry import StepResult
from app.onboarding.state import Ctx, persist_run
from app.planning.generate import generate_next_tasks
from app.planning.store import save_tasks
from app.state import MEM, db
from core.clock import utcnow
from core.ids import new_id


def _venture_root(venture_id: str) -> Path:
    from app.config import data_root

    return data_root() / "ventures" / venture_id


async def human_tasks(ctx: Ctx) -> StepResult:
    from app.concierge.human import ensure_human_task

    tasks = [
        ensure_human_task(
            ctx.venture.id,
            title="Connect GitHub for a real repository",
            why="Your repository is local until a GitHub account is connected.",
            provider="github",
            steps=[{"text": "Open Settings and connect GitHub", "url": None}],
        ),
        ensure_human_task(
            ctx.venture.id,
            title="Share the social handles we should post from",
            why="Nothing posts until we know which X, Instagram, or LinkedIn accounts to use.",
            provider="social",
            priority=2,
            steps=[{"text": "Add handles in Settings, or reply here with @usernames", "url": None}],
        ),
    ]
    count = len(tasks)
    return StepResult(
        output={"tasks": tasks, "count": count},
        tokens={"count": count},
        variant_key="handoff.human_tasks" if count else None,
    )


async def plan_next_tasks(ctx: Ctx) -> StepResult:
    plan = generate_next_tasks(ctx)
    headline = plan["headline"]
    ctx.run.next_up_headline = headline
    persist_run(ctx.run)
    return StepResult(
        output={
            "tasks": plan["tasks"],
            "headline": headline,
            "reasoning": plan.get("reasoning"),
        },
        tokens={"count": len(plan["tasks"])},
        detail=[t["title"] for t in plan["tasks"]],
    )


async def queue_tasks(ctx: Ctx) -> StepResult:
    planned = (ctx.out.get("handoff.plan_next_tasks") or {}).get("tasks") or []
    saved = save_tasks(ctx.venture.id, planned, source="handoff")
    return StepResult(output={"task_ids": [t["id"] for t in saved], "tasks": saved})


async def recap(ctx: Ctx) -> StepResult:
    home = _venture_root(ctx.venture.id) / "docs"
    home.mkdir(parents=True, exist_ok=True)
    charter = (ctx.out.get("commit.lock_charter") or {}).get("content") or {}
    website = MEM.get(f"website:{ctx.venture.id}") or {}
    body = (
        f"# Recap\n\n"
        f"**{ctx.company}** is set up.\n\n"
        f"- Plan: {charter.get('one_liner', ctx.venture.raw_idea[:80])}\n"
        f"- Site: {website.get('url', 'preview pending')}\n"
    )
    path = home / "recap.md"
    path.write_text(body, encoding="utf-8")
    doc_id = new_id("doc")
    now = utcnow()
    factory = db()
    with factory() as session:
        session.add(
            VentureDocument(
                id=doc_id,
                venture_id=ctx.venture.id,
                kind="recap",
                title="Setup recap",
                path=str(path),
                body_md=body,
                provenance_json=json.dumps({"source": "handoff"}),
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return StepResult(output={"document_id": doc_id})


async def welcome_email(ctx: Ctx) -> StepResult:
    """Email is out of MVP scope — record a local draft only, never send."""
    email = (ctx.out.get("provision.email") or {}).get("display") or f"{ctx.company.lower().replace(' ', '')}@companyos.local"
    return StepResult(
        output={"email": email, "draft": True, "sent": False, "skipped_reason": "email_out_of_mvp"},
        tokens={"email": email, "company": ctx.company},
        downgraded=True,
        variant_key="handoff.welcome_email",
    )


async def done(ctx: Ctx) -> StepResult:
    # Advance past charter_review so the UI doesn't stay stuck on "Reviewing plan".
    try:
        from core.enums import VentureStage

        factory = db()
        with factory() as session:
            from app.db.models import Venture

            ven = session.get(Venture, ctx.venture.id)
            if ven:
                ven.stage = VentureStage.LIVE.value
                ven.updated_at = utcnow()
                session.commit()
    except Exception:  # noqa: BLE001 — handoff must still finish
        pass
    from app.chat.next_moves import compose, post
    from app.core.local_mode import is_local

    if is_local():
        from app.ventures.stage import get_stage

        try:
            headline = get_stage(ctx.venture.id)["label"]
        except KeyError:
            headline = "Shaping your idea"
        return StepResult(output={"recap": post(ctx.venture.id), "headline": headline},
                          tokens={"company": ctx.company})

    next_up = compose(ctx.venture.id)
    headline = next_up["stage"]
    append_message(
        ctx.venture.id,
        role="assistant",
        kind=next_up["kind"],
        body_md=next_up["body_md"],
        actions=next_up["actions"],
        citations=next_up["citations"],
        idempotency_key=f"handoff-recap:{ctx.run.id}",
    )
    return StepResult(
        output={"recap": True, "headline": headline},
        tokens={"company": ctx.company},
    )
