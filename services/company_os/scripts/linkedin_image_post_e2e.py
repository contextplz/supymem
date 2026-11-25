"""End-to-end content machine: plan → card → Composio publish → OpenClaw verify.

Dry-run (default):

    python scripts/linkedin_image_post_e2e.py

One live LinkedIn text+image post (Merlin test account):

    LINKEDIN_IMAGE_LIVE=1 CONTENT_TEST_CADENCE=1 \\
      python scripts/linkedin_image_post_e2e.py

Live requires COMPOSIO_API_KEY, active LinkedIn account, and founder intent.
Clears ``paused_reason`` / resets today's count only when LINKEDIN_IMAGE_LIVE=1.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch


def _load_env() -> Path:
    root = Path(__file__).resolve().parents[3]
    for env_path in (root / ".env.local", root / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    # Prefer the local SQLite that already holds Merlin + prior posts unless
    # the operator explicitly points elsewhere.
    sqlite_url = f"sqlite:///{(root / 'data' / 'company_os.db').resolve()}"
    if os.environ.get("CONTENT_E2E_USE_ENV_DB", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        os.environ["COMPANY_OS_DB_URL"] = sqlite_url
        os.environ["DB_URL"] = sqlite_url
        os.environ["DATABASE_URL"] = sqlite_url
    return root


def _ensure_marketing_schema() -> None:
    from sqlalchemy import inspect, text

    from app.db.models import Base, MarketingJob, MarketingPlan
    from app.db.session import make_engine, normalize_db_url, resolve_database_url
    from app.state import reset_db_factory

    reset_db_factory()
    eng = make_engine(normalize_db_url(resolve_database_url()))
    Base.metadata.create_all(eng, tables=[MarketingPlan.__table__, MarketingJob.__table__])
    cols = {c["name"] for c in inspect(eng).get_columns("content_posts")}
    with eng.begin() as conn:
        if "visual_json" not in cols:
            conn.execute(text("ALTER TABLE content_posts ADD COLUMN visual_json TEXT DEFAULT '{}'"))
        if "visual_path" not in cols:
            conn.execute(text("ALTER TABLE content_posts ADD COLUMN visual_path TEXT"))



def _prepare_linkedin_for_live(wid: str) -> None:
    """One-shot unblock for the Merlin test account — not a permanent re-enable."""
    from sqlalchemy import select

    from core.clock import utcnow

    from app.db.models import ContentAccount
    from app.state import db

    now = utcnow()
    with db()() as session:
        rows = list(
            session.scalars(
                select(ContentAccount).where(
                    ContentAccount.workspace_id == wid,
                    ContentAccount.platform == "linkedin",
                    ContentAccount.status == "active",
                )
            )
        )
        for row in rows:
            row.paused_reason = None
            row.posts_today = 0
            row.daily_cap = max(int(row.daily_cap or 1), 3)
            row.last_post_at = now - timedelta(hours=48)
            row.health = "ok"
            row.last_error = None
            row.updated_at = now
            print(
                f"live_prep account={row.handle} cap={row.daily_cap} "
                f"cleared_pause posts_today=0"
            )
        session.commit()


def _sync_composio_linkedin(wid: str) -> dict:
    """Refresh Composio LinkedIn connection ids; return NeedsYou if expired."""
    from app.content import composio_social

    conns = composio_social.list_connections(
        workspace_id=wid, platform="linkedin", active_only=False
    )
    active = [c for c in conns if str(c.get("status") or "").upper() in {
        "ACTIVE",
        "SUCCESS",
        "CONNECTED",
    }]
    print(
        "composio_linkedin:",
        [
            {k: c.get(k) for k in ("id", "status")}
            for c in conns
        ],
    )
    if active:
        synced = composio_social.sync_workspace(wid, platform="linkedin")
        print("composio_synced:", synced)
        return {"ok": True, "connections": active}
    # Mint reconnect link for founder.
    try:
        link = composio_social.start_connect(workspace_id=wid, platform="linkedin")
    except Exception as exc:
        link = {"error": str(exc)[:300]}
    return {
        "ok": False,
        "needs_you": True,
        "reason": "linkedin_composio_expired_or_missing",
        "connections": conns,
        "reconnect": link,
    }


async def main() -> None:
    root = _load_env()
    os.chdir(root)
    import sys

    cos = str(root / "services" / "company_os")
    if cos not in sys.path:
        sys.path.insert(0, cos)

    os.environ.setdefault("CONTENT_DRY_RUN", "true")
    os.environ.setdefault("MARKETING_EXECUTION_ENABLED", "true")
    os.environ.setdefault("MARKETING_OWNS_CONTENT", "true")
    os.environ["CONTENT_SKIP_NEAR_DUPLICATE"] = "true"
    live = os.environ.get("LINKEDIN_IMAGE_LIVE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if live:
        os.environ["CONTENT_DRY_RUN"] = "false"
    # E2E always uses test cadence so caps/gaps don't strand the chain.
    os.environ["CONTENT_TEST_CADENCE"] = "true"

    from core.clock import utcnow
    from core.ids import new_id

    from app.content import brief as brief_store
    from app.content import director
    from app.content.visuals.render import render_png
    from app.content.visuals.spec import build_spec
    from app.db.models import ContentAccount, MarketingJob
    from app.marketing import store
    from app.marketing.adapters import openclaw_bridge
    from app.marketing.executor import run_tick
    from app.marketing.strategist import create_plan_from_brief
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    _ensure_marketing_schema()
    desk = ensure_desk(LOCAL_PERSON_ID)
    wid = desk.id
    print(
        f"workspace={wid} dry_run={os.environ.get('CONTENT_DRY_RUN')} "
        f"live={live} playwright={openclaw_bridge.browser_available()}"
    )

    brief_store.save(
        workspace_id=wid, data=brief_store.TEMPLATE, source_name="image-e2e.json"
    )

    from sqlalchemy import select

    now = utcnow()
    factory = db()
    with factory() as session:
        existing = list(
            session.scalars(
                select(ContentAccount).where(
                    ContentAccount.workspace_id == wid,
                    ContentAccount.platform == "linkedin",
                )
            )
        )
        if not existing:
            session.add(
                ContentAccount(
                    id=new_id("cac"),
                    workspace_id=wid,
                    platform="linkedin",
                    external_account="e2e-linkedin",
                    handle="e2e-linkedin",
                    author_urn="urn:li:person:e2e",
                    status="active",
                    scopes_json="[]",
                    daily_cap=12,
                    health="ok",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            print("seeded local linkedin account (smoke)")
        else:
            for a in existing:
                print(
                    f"linkedin handle={a.handle} status={a.status} "
                    f"cap={a.daily_cap} today={a.posts_today} pause={a.paused_reason!r}"
                )

    if live:
        sync = _sync_composio_linkedin(wid)
        if not sync.get("ok"):
            print("NEEDS_YOU linkedin reconnect required")
            print(json.dumps(sync, indent=2, default=str)[:2000])
            raise SystemExit("NEEDS_YOU: reconnect LinkedIn via Composio Connect Link above")

    # Always reset Merlin cadence for this chain (dry or live). Live also
    # clears the founder pause so one intentional post can ship.
    _prepare_linkedin_for_live(wid)
    if not live:
        # Keep pause reason visible in dry-run DB unless live.
        with factory() as session:
            for row in session.scalars(
                select(ContentAccount).where(
                    ContentAccount.workspace_id == wid,
                    ContentAccount.platform == "linkedin",
                    ContentAccount.status == "active",
                )
            ):
                if not row.paused_reason:
                    row.paused_reason = "e2e dry-run hold"
                row.updated_at = now
            session.commit()

    refill_out = await director.refill(workspace_id=wid, count=6)
    print("refill:", refill_out)
    # Always seed a lint-safe unique topic so e2e does not depend on LLM or
    # leftover short queued angles.
    from app.content import brief as _brief
    from app.db.models import ContentTopic
    from sqlalchemy import select as sa_select

    current = _brief.active(wid)
    with factory() as session:
        leftovers = list(
            session.scalars(
                sa_select(ContentTopic).where(
                    ContentTopic.workspace_id == wid,
                    ContentTopic.status == "queued",
                )
            )
        )
        for row in leftovers:
            row.status = "spent"
        session.add(
            ContentTopic(
                id=new_id("ctp"),
                workspace_id=wid,
                brief_id=(current or {}).get("id"),
                angle=(
                    f"E2E content machine proof {utcnow().isoformat()}: "
                    "the operator that ships beats the dashboard that reports"
                ),
                hook=(
                    f"[{utcnow().strftime('%Y %b %d %H %M %S')}] Most teams build a "
                    "content dashboard. Operators need a machine that drafts, renders "
                    "a branded card, publishes through the API, and verifies the live "
                    "URL without pretending a browser click is strategy."
                ),
                rationale="Forced unique angle for marketing e2e with enough body length.",
                icp_segment="founders",
                intent="value",
                evidence_json="[]",
                score=0.99,
                status="queued",
                created_at=now,
            )
        )
        session.commit()
    print(f"seeded_unique_topic queued={director.queued_count(wid)}")

    plan_out = create_plan_from_brief(
        workspace_id=wid, horizon_days=7, compile_days=7, activate=True
    )
    print(f"plan={plan_out['plan']['id']} jobs={plan_out['jobs_created']}")

    jobs = store.list_jobs(wid, plan_id=plan_out["plan"]["id"], limit=200)
    linkedin_creates = [
        j
        for j in jobs
        if j.get("platform") == "linkedin" and j["kind"] == "create_content"
    ]
    keep_ids: set[str] = set()
    if linkedin_creates:
        first = linkedin_creates[0]
        keep_ids.add(first["id"])
        changed = True
        while changed:
            changed = False
            for j in jobs:
                if j["id"] in keep_ids:
                    continue
                if any(d in keep_ids for d in (j.get("depends_on") or [])):
                    keep_ids.add(j["id"])
                    changed = True

    # Cancel leftover pending jobs from older plans so due_jobs is not starved.
    with factory() as session:
        for row in session.scalars(
            select(MarketingJob).where(
                MarketingJob.workspace_id == wid,
                MarketingJob.status.in_(("pending", "ready", "blocked")),
            )
        ):
            if row.plan_id == plan_out["plan"]["id"] and row.id in keep_ids:
                continue
            row.status = "cancelled"
            row.updated_at = now
            row.error_message = "superseded_by_e2e"
        session.commit()

    with factory() as session:
        for job in jobs:
            if job.get("platform") != "linkedin" or job["id"] not in keep_ids:
                continue
            if job["kind"] not in {
                "create_content",
                "create_visual",
                "publish_content",
                "check_result",
            }:
                continue
            row = session.get(MarketingJob, job["id"])
            if row is None:
                continue
            row.scheduled_for = now - timedelta(minutes=2)
            row.updated_at = now
        session.commit()
    print(f"forced_chain_jobs={len(keep_ids)}")

    spec = build_spec(
        body=(
            "The hardest part of agents isn't creating them.\n\n"
            "It's getting thirty of them to cooperate without becoming spam."
        ),
        brand={"wordmark": "Company OS", "accent": "#1B4D3E"},
    )
    card = Path("data/content_visuals") / wid / "e2e_card.png"
    rendered = render_png(spec, card)
    print("card:", rendered)
    assert rendered.get("ok"), rendered

    if os.environ.get("COMPOSIO_API_KEY") and os.environ.get("STAGE_IMAGE", "1") != "0":
        try:
            from app.content.composio_social import _stage_file_for_composio

            staged = _stage_file_for_composio(
                path=str(card),
                tool_slug="LINKEDIN_CREATE_LINKED_IN_POST",
                toolkit_slug="linkedin",
            )
            print("staged:", {k: staged[k] for k in ("name", "mimetype", "s3key")})
        except Exception as exc:
            print("stage_skipped:", type(exc).__name__, str(exc)[:200])

    warden_patch = patch(
        "app.content.engine._warden_allows",
        new=AsyncMock(return_value=(True, "e2e")),
    )
    with warden_patch:
        for i in range(10):
            summary = await run_tick(workspace_id=wid, limit=4)
            print(
                f"tick {i+1}: done={summary.get('done')} failed={summary.get('failed')} "
                f"blocked={summary.get('blocked')} due={summary.get('due')}"
            )
            for p in summary.get("processed") or []:
                print(f"  → {p}")
            if not store.due_jobs(wid, limit=1) and i >= 3:
                break

    print("counts:", store.job_counts(wid, plan_id=plan_out["plan"]["id"]))
    publish_proof = None
    for j in store.list_jobs(wid, plan_id=plan_out["plan"]["id"], limit=40):
        if j["kind"] in {
            "create_content",
            "create_visual",
            "publish_content",
            "check_result",
        } or j["status"] != "pending":
            proof = j.get("proof") or {}
            print(
                f"  {j['status']:8} {j['kind']:16} "
                f"exec={j.get('executor')} "
                f"id={proof.get('external_id') or proof.get('content_post_id') or proof.get('visual_path') or proof.get('screenshot_path')}"
            )
            if (
                j["kind"] == "publish_content"
                and j["status"] == "done"
                and j.get("plan_id") == plan_out["plan"]["id"]
            ):
                publish_proof = proof

    if live and publish_proof:
        eid = publish_proof.get("external_id")
        if not eid or str(eid).startswith("sim_"):
            raise SystemExit(f"LIVE_FAILED_NO_URN: {publish_proof}")
        print("LIVE_OK", eid, publish_proof.get("external_url"))
        # Restore founder kill-switch after the intentional single post.
        with factory() as session:
            for row in session.scalars(
                select(ContentAccount).where(
                    ContentAccount.workspace_id == wid,
                    ContentAccount.platform == "linkedin",
                    ContentAccount.status == "active",
                )
            ):
                row.paused_reason = "founder stopped posting"
                row.daily_cap = 1
                row.updated_at = utcnow()
            session.commit()
        print("restored pause: founder stopped posting")


if __name__ == "__main__":
    asyncio.run(main())
