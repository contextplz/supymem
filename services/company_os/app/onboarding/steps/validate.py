"""Phase 2 — validate (pause in guided mode)."""

from __future__ import annotations

from app.clarify.dilemmas import is_product_questions
from app.clarify.generator import assert_invariants, generate_questions as gen_questions
from app.clarify.slots import office_hours_slots
from app.gstack.methodology import FORCING_QUESTIONS, founder_topic
from app.gstack.plan import apply_inferred_answers, infer_from_office_hours
from app.onboarding.gates import get_run_gate, set_run_gate
from app.onboarding.registry import PauseRequest, StepResult
from app.onboarding.state import Ctx
from app.research.evidence_dimensions import explicit_demand_leads
from app.state import MEM
from app.memory.working import merge_working_dict, update_working_value
from app.validation.verdict import compute_verdict
from core.ids import new_id


def _idea_text(ctx: Ctx) -> str:
    return (
        (ctx.out.get("orient.read_brief") or {}).get("normalized")
        or ctx.venture.raw_idea
        or ""
    )


async def generate_questions(ctx: Ctx) -> StepResult:
    selected = office_hours_slots()
    idea = _idea_text(ctx)
    research = ctx.out.get("understand.research_market") or MEM.get(f"brief:{ctx.venture.id}", {}).get(
        "research"
    )
    if ctx.narrator:
        await ctx.narrator.emit(
            phase=getattr(ctx.run, "phase", None) or "validate",
            kind="step",
            status="running",
            text="Preparing the questions that test real demand.",
            step_id="validate.generate_questions",
            detail=[founder_topic(spec["name"]) for spec in FORCING_QUESTIONS.values()],
        )
    questions = gen_questions(
        selected,
        idea=idea,
        name=getattr(ctx.venture, "name", "") or "",
        one_liner=getattr(ctx.venture, "one_liner", "") or "",
        research=research if isinstance(research, dict) else None,
        include_llm_runtime=False,
    )
    assert_invariants(questions)
    round_id = new_id("clr")
    MEM[f"questions:{ctx.venture.id}"] = questions
    MEM[f"gstack_questions:{ctx.venture.id}"] = questions
    MEM[f"clarify_round:{ctx.venture.id}"] = round_id
    brief = {"questions": questions, "process": "gstack_office_hours"}
    if isinstance(research, dict):
        brief["research"] = research
    merge_working_dict(MEM, f"brief:{ctx.venture.id}", brief)
    return StepResult(
        output={
            "round_id": round_id,
            "n": len(questions),
            "questions": questions,
            "process": "gstack_office_hours",
        },
        tokens={"n": len(questions)},
    )


async def ask(ctx: Ctx) -> StepResult:
    gen = ctx.out.get("validate.generate_questions") or {}
    n = int(gen.get("n") or 0)
    questions = gen.get("questions") or MEM.get(f"questions:{ctx.venture.id}") or []
    round_id = gen.get("round_id") or MEM.get(f"clarify_round:{ctx.venture.id}")
    if is_product_questions(questions) or not questions:
        selected = office_hours_slots()
        idea = _idea_text(ctx)
        research = ctx.out.get("understand.research_market") or MEM.get(f"brief:{ctx.venture.id}", {}).get(
            "research"
        )
        questions = gen_questions(
            selected,
            idea=idea,
            name=getattr(ctx.venture, "name", "") or "",
            one_liner=getattr(ctx.venture, "one_liner", "") or "",
            research=research if isinstance(research, dict) else None,
            include_llm_runtime=False,
        )
        assert_invariants(questions)
        n = len(questions)
        MEM[f"questions:{ctx.venture.id}"] = questions
        MEM[f"gstack_questions:{ctx.venture.id}"] = questions
        merge_working_dict(MEM, f"brief:{ctx.venture.id}", {
            "questions": questions, "process": "gstack_office_hours"})

    if ctx.mode == "auto":
        answers: dict[str, str] = {}
        for q in questions:
            opts = q.get("options") or []
            rec = next((o for o in opts if o.get("recommended")), opts[0] if opts else None)
            if rec:
                answers[q["id"]] = rec["id"]
                # also map by slot for charter
                answers[q.get("slot_key", q["id"])] = rec.get("label") or rec["id"]
        MEM[f"answers:{ctx.venture.id}"] = answers
        MEM[f"answers_source:{ctx.venture.id}"] = "auto_default"
        ctx.set_flag("unvalidated", True)
        if ctx.narrator:
            await ctx.narrator.done(
                "validate.auto_answered",
                tokens={"n": n},
            )
        return StepResult(
            output={"answers": answers, "source": "auto_default", "n": n},
            tokens={"n": n},
            variant_key="validate.auto_answered",
        )

    existing = dict(MEM.get(f"answers:{ctx.venture.id}") or {})
    source = MEM.get(f"answers_source:{ctx.venture.id}")
    gstack_keys = {q.get("id") for q in questions} | {q.get("slot_key") for q in questions}
    if source == "founder" and any(k in existing for k in gstack_keys if k):
        return StepResult(
            output={"round_id": round_id, "n": n, "process": "gstack_office_hours", "source": "founder"},
            tokens={"n": n},
        )

    # Guided: pause for the six GStack forcing questions
    return StepResult(
        output={"round_id": round_id, "n": n, "process": "gstack_office_hours"},
        tokens={"n": n},
        pause=PauseRequest(
            reason="awaiting_clarification",
            resume_token=str(round_id),
            founder_action=f"/v/{ctx.venture.id}/#clarify",
        ),
    )


async def office_hours_plan(ctx: Ctx) -> StepResult:
    """After office hours, infer the rest and show the plan. No more quiz."""
    answers = dict(MEM.get(f"answers:{ctx.venture.id}") or {})
    questions = MEM.get(f"questions:{ctx.venture.id}") or []
    gstack_keys = {q.get("id") for q in questions} | {q.get("slot_key") for q in questions}
    has_gstack_answers = any(k in answers for k in gstack_keys if k) and not is_product_questions(questions)
    if is_product_questions(questions) or not questions or not has_gstack_answers:
        MEM.pop(f"gstack_plan:{ctx.venture.id}", None)
        asked = await ask(ctx)
        if asked.pause:
            return asked
        questions = MEM.get(f"questions:{ctx.venture.id}") or questions
        answers = dict(MEM.get(f"answers:{ctx.venture.id}") or answers)
    plan = infer_from_office_hours(
        idea=_idea_text(ctx),
        name=getattr(ctx.venture, "name", "") or "",
        one_liner=getattr(ctx.venture, "one_liner", "") or "",
        answers=answers,
        questions=questions,
    )
    product = (MEM.get(f"brief:{ctx.venture.id}") or {}).get("product") or MEM.get(
        f"product_brief:{ctx.venture.id}"
    )
    if isinstance(product, dict):
        plan["product_brief"] = product
        plan["product"] = product.get("name") or plan.get("product")
    MEM[f"gstack_plan:{ctx.venture.id}"] = plan
    MEM[f"answers:{ctx.venture.id}"] = apply_inferred_answers(answers, plan)
    if ctx.mode == "auto":
        set_run_gate("gstack_plan", ctx.venture.id, ctx.run.id, "accepted")
        return StepResult(output=plan, tokens={"verdict": plan["verdict"]})
    existing = get_run_gate("gstack_plan", ctx.venture.id, ctx.run.id)
    if existing:
        return StepResult(output={**plan, "review": existing})
    return StepResult(
        output=plan,
        tokens={"verdict": plan["verdict"]},
        pause=PauseRequest(
            reason="awaiting_gstack_plan",
            founder_action=f"/v/{ctx.venture.id}/#gstack-plan",
        ),
    )


def _research_query(ctx: Ctx, market: dict) -> str:
    product = (MEM.get(f"brief:{ctx.venture.id}") or {}).get("product") or {}
    idea = (
        product.get("research_query")
        or product.get("one_liner")
        or ctx.venture.raw_idea
        or ""
    )
    return str((market.get("queries") or [idea] or ["the product"])[0])[:120]


async def demand_read(ctx: Ctx) -> StepResult:
    import os

    market = dict(ctx.out.get("understand.research_market") or {})
    query = _research_query(ctx, market)
    test_run = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    already = bool(
        market.get("dossier")
        or market.get("engine")
        or (market.get("mode") == "live" and (market.get("sources") or market.get("competitors")))
    )
    dossier_meta: dict = {}

    if already:
        dossier_meta = {
            "reused": True,
            "engine": market.get("engine") or market.get("provider"),
            "download_path": market.get("download_path"),
        }
    elif not test_run:
        if ctx.narrator:
            await ctx.narrator.emit(
                phase=getattr(ctx.run, "phase", None) or "validate",
                kind="search",
                status="running",
                text="Deepening the product research we already started.",
                step_id="validate.demand_read",
                query=query,
            )
        try:
            if market.get("mode") != "live" or not (market.get("sources") or market.get("competitors")):
                from app.intake.normalizer import research_recon

                recon = await research_recon(query, budget_s=40.0)
                if recon.get("mode") == "live" and (recon.get("sources") or recon.get("competitors")):
                    market = {
                        **market,
                        **recon,
                        "demand_signals": recon.get("demand_signals") or [],
                        "demand": "mixed" if explicit_demand_leads(recon.get("demand_signals")) else "unknown",
                    }
                    merge_working_dict(MEM, f"brief:{ctx.venture.id}", {"research": market})
                    ctx.out["understand.research_market"] = market
            from app.research.dossier import build_validation_dossier

            dossier = await build_validation_dossier(
                venture_id=ctx.venture.id,
                idea=query,
                budget_s=90.0,
            )
            dossier_meta = {
                "engine": dossier.get("engine"),
                "download_path": dossier.get("download_path"),
                "enriched_source_count": dossier.get("enriched_source_count"),
            }
            MEM[f"research_dossier:{ctx.venture.id}"] = dossier_meta
            fast = dossier.get("fast_scan") or {}
            if fast.get("sources"):
                market = {**market, **{k: fast[k] for k in fast if k not in market or not market.get(k)}}
                if not market.get("demand_signals"):
                    market["demand_signals"] = fast.get("demand_signals") or []
        except Exception as exc:  # noqa: BLE001
            dossier_meta = {"error": str(exc)[:200]}
        if ctx.narrator and dossier_meta.get("download_path"):
            await ctx.narrator.emit(
                phase=getattr(ctx.run, "phase", None) or "validate",
                kind="search",
                status="done",
                text="Validation dossier ready to download.",
                step_id="validate.demand_read",
                query=query,
                detail=[dossier_meta.get("download_path", "")],
            )

    verdict = compute_verdict(market)
    demand = "mixed" if explicit_demand_leads(market.get("demand_signals")) else "unknown"
    conf = verdict.confidence
    validation = verdict.model_dump(mode="json")
    validation["source_count"] = len(market.get("sources") or [])
    validation["strongestFor"] = market.get("strongest_for") or validation.get("strongestFor")
    validation["strongestAgainst"] = market.get("strongest_against") or validation.get("strongestAgainst")
    validation["changesVerdict"] = market.get("changes_verdict") or validation.get("changesVerdict")
    validation["summary"] = market.get("summary")
    validation["mode"] = market.get("mode")
    validation["reason"] = market.get("reason")
    validation["competitors"] = (market.get("competitors") or [])[:8]
    validation["demand_signals"] = explicit_demand_leads(market.get("demand_signals"))[:8]
    validation["sources"] = [
        {"title": s.get("title"), "url": s.get("url")}
        for s in (market.get("sources") or [])[:8]
        if isinstance(s, dict)
    ]
    validation["dossier_download"] = dossier_meta.get("download_path")
    validation["dossier_engine"] = dossier_meta.get("engine")
    validation["provider"] = market.get("provider")
    validation["elapsed_ms"] = market.get("elapsed_ms")
    validation["depth"] = market.get("depth") or "snippet_scan"
    if market.get("mode") != "live":
        validation["recommendation"] = (
            "Live web research did not run. This is not a market read. "
            "Retry the scan; if it persists, report the research failure."
        )
        validation["strongestFor"] = "No live demand signals, research did not complete."
        validation["strongestAgainst"] = (
            "We did not invent scores. Until a live scan lands, there is no verdict."
        )
        validation["changesVerdict"] = "A successful live web scan with verifiable sources."
    MEM[f"validation:{ctx.venture.id}"] = validation
    MEM[f"demand:{ctx.venture.id}"] = {"demand": demand, "confidence": conf, "verdict": validation}
    return StepResult(
        output={"demand": demand, "confidence": conf, "verdict": validation, "dossier": dossier_meta},
        demand=demand,
        kind="search",
        query=query,
        tokens={"demand": demand},
        detail=(
            ["Using the product research already gathered."]
            if dossier_meta.get("reused")
            else ([dossier_meta.get("download_path")] if dossier_meta.get("download_path") else None)
        ),
    )


async def recommendation(ctx: Ctx) -> StepResult:
    demand = (ctx.out.get("validate.demand_read") or {}).get("demand") or "unknown"
    if demand == "strong":
        rec = "go"
    elif demand == "weak":
        rec = "narrow first"
    elif demand == "mixed":
        rec = "test demand first"
    else:
        rec = "treat the first build as the experiment"
    research = ctx.out.get("understand.research_market") or {}
    update_working_value(MEM, f"demand:{ctx.venture.id}", lambda row: {
            **(row or {}),
            "demand": (row or {}).get("demand") or demand,
            "confidence": (row or {}).get("confidence"),
            "recommendation": rec,
            "competitors": research.get("competitors") or [],
            "claims": research.get("claims") or [],
            "queries": research.get("queries") or [],
        })
    return StepResult(output={"recommendation": rec}, tokens={"recommendation": rec})


async def review_choice(ctx: Ctx) -> StepResult:
    """Persisted choice between the dossier and the concise verdict.

    Auto mode may proceed with an explicit disclosure stamp (doc 78 §1.1) — never silently.
    Guided mode blocks until the founder chooses.
    """
    validation = MEM.get(f"validation:{ctx.venture.id}") or {}
    research = ctx.out.get("understand.research_market") or {}
    existing = get_run_gate("validation_review", ctx.venture.id, ctx.run.id)

    if ctx.mode == "auto" and not existing:
        set_run_gate("validation_review", ctx.venture.id, ctx.run.id, "summary")
        set_run_gate("validation_mode", ctx.venture.id, ctx.run.id, "auto_proceeded")
        ctx.set_flag("validation_auto_proceeded", True)
        existing = "summary"

    if existing:
        return StepResult(
            output={
                "verdict": validation,
                "review": existing,
                "source_count": len(research.get("sources") or []),
                "competitor_count": len(research.get("competitors") or []),
                "auto_proceeded": ctx.mode == "auto",
            }
        )

    return StepResult(
        output={
            "verdict": validation,
            "source_count": len(research.get("sources") or []),
            "competitor_count": len(research.get("competitors") or []),
        },
        pause=PauseRequest(
            reason="awaiting_validation_review",
            founder_action=f"/v/{ctx.venture.id}/#validation",
        ),
    )


async def decision(ctx: Ctx) -> StepResult:
    choice = get_run_gate("validation_decision", ctx.venture.id, ctx.run.id)
    if ctx.mode == "auto" and not choice:
        # Disclose and proceed — founder can later open the dossier.
        choice = {
            "action": "proceed",
            "note": "auto_proceeded",
            "actor": "system",
            "would_have_shown": MEM.get(f"validation:{ctx.venture.id}") or {},
        }
        set_run_gate("validation_decision", ctx.venture.id, ctx.run.id, choice)
        set_run_gate("validation_mode", ctx.venture.id, ctx.run.id, "auto_proceeded")
        ctx.set_flag("validation_auto_proceeded", True)
        ctx.set_flag("unvalidated", True)

    if not choice:
        return StepResult(
            output={"verdict": MEM.get(f"validation:{ctx.venture.id}") or {}},
            pause=PauseRequest(
                reason="awaiting_validation_decision",
                founder_action=f"/v/{ctx.venture.id}/#validation",
            ),
        )
    return StepResult(output={"decision": choice})
