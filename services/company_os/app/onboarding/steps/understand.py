"""Phase 1 — understand."""

from __future__ import annotations

import asyncio

from app.intake.normalizer import (
    classify_deterministic,
    question_budget,
    research_recon,
    score_complexity as score_complexity_fn,
)
from app.onboarding.registry import StepResult
from app.onboarding.state import Ctx
from app.state import MEM
from app.memory.working import merge_working_dict

_COMPLEXITY_WORDS = (
    (0.35, "straightforward"),
    (0.55, "moderate"),
    (0.75, "involved"),
    (1.01, "heavy"),
)


def _complexity_word(score: float) -> str:
    for bound, word in _COMPLEXITY_WORDS:
        if score < bound:
            return word
    return "heavy"


def _reason_phrase(reasons: list[str]) -> str:
    mapping = {
        "base_minimum_3": "a few open choices",
        "complexity_mid": "moderate build complexity",
        "complexity_elevated": "elevated complexity",
        "complexity_high": "a heavy build surface",
        "uncertainty": "some uncertainty",
        "regulatory": "a regulated surface",
    }
    human = [mapping.get(r, r.replace("_", " ")) for r in reasons if r != "base_minimum_3"]
    if not human:
        return "a few open choices"
    if len(human) == 1:
        return human[0]
    return ", ".join(human[:-1]) + " and " + human[-1]


async def product_ask(ctx: Ctx) -> StepResult:
    """Resolve what the founder means before GStack or research."""
    from app.clarify.dilemmas import dilemmas_for_idea
    from app.onboarding.gates import get_run_gate, set_run_gate
    from app.onboarding.registry import PauseRequest
    from core.ids import new_id

    idea = (
        (ctx.out.get("orient.read_brief") or {}).get("normalized")
        or ctx.venture.raw_idea
        or ""
    )
    from app.clarify.dilemmas import is_product_questions

    questions = MEM.get(f"product_questions:{ctx.venture.id}") or []
    if not questions:
        questions = dilemmas_for_idea(idea, name=getattr(ctx.venture, "name", "") or "")
        MEM[f"product_questions:{ctx.venture.id}"] = questions
    from app.ventures import known
    checks_enabled = known.enabled()
    assumptions = []
    if checks_enabled:
        remaining = []
        answers = dict(MEM.get(f"product_answers:{ctx.venture.id}") or {})
        for question in questions:
            slot = question.get("slot_key", question["id"])
            if answers.get(slot) or answers.get(question["id"]):
                continue
            result = known.known(ctx.venture, slot)
            if result["status"] == "missing":
                remaining.append(question)
                continue
            card = known.question(result, slot.removeprefix("product_"))
            if result["status"] == "known":
                answers[slot] = result["value"]
                assumptions.append(card)
            else:
                remaining.append({**question, "prompt": card["text"], "known_check": result,
                    "options": [{"id": "known_" + str(index), "label": str(value["value"]), "recommended": False}
                                for index, value in enumerate(card["values"])]})
        MEM[f"product_answers:{ctx.venture.id}"] = answers
        questions = remaining
    current = MEM.get(f"questions:{ctx.venture.id}") or []
    if checks_enabled or not current or is_product_questions(current):
        MEM[f"questions:{ctx.venture.id}"] = questions
    merge_working_dict(MEM, f"brief:{ctx.venture.id}", {"normalized": idea, "process": "product_understanding"})
    if ctx.narrator:
        await ctx.narrator.emit(
            phase="understand",
            kind="step",
            status="running",
            text="Figuring out what you actually mean.",
            step_id="understand.product_ask",
        )
        if assumptions:
            await ctx.narrator.emit(phase="understand", kind="step", status="running",
                text="\n\n".join(card["text"] for card in assumptions), step_id="understand.product_ask")
    if (not questions if checks_enabled else MEM.get(f"product_answers:{ctx.venture.id}")) or get_run_gate(
        "product_understanding", ctx.venture.id, ctx.run.id
    ):
        return StepResult(output={"n": len(questions), "review": "accepted", **({"assumptions": assumptions} if checks_enabled else {})})
    if ctx.mode == "auto" and not any(question.get("known_check") for question in questions):
        answers: dict[str, str] = dict(MEM.get(f"product_answers:{ctx.venture.id}") or {}) if checks_enabled else {}
        for q in questions:
            rec = next((o for o in (q.get("options") or []) if o.get("recommended")), None)
            if rec:
                answers[q["id"]] = rec["id"]
                answers[q.get("slot_key", q["id"])] = rec.get("label") or rec["id"]
        MEM[f"product_answers:{ctx.venture.id}"] = answers
        if checks_enabled:
            MEM[f"product_answers_source:{ctx.venture.id}"] = "auto"
        set_run_gate("product_understanding", ctx.venture.id, ctx.run.id, "auto")
        return StepResult(output={"n": len(questions), "source": "auto"}, tokens={"n": len(questions)})
    return StepResult(
        output={"n": len(questions), "questions": questions, "process": "product_understanding",
                **({"assumptions": assumptions} if checks_enabled else {})},
        tokens={"n": len(questions)},
        pause=PauseRequest(
            reason="awaiting_product_understanding",
            resume_token=new_id("clr"),
            founder_action=f"/v/{ctx.venture.id}/#clarify",
        ),
    )


async def product_brief(ctx: Ctx) -> StepResult:
    """Write the product we think we heard. Founder confirms before research."""
    from app.clarify.dilemmas import brief_from_answers
    from app.onboarding.gates import get_run_gate, set_run_gate
    from app.onboarding.registry import PauseRequest

    idea = (
        (ctx.out.get("orient.read_brief") or {}).get("normalized")
        or ctx.venture.raw_idea
        or ""
    )
    answers = dict(MEM.get(f"product_answers:{ctx.venture.id}") or MEM.get(f"answers:{ctx.venture.id}") or {})
    questions = MEM.get(f"product_questions:{ctx.venture.id}") or []
    brief = brief_from_answers(
        idea=idea,
        name=getattr(ctx.venture, "name", "") or "",
        answers=answers,
        questions=questions,
    )
    merge_working_dict(MEM, f"brief:{ctx.venture.id}", {"product": brief})
    MEM[f"product_brief:{ctx.venture.id}"] = brief
    if ctx.mode == "auto":
        set_run_gate("product_brief", ctx.venture.id, ctx.run.id, "accepted")
        return StepResult(output=brief, tokens={"product": brief.get("name")})
    if get_run_gate("product_brief", ctx.venture.id, ctx.run.id):
        return StepResult(output={**brief, "review": "accepted"})
    return StepResult(
        output=brief,
        tokens={"product": brief.get("name")},
        pause=PauseRequest(
            reason="awaiting_product_brief",
            founder_action=f"/v/{ctx.venture.id}/#product-brief",
        ),
    )


async def classify(ctx: Ctx) -> StepResult:
    idea = (ctx.out.get("orient.read_brief") or {}).get("normalized") or ctx.venture.raw_idea
    classification = classify_deterministic(idea)
    merge_working_dict(MEM, f"brief:{ctx.venture.id}", {"classification": classification})
    arch = classification.get("build_archetype", "product").replace("_", " ")
    aud = classification.get("audience", "users")
    return StepResult(
        output=classification,
        tokens={"archetype": arch, "audience": aud},
    )


async def research_founder(ctx: Ctx) -> StepResult:
    # Only reached when research_founder opted in (skip_if otherwise)
    name = ctx.company
    query = f"{name} founder public profile"
    if ctx.narrator:
        await ctx.narrator.search(query, step_id="understand.research_founder")
    return StepResult(output={"queries": [query], "claims": []}, query=query)


async def research_market(ctx: Ctx) -> StepResult:
    import os

    product = (MEM.get(f"brief:{ctx.venture.id}") or {}).get("product") or {}
    idea = (
        product.get("research_query")
        or product.get("one_liner")
        or ctx.venture.raw_idea
    )
    classification = ctx.out.get("understand.classify") or {}
    archetype = classification.get("build_archetype", "product")
    preview_queries = [
        f"{str(idea)[:80]} competitors alternatives",
        f"{product.get('first_job') or archetype} existing products pricing",
        f"{product.get('today') or 'workaround'} {product.get('first_user') or ''}".strip(),
    ]

    from app.org.heartbeat import mark_idle, mark_working

    try:
        mark_working("researcher", ctx.venture.id, "Reading the market for the product we just named.")
    except Exception:  # noqa: BLE001
        pass

    announced: list[str] = []
    if ctx.narrator:
        for q in preview_queries:
            if q and q not in announced:
                announced.append(q)
                await ctx.narrator.search(q, step_id="understand.research_market")

    test_run = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    extras: list[str] = []
    if not test_run:
        if product.get("first_job"):
            extras.append(f"{product['first_job']} software tools people use now")
        if product.get("today"):
            extras.append(f"{product['today']} complaints alternative workaround")
        if product.get("first_user") and product.get("first_job"):
            extras.append(f"{product['first_user']} {product['first_job']} forum reddit")
    budget = 12.0 if test_run else 70.0
    recon: dict = {}
    try:
        recon = await asyncio.wait_for(
            research_recon(str(idea), budget_s=budget, extra_queries=extras or None),
            timeout=budget + 8.0,
        )
        if not test_run and (recon.get("mode") != "live" or not (recon.get("sources") or [])):
            recon = await asyncio.wait_for(
                research_recon(str(idea), budget_s=35.0, extra_queries=extras or None),
                timeout=43.0,
            )
        if not test_run and recon.get("mode") == "live" and (recon.get("sources") or []):
            try:
                from app.research.dossier import build_validation_dossier

                dossier = await asyncio.wait_for(
                    build_validation_dossier(
                        venture_id=ctx.venture.id,
                        idea=str(idea),
                        budget_s=40.0,
                    ),
                    timeout=48.0,
                )
                if dossier.get("fast_scan"):
                    recon = {**recon, **(dossier.get("fast_scan") or {}), "dossier": True}
                recon["summary"] = recon.get("summary") or (dossier.get("markdown") or "")[:400]
                recon["engine"] = dossier.get("engine")
                recon["dossier"] = True
                recon["download_path"] = dossier.get("download_path")
                recon["enriched_source_count"] = dossier.get("enriched_source_count")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        recon = {
            "mode": "skipped",
            "reason": "research_error",
            "queries": announced or preview_queries,
            "claims": [],
            "demand_signals": [],
            "competitors": [],
            "sources": [],
        }

    claims = list(recon.get("claims") or [])
    from app.research.evidence_dimensions import explicit_demand_leads

    demand_signals = explicit_demand_leads(recon.get("demand_signals"))
    competitors = list(recon.get("competitors") or [])
    demand = "unknown"
    if recon.get("mode") == "live" and demand_signals:
        demand = "mixed"
    out = {
        "demand": demand,
        "claims": claims,
        "demand_signals": demand_signals,
        "competitors": competitors,
        "queries": recon.get("queries") or preview_queries,
        "sources": recon.get("sources") or [],
        "mode": recon.get("mode"),
        "reason": recon.get("reason"),
        "differentiation_score": recon.get("differentiation_score"),
        "evidence_dimensions": recon.get("evidence_dimensions"),
        "strongest_for": recon.get("strongest_for"),
        "strongest_against": recon.get("strongest_against"),
        "changes_verdict": recon.get("changes_verdict"),
        "summary": recon.get("summary"),
        "provider": recon.get("provider"),
        "engine": recon.get("engine"),
        "dossier": recon.get("dossier"),
        "download_path": recon.get("download_path"),
        "enriched_source_count": recon.get("enriched_source_count"),
    }
    merge_working_dict(MEM, f"brief:{ctx.venture.id}", {"research": out})

    queries = list(recon.get("queries") or preview_queries)
    if ctx.narrator:
        for q in queries[:4]:
            if q and q not in announced:
                announced.append(q)
                await ctx.narrator.search(q, step_id="understand.research_market")

    try:
        mark_idle("researcher", ctx.venture.id)
    except Exception:  # noqa: BLE001
        pass

    if demand == "unknown":
        reason = recon.get("reason") or "no_results"
        return StepResult(
            output=out,
            demand="unknown",
            kind="search",
            variant_key="understand.research_market",
            tokens={"count": 0},
            query=(queries[0] if queries else str(idea)[:80]),
            detail=[f"Live research did not run ({reason}). No simulated market score was written."],
        )
    return StepResult(
        output=out,
        kind="search",
        query=(queries[0] if queries else str(idea)[:80]),
        tokens={"count": len(competitors)},
        detail=[
            recon.get("summary") or f"{len(competitors)} competitors · {len(demand_signals)} demand signals",
            *[f"{c.get('name', 'unknown')}{(c.get('website') or '')[:60]}" for c in competitors[:5]],
        ],
    )


async def score_complexity(ctx: Ctx) -> StepResult:
    idea = ctx.venture.raw_idea
    classification = ctx.out.get("understand.classify") or {}
    result = score_complexity_fn(idea, classification)
    word = _complexity_word(result.score)
    complexity = {
        "score": result.score,
        "uncertainty": result.uncertainty,
        "build_feasibility": result.build_feasibility,
        "factors": result.factors,
        "word": word,
    }
    merge_working_dict(MEM, f"brief:{ctx.venture.id}", {"complexity": complexity})
    return StepResult(
        output={
            "score": result.score,
            "uncertainty": result.uncertainty,
            "build_feasibility": result.build_feasibility,
            "factors": result.factors,
            "word": word,
        },
        tokens={"complexity_word": word},
        detail=[f["why"] for f in result.factors],
    )


async def question_budget_step(ctx: Ctx) -> StepResult:
    cx = ctx.out.get("understand.score_complexity") or {}
    classification = ctx.out.get("understand.classify") or {}
    n, meta = question_budget(
        float(cx.get("score", 0.3)),
        float(cx.get("uncertainty", 0.4)),
        str(classification.get("regulatory_surface", "none")),
    )
    reason = _reason_phrase(list(meta.get("reasons") or []))
    merge_working_dict(MEM, f"brief:{ctx.venture.id}", {"budget": meta})
    return StepResult(
        output={"n": n, "budget": meta, "reason": reason},
        tokens={"n": n, "reason": reason},
    )
