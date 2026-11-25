"""Charter synthesize, lock, get."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.api.schemas import AnswerBody
from app.charter.current import current_locked_charter
from app.charter.synthesize import lock_charter, synthesize_charter
from app.core.stages import transition
from app.db.models import Charter, Venture
from app.db.session import serialize_writes
from app.intake.normalizer import classify_deterministic
from app.state import MEM, db
from core.canonical import canonical_json
from core.clock import utcnow
from core.enums import VentureStage
from core.errors import IllegalTransition

router = APIRouter(tags=["charter"])


def _lock_receipt(locked: dict) -> dict:
    return {
        "charter_id": locked["id"],
        "version": locked["version"],
        "checksum": locked["checksum"],
        "content": locked["content"],
        "stage": VentureStage.CHARTER_REVIEW.value,
    }


@router.post("/ventures/{venture_id}/charter/lock")
def charter_lock(venture_id: str, payload: AnswerBody):
    factory = db()
    with factory() as session:
        serialize_writes(session, key=f"charter:{venture_id}")
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        answers = dict(payload.answers)
        if payload.other_verbatim:
            answers["founder_other_verbatim"] = dict(payload.other_verbatim)
            for k, v in payload.other_verbatim.items():
                answers.setdefault(k, v)
        if ven.active_charter_id:
            canonical = current_locked_charter(session, venture_id, workspace_id=ven.workspace_id)
            if canonical is not None:
                try:
                    saved_inputs = [json.loads(canonical["slots_snapshot_json"]), json.loads(canonical["dissent_json"])]
                except ValueError:
                    saved_inputs = None
                provenance = canonical["content"].get("provenance", {})
                if isinstance(provenance, dict) and canonical_json(saved_inputs) == canonical_json([answers, payload.dissent]):
                    saved_verbatim = {name: entry.get("value") for name, entry in provenance.items()
                                      if isinstance(entry, dict) and entry.get("source") == "founder_words"}
                    if saved_verbatim == payload.other_verbatim:
                        return _lock_receipt(canonical)
            raise HTTPException(409, {"error": {
                "code": "charter_locked",
                "message": "You already locked this charter. Open your saved charter on your venture page to review it.",
            }})
        brief_meta = MEM.get(f"brief:{venture_id}") or {}
        classification = brief_meta.get("classification") or classify_deterministic(ven.raw_idea)
        content = synthesize_charter(
            idea=ven.raw_idea,
            classification=classification,
            answers=answers,
            dissent=payload.dissent,
        )
        content.setdefault("provenance", {})
        for k, v in payload.other_verbatim.items():
            content["provenance"][k] = {"source": "founder_words", "value": v}
        locked = lock_charter(
            venture_id=venture_id,
            content=content,
            slots_snapshot=answers,
            dissent=payload.dissent,
        )
        row = Charter(
            id=locked["id"],
            venture_id=venture_id,
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
        ven.active_charter_id = row.id
        try:
            ven.stage = transition(ven.stage, VentureStage.CHARTER_REVIEW).value
        except IllegalTransition:
            ven.stage = VentureStage.CHARTER_REVIEW.value
        ven.updated_at = utcnow()
        session.commit()
        locked["content"] = content
        MEM[f"charter:{venture_id}"] = locked
        return _lock_receipt(locked)


@router.get("/ventures/{venture_id}/charter")
def get_charter(venture_id: str):
    # Preserve the hosted store's scope check. Working-only context does not
    # establish a locked charter; the API always reads the canonical pointer.
    MEM.get(f"charter:{venture_id}")
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven or not ven.active_charter_id:
            raise HTTPException(404, "no_charter")
        canonical = current_locked_charter(session, venture_id, workspace_id=ven.workspace_id)
        if canonical is None:
            raise HTTPException(404, "no_charter")
        return {
            "charter_id": canonical["id"],
            "version": canonical["version"],
            "checksum": canonical["checksum"],
            "content": canonical["content"],
            "locked": True,
        }


@router.patch("/ventures/{venture_id}/charter/{charter_id}")
def refuse_locked_update(venture_id: str, charter_id: str, payload: dict):
    factory = db()
    with factory() as session:
        row = session.get(Charter, charter_id)
        if not row or row.venture_id != venture_id:
            raise HTTPException(404, "not_found")
        if row.locked:
            raise HTTPException(
                409,
                {
                    "error": {
                        "code": "charter_locked",
                        "message": "Locked charters are immutable; amend instead.",
                    }
                },
            )
        return {"ok": True}
