"""Durable, tenant-scoped feedback ingest API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.schemas import FeedbackBody
from app.db.models import FeedbackEvent, SuspiciousContentEvent, Venture
from app.feedback.classify import classify_text
from app.feedback.normalize import sanitize
from app.state import db
from app.tenancy import current_or_none
from core.clock import utcnow
from core.ids import new_id

router = APIRouter(tags=["feedback"])


@router.post("/ventures/{venture_id}/feedback")
def post_feedback(venture_id: str, payload: FeedbackBody):
    factory = db()
    with factory() as session:
        venture = session.get(Venture, venture_id)
        scope = current_or_none()
        if not venture or scope and scope.workspace_id not in {"none", venture.workspace_id}:
            raise HTTPException(404, "not_found")
        text = sanitize(payload.text).strip()
        if not text:
            raise HTTPException(422, detail={"code": "feedback_text_required"})
        classified = classify_text(text, source=payload.source)
        trust = "founder" if payload.source == "founder" else classified.trust
        now = utcnow()
        identifier = new_id("fb")
        session.add(FeedbackEvent(
            id=identifier, venture_id=venture_id, source=payload.source,
            source_ref=f"founder:{scope.actor}" if payload.source == "founder" and scope else None,
            raw_text=text, trust=trust, classified_by="feedback_classifier_v1",
            classified_at=now, created_at=now,
        ))
        if classified.injection_attempt:
            session.add(SuspiciousContentEvent(
                id=new_id("sce"), workspace_id=venture.workspace_id,
                source_ref=f"feedback:{identifier}", pattern=classified.matched or "unknown",
                excerpt=text[:240], at=now,
            ))
        session.commit()
        from app.analytics.funnel import emit_for_venture
        from app.analytics.error_events import actor_person_id
        emit_for_venture("feedback.submitted", venture_id, key=f"feedback:{identifier}", workspace_id=venture.workspace_id,
                         person_id=actor_person_id(scope.actor if scope else None),
                         props={"kind": "venture_feedback", "source": payload.source})
        return {
            "id": identifier,
            "summary": text,
            "trust": trust,
            "action_taken": "none",
            "security_logged": classified.injection_attempt,
        }


@router.get("/ventures/{venture_id}/feedback")
def list_feedback(venture_id: str):
    factory = db()
    with factory() as session:
        venture = session.get(Venture, venture_id)
        scope = current_or_none()
        if not venture or scope and scope.workspace_id not in {"none", venture.workspace_id}:
            raise HTTPException(404, "not_found")
        rows = session.scalars(
            select(FeedbackEvent).where(FeedbackEvent.venture_id == venture_id)
            .order_by(FeedbackEvent.created_at.desc(), FeedbackEvent.id.desc()).limit(100)
        ).all()
        return {"items": [{
            "id": row.id, "summary": row.raw_text or "Feedback event",
            "source": row.source, "trust": row.trust, "created_at": row.created_at.isoformat(),
        } for row in rows]}
