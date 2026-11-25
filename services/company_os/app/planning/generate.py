"""Compatibility adapter from legacy planning callers to the Will Slate."""

from __future__ import annotations

from typing import Any

from app.will.slate import generate_slate


def generate_next_tasks(ctx: Any) -> dict[str, Any]:
    idea = getattr(ctx.venture, "raw_idea", None) or getattr(ctx, "company", None)
    slate = generate_slate(ctx.venture.id, because="planning requested next work", idea=idea)
    tasks = [
        {
            "title": item.title,
            "detail": item.expected_outcome,
            "why_now": item.why_now,
            "tags": [item.origin, item.capability_id],
            "est_minutes": {"minutes": 20, "hours": 60, "days": 240}[item.effort],
            "runnable": not item.blocked_by,
            "rank": item.rank,
            "slate_id": slate.id,
        }
        for item in slate.items
    ]
    return {
        "tasks": tasks,
        "headline": f"Five evidence-backed actions for {getattr(ctx, 'company', 'this venture')}.",
        "reasoning": slate.generated_because,
        "slate": slate.model_dump(mode="json"),
    }
