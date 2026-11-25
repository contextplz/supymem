"""Intake: classify → recon → question budget."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from app.clarify.generator import assert_invariants, generate_questions
from app.clarify.slots import office_hours_slots
from app.core.stages import transition
from app.db.models import IdeaBrief, Venture
from app.intake.normalizer import (
    classify_deterministic,
    normalize,
    question_budget,
    research_recon,
    score_complexity,
)
from app.state import MEM, db
from core.clock import utcnow
from core.enums import VentureStage
from core.errors import IllegalTransition
from core.ids import new_id

router = APIRouter(tags=["intake"])


@router.post("/ventures/{venture_id}/intake")
async def run_intake(venture_id: str):
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        # Idempotent: return cached questions if already run
        cached_q = MEM.get(f"questions:{venture_id}")
        cached_b = MEM.get(f"brief:{venture_id}")
        if cached_q and cached_b:
            return {
                "idea_brief_id": cached_b.get("id"),
                "classification": cached_b.get("classification"),
                "complexity": cached_b.get("complexity"),
                "research": cached_b.get("research") or [],
                "question_budget": cached_b.get("budget"),
                "questions": cached_q,
                "stage": ven.stage,
                "cached": True,
            }
        norm = normalize(ven.raw_idea)
        try:
            ven.stage = transition(ven.stage, VentureStage.CLASSIFYING).value
        except IllegalTransition:
            ven.stage = VentureStage.CLASSIFYING.value
        classification = classify_deterministic(norm["normalized"])
        research = await research_recon(norm["normalized"], budget_s=2.0)
        complexity = score_complexity(norm["normalized"], classification)
        brief = IdeaBrief(
            id=new_id("idea"),
            venture_id=venture_id,
            domain=classification["domain"],
            business_model=classification["business_model"],
            build_archetype=classification["build_archetype"],
            audience=classification["audience"],
            monetization=classification.get("monetization"),
            regulatory_surface=classification["regulatory_surface"],
            novelty=classification["novelty"],
            complexity_score=complexity.score,
            uncertainty_score=complexity.uncertainty,
            build_feasibility=complexity.build_feasibility,
            complexity_factors_json=json.dumps(complexity.factors),
            research_notes_json=json.dumps(research),
            classifier_version=classification["classifier_version"],
            created_at=utcnow(),
        )
        n, budget_reason = question_budget(
            complexity.score, complexity.uncertainty, classification["regulatory_surface"]
        )
        session.add(brief)
        try:
            ven.stage = transition(ven.stage, VentureStage.CLARIFYING).value
        except IllegalTransition:
            ven.stage = VentureStage.CLARIFYING.value
        ven.updated_at = utcnow()
        name = ven.name or ""
        session.commit()
        slots = office_hours_slots()
        questions = generate_questions(
            slots,
            idea=norm["normalized"],
            name=name,
            include_llm_runtime=False,
        )
        assert_invariants(questions)
        MEM[f"questions:{venture_id}"] = questions
        MEM[f"brief:{venture_id}"] = {
            "id": brief.id,
            "classification": classification,
            "complexity": {
                "score": complexity.score,
                "uncertainty": complexity.uncertainty,
                "factors": complexity.factors,
                "build_feasibility": complexity.build_feasibility,
            },
            "budget": budget_reason,
            "normalized": norm,
            "research": research,
        }
        return {
            "idea_brief_id": brief.id,
            "classification": classification,
            "complexity": MEM[f"brief:{venture_id}"]["complexity"],
            "research": research,
            "question_budget": budget_reason,
            "questions": questions,
            "stage": ven.stage,
        }
