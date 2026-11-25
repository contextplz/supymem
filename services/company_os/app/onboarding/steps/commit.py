"""Phase 3 — commit (mission, charter, team)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.charter.synthesize import lock_charter, synthesize_charter
from app.db.models import Charter, Venture, VentureDocument
from app.onboarding.registry import StepResult
from app.onboarding.state import Ctx
from app.org.lifecycle import materialize_org
from app.state import MEM, db
from core.clock import utcnow
from core.enums import VentureStage
from core.ids import new_id


def _venture_root(venture_id: str) -> Path:
    return Path(__file__).resolve().parents[5] / "data" / "ventures" / venture_id


def _answers_for_charter(ctx: Ctx) -> dict:
    answers = dict(MEM.get(f"answers:{ctx.venture.id}") or {})
    # Map option ids to labels via questions
    questions = MEM.get(f"questions:{ctx.venture.id}") or []
    by_id = {q["id"]: q for q in questions}
    mapped: dict = {}
    for k, v in answers.items():
        if k.startswith("_"):
            continue  # internal metadata such as _answer_provenance, never charter content
        q = by_id.get(k)
        if q:
            opt = next((o for o in q.get("options") or [] if o.get("id") == v), None)
            slot = q.get("slot_key") or k
            mapped[slot] = (opt or {}).get("label") or v
            mapped[k] = v
        else:
            mapped[k] = v
    return mapped


async def mission(ctx: Ctx) -> StepResult:
    idea = ctx.venture.raw_idea.strip()
    one = idea.split(".")[0][:160]
    body = (
        f"# Mission\n\n{ctx.company} exists to {one[0].lower() + one[1:] if one else idea}.\n\n"
        f"We build for people who need this now, not someday.\n"
    )
    return StepResult(output={"mission": body, "quote": one})


def _reuse_locked_charter(venture_id: str) -> dict | None:
    """Return an already-locked charter so a timed-out refine cannot fail resume."""
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        row = session.get(Charter, ven.active_charter_id) if ven and ven.active_charter_id else None
        if row is None:
            row = session.scalars(
                select(Charter)
                .where(Charter.venture_id == venture_id, Charter.locked == 1)
                .order_by(Charter.created_at.desc())
            ).first()
        if not row:
            return None
        try:
            content = json.loads(row.content_json)
        except json.JSONDecodeError:
            content = {}
        return {
            "id": row.id,
            "version": row.version,
            "parent_version": row.parent_version,
            "checksum": row.checksum,
            "content_json": row.content_json,
            "slots_snapshot_json": row.slots_snapshot_json,
            "dissent_json": row.dissent_json,
            "content": content,
        }


async def lock_charter_step(ctx: Ctx) -> StepResult:
    classification = ctx.out.get("understand.classify") or MEM.get(f"brief:{ctx.venture.id}", {}).get(
        "classification"
    ) or {}
    reused = _reuse_locked_charter(ctx.venture.id)
    if reused:
        locked = reused
        content = reused["content"]
    else:
        answers = _answers_for_charter(ctx)
        content = synthesize_charter(
            idea=ctx.venture.raw_idea,
            classification=classification,
            answers=answers,
        )
        locked = lock_charter(
            venture_id=ctx.venture.id,
            content=content,
            slots_snapshot=answers,
        )
        factory = db()
        with factory() as session:
            row = Charter(
                id=locked["id"],
                venture_id=ctx.venture.id,
                version=locked["version"],
                parent_version=locked["parent_version"],
                locked=1,
                content_json=locked["content_json"],
                checksum=locked["checksum"],
                slots_snapshot_json=locked["slots_snapshot_json"],
                dissent_json=locked["dissent_json"],
                created_at=utcnow(),
                locked_at=utcnow(),
            )
            session.add(row)
            ven = session.get(Venture, ctx.venture.id)
            if ven:
                ven.active_charter_id = row.id
                ven.stage = VentureStage.CHARTER_REVIEW.value
                ven.updated_at = utcnow()
            session.commit()
    MEM[f"charter:{ctx.venture.id}"] = {**locked, "content": content}

    # Site refine is best-effort. It must not time out a lock that already committed.
    wid = getattr(ctx.run, "workspace_id", None) or getattr(ctx.venture, "workspace_id", None)
    from app.core.local_mode import is_local
    if wid and not is_local():
        try:
            from app.firstlook.admission import capture_onboarding_intent
            from app.firstlook.service import refine_from_charter

            view = await asyncio.wait_for(
                refine_from_charter(
                    wid,
                    ctx.venture.raw_idea or "",
                    {"content": content},
                    venture_id=ctx.venture.id,
                    venture_name=ctx.company,
                    archetype=(classification or {}).get("build_archetype"),
                    generation_intent=capture_onboarding_intent(ctx),
                ),
                timeout=25,
            )
            MEM[f"firstlook:{ctx.venture.id}"] = {
                "path": getattr(view, "path", None),
                "url": getattr(view, "url", None),
                "stage": getattr(view, "stage", "refined"),
                "version": getattr(view, "version", None),
            }
            if ctx.narrator:
                await ctx.narrator.done(
                    "firstlook.refine",
                    tokens={"stage": "refined"},
                )
        except Exception:  # noqa: BLE001 — lock already persisted
            pass

    return StepResult(output={"charter_id": locked["id"], "checksum": locked["checksum"], "content": content})


async def roadmap(ctx: Ctx) -> StepResult:
    milestones = [
        "Ship a usable landing page and waitlist",
        "Onboard the first ten users and learn",
        "Close the loop with a paid offer or clear next bet",
    ]
    body = "# Roadmap\n\n" + "\n".join(f"{i+1}. {m}" for i, m in enumerate(milestones)) + "\n"
    return StepResult(output={"milestones": milestones, "body": body}, tokens={"count": len(milestones)})


async def file_documents(ctx: Ctx) -> StepResult:
    home = _venture_root(ctx.venture.id) / "docs"
    home.mkdir(parents=True, exist_ok=True)
    mission_body = (ctx.out.get("commit.mission") or {}).get("mission") or "# Mission\n"
    roadmap_body = (ctx.out.get("commit.roadmap") or {}).get("body") or "# Roadmap\n"
    market = ctx.out.get("understand.research_market") or {}
    market_body = (
        "# Market research\n\n"
        f"Demand: {market.get('demand', 'unknown')}\n\n"
        + "\n".join(f"- {c}" for c in (market.get("competitors") or ["unknown"]))
        + "\n"
    )
    docs = [
        ("mission", "Mission", mission_body),
        ("roadmap", "Roadmap", roadmap_body),
        ("market", "Market research", market_body),
    ]
    ids = []
    factory = db()
    now = utcnow()
    with factory() as session:
        for kind, title, body in docs:
            path = home / f"{kind}.md"
            path.write_text(body, encoding="utf-8")
            doc_id = new_id("doc")
            session.add(
                VentureDocument(
                    id=doc_id,
                    venture_id=ctx.venture.id,
                    kind=kind,
                    title=title,
                    path=str(path),
                    body_md=body,
                    provenance_json=json.dumps({"source": "onboarding"}),
                    created_at=now,
                    updated_at=now,
                )
            )
            ids.append({"id": doc_id, "kind": kind, "title": title})
        session.commit()
    MEM[f"documents:{ctx.venture.id}"] = ids
    return StepResult(output={"documents": ids})


def _role_list_phrase(roles: list[str]) -> str:
    if not roles:
        return "a small team"
    if len(roles) == 1:
        return f"a {roles[0]}"
    if len(roles) == 2:
        return f"a {roles[0]} and a {roles[1]}"
    return "a " + ", a ".join(roles[:-1]) + f" and a {roles[-1]}"


async def compose_team(ctx: Ctx) -> StepResult:
    charter = (ctx.out.get("commit.lock_charter") or {}).get("content") or {}
    brief = MEM.get(f"brief:{ctx.venture.id}") or {}
    result = materialize_org(
        venture_id=ctx.venture.id,
        charter=charter,
        brief=brief,
        stage="org_planning",
    )
    from app.org.summary import persist_org

    persist_org(ctx.venture.id, result)
    roles = [a.get("role") or a.get("name") for a in result.get("agents") or []]
    roles = [r.replace("_", " ") for r in roles if r]
    capped = len(result.get("deferred") or [])
    if capped and ctx.narrator:
        await ctx.narrator.done(
            "commit.team_capped",
            tokens={"count": capped, "n": result.get("agent_count", 0)},
        )
    return StepResult(
        output=result,
        tokens={"n": result.get("agent_count", 0), "role_list": _role_list_phrase(roles)},
        detail=roles,
    )


async def hire(ctx: Ctx) -> StepResult:
    org = ctx.out.get("commit.compose_team") or MEM.get(f"org:{ctx.venture.id}") or {}
    agents = org.get("agents") or []
    # Activate non-orchestrator for building stage
    from app.org.registry import registry

    for a in agents:
        inst = registry.get_instance(a["id"]) if a.get("id") else None
        if inst and inst.status == "quarantined":
            inst.status = "active"
            a["status"] = "active"
    if agents:
        from app.org.summary import persist_org

        persist_org(ctx.venture.id, {**org, "agents": agents, "agent_count": len(agents)})
    active = sum(1 for a in agents if a.get("status") == "active")
    return StepResult(output={"hired": len(agents), "active": active, "agents": agents})
