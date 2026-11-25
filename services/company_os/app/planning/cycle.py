"""On-demand replan cycle."""

from __future__ import annotations

from typing import Any

from app.planning.generate import generate_next_tasks
from app.planning.store import (
    enqueue_task,
    has_running_task,
    list_tasks,
    next_runnable_task,
    save_tasks,
)


def replan(ctx: Any) -> dict:
    plan = generate_next_tasks(ctx)
    saved = save_tasks(ctx.venture.id, plan["tasks"], source="replan")
    return {"headline": plan["headline"], "tasks": saved}


def refill_unused_work(venture_id: str) -> list[dict]:
    """When the runnable queue is empty, enqueue the next unused playbook slice.

    Does not wipe parked (needs-you) tasks. Titles already done or open are skipped
    so the next five stay about this idea instead of remixing the same five.
    """
    if has_running_task(venture_id) or next_runnable_task(venture_id):
        return []
    from app.db.models import Venture
    from app.planning.dedupe import fingerprint_title
    from app.planning.execute import is_product_work, work_kind
    from app.state import db
    from app.will.slate import _rank, gather_candidates

    unfinished = list_tasks(venture_id, statuses=("open", "running"))
    if any(
        work_kind(str(row.get("title") or ""), row.get("tags") or []) != "research"
        for row in unfinished
    ):
        # Parked landing/waitlist/prototype work is still the job — don't mint
        # another generic discovery slice because a search note "finished".
        return []

    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        idea = ven.raw_idea if ven else None
    used_titles = {
        str(row.get("title") or "")
        for row in list_tasks(
            venture_id,
            statuses=("open", "running", "done", "dismissed", "superseded"),
        )
    }
    unused = [
        item
        for item in gather_candidates(venture_id, idea=idea)
        if item.title not in used_titles
        and fingerprint_title(item.title) not in {
            fingerprint_title(title) for title in used_titles if title
        }
        and is_product_work(item.title, [item.origin, item.capability_id])
    ]
    from app.planning.store import founder_constraints

    blocked = founder_constraints(venture_id)
    if blocked:
        unused = [
            item
            for item in unused
            if not any(needle in item.title.lower() for needle in blocked)
        ]
    if not unused:
        return []
    picked = _rank(unused, venture_id) or unused[:5]
    saved: list[dict] = []
    for item in picked[:5]:
        saved.append(
            enqueue_task(
                venture_id,
                {
                    "title": item.title,
                    "detail": item.expected_outcome,
                    "why_now": item.why_now,
                    "tags": [item.origin, item.capability_id],
                    "est_minutes": {"minutes": 20, "hours": 60, "days": 240}.get(item.effort, 60),
                    "runnable": not item.blocked_by,
                    "rank": item.rank,
                },
                source="autopilot",
            )
        )
    return saved
