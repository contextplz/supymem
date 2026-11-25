"""Generated question sets obey 06 §1 invariants."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.clarify.generator import assert_invariants, generate_questions
from app.clarify.slots import slots_for_budget
from app.intake.normalizer import classify_deterministic, question_budget, score_complexity


def test_invariants_across_generated_sets() -> None:
    ideas = [
        "notes app",
        "home bakers WhatsApp cake orders",
        "marketplace with payments and KYC for freelancers",
        "HIPAA patient scheduling",
        "instagram analytics for creators",
    ]
    for idea in ideas:
        for _ in range(10):
            c = classify_deterministic(idea)
            sc = score_complexity(idea, c)
            n, _ = question_budget(sc.score, sc.uncertainty, c["regulatory_surface"])
            qs = generate_questions(slots_for_budget(n))
            assert_invariants(qs)
            assert any(q["slot_key"] == "llm_runtime" for q in qs)
