"""Legacy topic utilities. New AI planning uses content.generation durable jobs.

Manual topic derivation remains available to explicit callers; it is never a
fallback for failed AI work. Legacy automated entry points fail closed.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import ContentGenerationRun, ContentTopic
from app.state import db

SYSTEM = (
    "You plan content for a founder posting to a specific buyer. You write like "
    "an operator, not a marketer: concrete, specific, and useful on its own. You "
    "never invent statistics, customer counts, or outcomes."
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _extract_json(text: str) -> Any:
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("```")[1] if "```" in body[3:] else body.strip("`")
        body = body.removeprefix("json").strip()
    start = min(
        (i for i in (body.find("{"), body.find("[")) if i >= 0), default=-1
    )
    if start > 0:
        body = body[start:]
    return json.loads(body)


def deterministic_topics(brief: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """Angles derivable from the brief with no model call.

    Pain and objection angles come first: those are the posts the ICP recognizes
    themselves in, and they carry no claim risk.
    """
    segments = (brief.get("icp") or {}).get("segments") or []
    competitors = brief.get("competitors") or []
    product = brief.get("product") or {}
    differentiators = product.get("differentiators") or []

    out: list[dict[str, Any]] = []
    for seg in segments:
        name = str(seg.get("name") or "this segment")
        for pain in seg.get("pains") or []:
            out.append(
                {
                    "angle": f"Why '{pain}' happens and what actually fixes it",
                    "hook": str(pain),
                    "rationale": f"Named pain for {name}; recognition post, no claims.",
                    "icp_segment": name,
                    "intent": "value",
                    "evidence": [f"icp.segments[{name}].pains"],
                    "score": 0.8,
                }
            )
        for objection in seg.get("objections") or []:
            out.append(
                {
                    "angle": f"Steelmanning the objection: {objection}",
                    "hook": str(objection),
                    "rationale": f"Objection held by {name}; answering it earns trust.",
                    "icp_segment": name,
                    "intent": "value",
                    "evidence": [f"icp.segments[{name}].objections"],
                    "score": 0.75,
                }
            )
        for trigger in seg.get("triggers") or []:
            out.append(
                {
                    "angle": f"What to do in the first month after: {trigger}",
                    "hook": str(trigger),
                    "rationale": f"Buying trigger for {name}; timely and practical.",
                    "icp_segment": name,
                    "intent": "value",
                    "evidence": [f"icp.segments[{name}].triggers"],
                    "score": 0.7,
                }
            )

    for comp in competitors:
        for gap in comp.get("weaknesses") or []:
            out.append(
                {
                    "angle": f"The gap category tools leave open: {gap}",
                    "hook": str(gap),
                    # Category-level so the post never reads as a competitor attack.
                    "rationale": f"Weakness recorded for {comp.get('name')}.",
                    "icp_segment": segments[0].get("name") if segments else None,
                    "intent": "value",
                    "evidence": [f"competitors[{comp.get('name')}].weaknesses"],
                    "score": 0.65,
                }
            )

    for diff in differentiators:
        out.append(
            {
                "angle": f"How we do it differently: {diff}",
                "hook": str(diff),
                "rationale": "Registered differentiator; the one promo-shaped angle.",
                "icp_segment": segments[0].get("name") if segments else None,
                "intent": "promo",
                "evidence": ["product.differentiators"],
                "score": 0.5,
            }
        )

    out.sort(key=lambda t: t["score"], reverse=True)
    return out[:count]


async def _llm_topics(
    brief: dict[str, Any], count: int, workspace_id: str, avoid: list[str]
) -> list[dict[str, Any]]:
    raise RuntimeError("content_scoped_generation_required: use the company generation quote and durable job API")


def _recent_angles(workspace_id: str, limit: int = 60) -> list[str]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentTopic)
            .where(ContentTopic.workspace_id == workspace_id)
            .order_by(ContentTopic.created_at.desc())
            .limit(limit)
        ).all()
        return [r.angle for r in rows]


def queued_count(workspace_id: str, *, venture_id: str | None = None, brief_id: str | None = None) -> int:
    factory = db()
    with factory() as session:
        query = select(ContentTopic).where(
                ContentTopic.workspace_id == workspace_id,
                ContentTopic.status == "queued",
            )
        if venture_id:
            if not brief_id:
                return 0
            query = query.join(ContentGenerationRun, ContentTopic.generation_run_id == ContentGenerationRun.id).where(
                ContentGenerationRun.workspace_id == workspace_id,
                ContentGenerationRun.venture_id == venture_id,
                ContentTopic.brief_id == brief_id)
        return len(session.scalars(query).all())


async def refill(
    *, workspace_id: str, count: int = 12
) -> dict[str, Any]:
    raise RuntimeError("content_scoped_generation_required: use the company generation quote and durable job API")


def take_topic(workspace_id: str, *, intent: str | None = None) -> dict[str, Any] | None:
    """Claim the highest-scoring queued topic and mark it drafted."""
    factory = db()
    with factory() as session:
        stmt = select(ContentTopic).where(
            ContentTopic.workspace_id == workspace_id,
            ContentTopic.status == "queued",
        )
        if intent:
            stmt = stmt.where(ContentTopic.intent == intent)
        row = session.scalars(
            stmt.order_by(ContentTopic.score.desc(), ContentTopic.created_at)
        ).first()
        if row is None:
            return None
        row.status = "drafted"
        session.commit()
        session.refresh(row)
        try:
            evidence = json.loads(row.evidence_json or "[]")
        except Exception:
            evidence = []
        return {
            "id": row.id,
            "brief_id": row.brief_id,
            "angle": row.angle,
            "hook": row.hook,
            "rationale": row.rationale,
            "icp_segment": row.icp_segment,
            "intent": row.intent,
            "evidence": evidence,
            "score": row.score,
        }
