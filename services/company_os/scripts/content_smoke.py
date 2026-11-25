"""Dry-run smoke test for the content engine loop.

Usage:
    COMPANY_OS_DB_URL=sqlite:////tmp/cosmig/t.db python scripts/content_smoke.py
"""

from __future__ import annotations

import asyncio
import json


async def main() -> None:
    from core.clock import utcnow
    from core.ids import new_id

    from app.content import brief as brief_store
    from app.content import director, engine
    from app.content.oauth import flow
    from app.db.models import ContentAccount
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    wid = desk.id
    print(f"workspace: {wid}")

    saved = brief_store.save(
        workspace_id=wid, data=brief_store.TEMPLATE, source_name="smoke.json"
    )
    print(f"brief v{saved['version']} saved, {len(saved['icp']['segments'])} segment(s)")

    # Stand in for a completed OAuth handshake so the loop has somewhere to post.
    now = utcnow()
    factory = db()
    with factory() as session:
        for platform, urn in (("linkedin", "urn:li:person:smoke"), ("x", "")):
            session.merge(
                ContentAccount(
                    id=new_id("cac"),
                    workspace_id=wid,
                    platform=platform,
                    external_account=f"smoke-{platform}",
                    handle=f"smoke-{platform}",
                    author_urn=urn,
                    status="active",
                    scopes_json="[]",
                    daily_cap=3,
                    health="ok",
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    print(f"accounts: {[a['platform'] for a in flow.list_accounts(wid)]}")

    refill = await director.refill(workspace_id=wid, count=10)
    print(f"topics: {refill}")

    result = await engine.tick(workspace_id=wid)
    print("\ntick summary:")
    print(json.dumps(result, indent=2, default=str))

    posts = engine.recent_posts(wid, limit=10)
    print(f"\n{len(posts)} post(s):")
    for post in posts:
        head = (post["body"] or "").replace("\n", " ")[:90]
        print(f"  [{post['platform']:9} {post['state']:9}] {head}")
        if post["refusal_reason"]:
            print(f"      refused: {post['refusal_reason']}")

    print("\nevents:")
    for event in engine.recent_events(wid, limit=12):
        print(f"  {event['kind']:22} {event['platform'] or '-'}")


if __name__ == "__main__":
    asyncio.run(main())
