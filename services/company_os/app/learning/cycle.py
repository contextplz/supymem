"""Learning loop writes proposals with a diff; never mutates live config (D-22)."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.db.supymem_models import FounderRequest, LearningProposal
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def run_cycle(_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    with factory() as session:
        rows = list(session.scalars(select(FounderRequest)))
        by_class = Counter(r.classification for r in rows)
        by_intent = Counter(r.intent_category for r in rows)
        out_caps = [
            r.failure_reason
            for r in rows
            if r.classification == "out" and r.failure_reason
        ]
        top_out = Counter(out_caps).most_common(1)
        summary = (
            f"{len(rows)} founder requests; "
            f"in={by_class.get('in', 0)} out={by_class.get('out', 0)} "
            f"conditional={by_class.get('conditional', 0)}"
        )
        patch = {
            "file": "config/platform_capabilities.yaml",
            "before": {"note": "no live mutation"},
            "after": {
                "promote": top_out[0][0] if top_out else None,
                "reason": "most common out-of-scope failure_reason",
            },
            "intents": dict(by_intent),
            "classifications": dict(by_class),
        }
        row = LearningProposal(
            id=new_id("lrn"),
            kind="capability_maturity",
            summary=summary,
            patch=json.dumps(patch, sort_keys=True),
            state="proposed",
            created_at=now,
        )
        session.add(row)
        session.commit()
        return {"id": row.id, "summary": summary, "applied": False}


def list_proposals() -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = list(session.scalars(select(LearningProposal).order_by(LearningProposal.created_at.desc())))
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "summary": r.summary,
                "patch": json.loads(r.patch or "{}"),
                "state": r.state,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def set_state(proposal_id: str, state: str) -> dict[str, Any]:
    if state not in {"proposed", "accepted", "rejected"}:
        raise ValueError("bad_state")
    factory = db()
    with factory() as session:
        row = session.get(LearningProposal, proposal_id)
        if row is None:
            raise KeyError("proposal")
        row.state = state
        session.commit()
        return {"id": row.id, "state": row.state, "applied": False}
