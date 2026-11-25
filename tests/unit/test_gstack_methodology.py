"""gstack algorithms are wired without breaking existing Company OS contracts."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.clarify.generator import assert_invariants, generate_questions
from app.clarify.slots import office_hours_slots, slots_for_budget
from app.gstack.methodology import (
    FORCING_QUESTIONS,
    INVESTIGATE_IRON_LAW,
    default_ceo_mode,
    pick_approach,
    specialist_for_role,
)
from app.gstack.prompts import overlay_system_prompt, wrap_coding_prompt
from app.org.composer import compose
from coding_agents.gstack_charter import wrap_prompt


def test_office_hours_questions_keep_slot_invariants():
    qs = generate_questions(
        office_hours_slots(),
        idea="Brand kit generator for solo founders",
        research={"competitors": ["Canva"], "demand": "mixed"},
        include_llm_runtime=False,
    )
    assert_invariants(qs)
    keys = [q["slot_key"] for q in qs]
    assert keys == [
        "goal_primary",
        "market_reach",
        "audience",
        "mvp_scope",
        "gtm_order",
        "non_goals",
    ]
    assert all(q["gstack_id"] and q["gstack_name"] for q in qs)
    assert all(q["process"] == "gstack_office_hours" for q in qs)
    blob = " ".join(q["prompt"] + " " + q["why_asked"] for q in qs)
    assert "Brand kit" in blob
    assert "I want" not in blob
    assert "…" not in blob
    demand = next(q for q in qs if q["slot_key"] == "goal_primary")
    assert "waitlist" in demand["why_asked"].lower() or "demand" in demand["why_asked"].lower()
    market = next((q for q in qs if q["slot_key"] == "market_reach"), None)
    if market:
        assert "Canva" in market["prompt"] or "Canva" in (market["options"][2].get("label") or "")
    leftover = generate_questions(slots_for_budget(4), idea="Brand kit generator for solo founders")
    assert any(q["slot_key"] == "llm_runtime" for q in leftover)


def test_autoplan_picks_complete_then_simple():
    winner = pick_approach(
        [
            {"name": "platform", "completeness": 3, "effort": "XL"},
            {"name": "wedge", "completeness": 3, "effort": "S"},
            {"name": "demo", "completeness": 1, "effort": "S"},
        ]
    )
    assert winner["name"] == "wedge"


def test_ceo_mode_defaults():
    assert default_ceo_mode(stage="draft") == "scope_expansion"
    assert default_ceo_mode(stage="live") == "hold_scope"
    assert default_ceo_mode(files_touched=20) == "scope_reduction"


def test_builder_prompt_overlay_keeps_deliverable():
    base = "You are the builder for: Acme. Deliverable: Merged Work Orders."
    out = overlay_system_prompt("builder", base)
    assert "Merged Work Orders" in out
    assert "Iron Law" in out or "root cause" in out.lower()
    assert overlay_system_prompt("builder", out) == out  # idempotent


def test_coding_wrap_is_idempotent_and_keeps_task():
    raw = "Build index.html for Acme"
    once = wrap_prompt(raw)
    twice = wrap_prompt(once)
    assert once == twice
    assert "TASK:" in once
    assert "Build index.html for Acme" in once
    assert "ROOT CAUSE" in once
    company = wrap_coding_prompt(raw)
    assert "Build index.html for Acme" in company


def test_compose_still_returns_orchestrator():
    plan = compose({"one_liner": "A notes app", "current_stage": "org_planning"}, {})
    roles = {r.role for r in plan.roles}
    assert "orchestrator" in roles
    orch = next(r for r in plan.roles if r.role == "orchestrator")
    assert "office-hours" in orch.system_prompt.lower() or "wedge" in orch.system_prompt.lower()


def test_forcing_questions_cover_six():
    assert set(FORCING_QUESTIONS) >= {
        "goal_primary",
        "market_reach",
        "audience",
        "mvp_scope",
        "gtm_order",
        "non_goals",
    }
    for spec in FORCING_QUESTIONS.values():
        assert len(spec["options"]) == 3
        assert sum(1 for o in spec["options"] if o["recommended"]) == 1


def test_specialist_unknown_role_is_empty():
    assert specialist_for_role("not_a_role") == ""


@pytest.mark.asyncio
async def test_validate_step_is_full_office_hours():
    from types import SimpleNamespace

    from app.onboarding.state import Ctx
    from app.onboarding.steps.validate import generate_questions as validate_generate
    from app.onboarding.steps.validate import office_hours_plan
    from app.state import MEM

    ven = SimpleNamespace(id="ven_gstack", raw_idea="EoR for remote teams", name="Paygrid", one_liner="")
    run = SimpleNamespace(id="obr_gstack", phase="validate")
    ctx = Ctx(venture=ven, run=run, mode="guided")  # type: ignore[arg-type]
    result = await validate_generate(ctx)
    assert result.output["process"] == "gstack_office_hours"
    assert result.output["n"] == 6
    names = [q["gstack_name"] for q in result.output["questions"]]
    assert names == [
        "Demand Reality",
        "Status Quo",
        "Desperate Specificity",
        "Narrowest Wedge",
        "Observation & Surprise",
        "Future-Fit",
    ]
    assert not any(q["slot_key"] == "llm_runtime" for q in result.output["questions"])
    MEM[f"answers:{ven.id}"] = {q["slot_key"]: "opt_a" for q in result.output["questions"]}
    MEM[f"questions:{ven.id}"] = result.output["questions"]
    planned = await office_hours_plan(ctx)
    assert planned.pause is not None
    assert planned.pause.reason == "awaiting_gstack_plan"
    assert planned.output["verdict"] == "go_wedge"
    assert planned.output["inferred"]["monetization"]


def test_office_hours_plan_infers_the_rest():
    from app.gstack.plan import apply_inferred_answers, infer_from_office_hours

    answers = {
        "goal_primary": "opt_a",
        "market_reach": "opt_a",
        "audience": "opt_c",
        "mvp_scope": "opt_a",
        "gtm_order": "opt_c",
        "non_goals": "opt_c",
    }
    plan = infer_from_office_hours(
        idea="Employer of record for remote teams",
        name="Paygrid",
        answers=answers,
    )
    assert plan["process"] == "gstack_office_hours"
    assert plan["product"] == "Paygrid"
    assert plan["verdict"] == "go_wedge"
    assert len(plan["stages"]) == 6
    assert len(plan["next_five"]) == 5
    assert "platform" in " ".join(plan["wont_build"]).lower()
    assert plan["inferred"]["gtm_order"] == "product_first"
    assert "monetization" in plan["inferred"]
    assert "channels" in plan["inferred"]
    merged = apply_inferred_answers(answers, plan)
    assert merged["monetization"]
    assert merged["goal_primary"] == "opt_a"
