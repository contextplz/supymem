"""Marketing execution spine smoke test (dry-run).

Usage:
    COMPANY_OS_DB_URL=sqlite:////tmp/cos_mkt.db \\
    CONTENT_DRY_RUN=true \\
    python scripts/marketing_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta


async def main() -> None:
    os.environ.setdefault("CONTENT_DRY_RUN", "true")
    os.environ.setdefault("MARKETING_EXECUTION_ENABLED", "true")

    from unittest.mock import AsyncMock, patch

    from core.clock import utcnow
    from core.ids import new_id

    from app.content import brief as brief_store
    from app.content import director
    from app.db.models import ContentAccount, MarketingJob
    from app.marketing import store
    from app.marketing.executor import run_tick
    from app.marketing.strategist import create_plan_from_brief
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    wid = desk.id
    print(f"workspace: {wid}")

    brief_store.save(workspace_id=wid, data=brief_store.TEMPLATE, source_name="mkt-smoke.json")
    now = utcnow()
    factory = db()
    with factory() as session:
        session.merge(
            ContentAccount(
                id=new_id("cac"),
                workspace_id=wid,
                platform="linkedin",
                external_account="smoke-linkedin",
                handle="smoke-linkedin",
                author_urn="urn:li:person:smoke",
                status="active",
                scopes_json="[]",
                daily_cap=12,
                health="ok",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    await director.refill(workspace_id=wid, count=8)
    plan_out = create_plan_from_brief(
        workspace_id=wid, horizon_days=30, compile_days=3, activate=True
    )
    print(f"plan: {plan_out['plan']['id']} jobs={plan_out['jobs_created']}")

    # Pull create/visual/publish for LinkedIn into the past so the tick runs them.
    jobs = store.list_jobs(wid, plan_id=plan_out["plan"]["id"], limit=200)
    with factory() as session:
        for job in jobs:
            if job["platform"] != "linkedin":
                continue
            if job["kind"] not in {"create_content", "create_visual", "publish_content"}:
                continue
            row = session.get(MarketingJob, job["id"])
            if row is None:
                continue
            row.scheduled_for = now - timedelta(minutes=5)
            row.updated_at = now
        session.commit()

    # Dry-run still hits Warden; allow it for the smoke path.
    with patch(
        "app.content.engine._warden_allows",
        new=AsyncMock(return_value=(True, "smoke")),
    ):
        for i in range(10):
            summary = await run_tick(workspace_id=wid, limit=4)
            print(
                f"tick {i+1}: "
                + json.dumps(
                    {k: summary[k] for k in ("done", "failed", "blocked", "due")},
                    default=str,
                )
            )
            if not store.due_jobs(wid, limit=1):
                # Allow one more tick after visual unlocks publish.
                if i >= 2:
                    break

    counts = store.job_counts(wid, plan_id=plan_out["plan"]["id"])
    print("job_counts:", counts)
    done = [j for j in store.list_jobs(wid, status="done", limit=20)]
    for j in done[:8]:
        proof = j.get("proof") or {}
        print(
            f"  [{j['kind']:16}] proof_id={proof.get('external_id') or proof.get('content_post_id') or proof.get('visual_path')}"
        )
    blocked = store.list_jobs(wid, status="blocked", limit=10)
    for j in blocked:
        print(f"  BLOCKED {j['kind']}: {j.get('block_reason')}")


if __name__ == "__main__":
    asyncio.run(main())
