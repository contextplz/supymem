"""Infer the rest of the charter from GStack office-hours answers."""

from __future__ import annotations

from typing import Any

from app.gstack.methodology import FORCING_QUESTIONS

VERDICT_COPY = {
    "go_wedge": {
        "label": "Build the narrowest paid wedge",
        "thesis": (
            "Someone already behaves like a customer. Prove one workflow they would "
            "pay for this week. Everything else waits."
        ),
    },
    "test_demand": {
        "label": "Test demand before you build",
        "thesis": (
            "Interest is not demand. Talk to named people and run a concierge or "
            "waitlist test before investing in a platform."
        ),
    },
    "research_first": {
        "label": "Watch the status quo before you build",
        "thesis": (
            "Conviction is not evidence. Name the workaround, watch one person "
            "struggle, then decide the wedge."
        ),
    },
}


def _option_id(answers: dict[str, Any], questions: list[dict[str, Any]], slot_key: str) -> str:
    by_slot = {q.get("slot_key"): q for q in questions}
    question = by_slot.get(slot_key) or {}
    raw = answers.get(question.get("id") or "") or answers.get(slot_key) or ""
    raw = str(raw or "").strip()
    provenance = answers.get("_answer_provenance") or {}
    if slot_key in {"goal_primary", "market_reach", "audience", "gtm_order"} and (
            provenance.get(slot_key) or provenance.get(question.get("id")) or {}).get("provenance") == "suggested_choice":
        return ""
    if raw in {"opt_a", "opt_b", "opt_c"}:
        return raw
    options = question.get("options") or (FORCING_QUESTIONS.get(slot_key) or {}).get("options") or []
    for opt in options:
        if raw == opt.get("id") or raw == opt.get("label"):
            return str(opt.get("id") or "")
    if raw:
        return "other"
    return ""


def _answer_text(answers: dict[str, Any], questions: list[dict[str, Any]], slot_key: str, option_id: str) -> str:
    if answers.get(slot_key):
        return str(answers[slot_key])
    question = next((q for q in questions if q.get("slot_key") == slot_key), {})
    options = question.get("options") or (FORCING_QUESTIONS.get(slot_key) or {}).get("options") or []
    chosen = next((o for o in options if o.get("id") == option_id), None)
    if chosen:
        return str(chosen.get("label") or option_id)
    return "Not answered yet"


def infer_from_office_hours(
    *,
    idea: str = "",
    name: str = "",
    one_liner: str = "",
    answers: dict[str, Any] | None = None,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.clarify.generator import product_referent

    answers = answers or {}
    questions = questions or []
    product = product_referent(idea=idea, name=name, one_liner=one_liner)

    demand = _option_id(answers, questions, "goal_primary")
    status = _option_id(answers, questions, "market_reach")
    audience = _option_id(answers, questions, "audience")
    wedge = _option_id(answers, questions, "mvp_scope")
    watch = _option_id(answers, questions, "gtm_order")
    future = _option_id(answers, questions, "non_goals")

    if demand == "opt_a":
        verdict = "go_wedge"
        gtm = "product_first"
        monetization = "Charge the first design partner this week for one finished workflow."
        channels = "The people you already talk to, five named conversations, not a channel mix."
    elif demand == "opt_b":
        verdict = "test_demand"
        gtm = "market_first"
        monetization = "Do not charge until someone would be upset if this disappeared."
        channels = "Landing page plus ten honest conversations before any product build."
    else:
        verdict = "research_first"
        gtm = "market_first"
        monetization = "No pricing until a named person loses sleep over the current workaround."
        channels = "Watch one person struggle with the status quo before you write copy."

    if wedge == "opt_b":
        gtm = "market_first"
        wedge_line = (
            f"A thin landing and waitlist for {product}, coding stays idle until demand is real."
        )
    elif wedge == "opt_c":
        wedge_line = f"One workflow of {product} for one role. A broader first version waits."
    else:
        wedge_line = (
            f"One end-to-end workflow of {product} a design partner would pay for this week."
        )

    if audience == "opt_a":
        first_user = "The person you can already name. Messaging and MVP target that role only."
    elif audience == "opt_b":
        first_user = "A tight niche you know. Next: a list of five named humans, not a category."
    else:
        first_user = "Not a category. Refuse generic copy until one role has a name and a job."

    if status == "opt_a":
        status_quo = "Position against the named workaround, spreadsheet, inbox, agency, or duct tape."
    elif status == "opt_b":
        status_quo = "Treat the named products as the foil. Differentiate in copy and in the wedge."
    else:
        status_quo = (
            "Nothing exists is a red flag unless you can name a person who loses sleep over it."
        )

    if watch == "opt_a":
        observation = "Build the surprising behavior you already watched, not the pitch."
    elif watch == "opt_b":
        observation = "Demos taught the pitch. Schedule one silent watch-session before expanding scope."
    else:
        observation = "Observation is a kill-criteria item. Watch someone struggle before you scale build."

    if future == "opt_a":
        future_fit = "Write the user-world change into the charter and keep scope pointed at it."
        wont = ["Anything that does not serve that 3-year user-world change"]
    elif future == "opt_b":
        future_fit = "Growing market is not a vision. Require a user-world thesis before expanding scope."
        wont = ["Platform expansion until the user-world thesis is written"]
    else:
        future_fit = "This is a now-problem. Fine for a wedge. Explicitly not a platform bet."
        wont = ["A platform bet", "Features for a second user type"]

    next_five: list[str] = []
    if verdict == "research_first":
        next_five.append("Name one person who already cobbles a workaround and watch them do it.")
    if audience != "opt_a":
        next_five.append("Write five named humans who would use this this month.")
    if watch != "opt_a":
        next_five.append("Sit behind one user without helping. File the surprise.")
    if verdict == "test_demand" or wedge == "opt_b":
        next_five.append("Run a concierge or waitlist test before writing product code.")
    else:
        next_five.append(f"Ship the one-workflow wedge for {product}, nothing else.")
    next_five.append("Lock this plan. Monetization, channels, and tone stay inferred until you override them.")
    extras = [
        "Write what you will not build this month.",
        "Pick the first design partner and a date to charge or walk away.",
        "Keep the landing page specific to one role and one workaround.",
    ]
    for extra in extras:
        if len(next_five) >= 5:
            break
        if extra not in next_five:
            next_five.append(extra)

    stages = []
    for slot_key, spec in FORCING_QUESTIONS.items():
        option_id = _option_id(answers, questions, slot_key)
        question = next((q for q in questions if q.get("slot_key") == slot_key), {})
        options = question.get("options") or spec["options"]
        chosen = next((o for o in options if o.get("id") == option_id), None)
        provenance = ((answers.get("_answer_provenance") or {}).get(slot_key) or {})
        stages.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "slot_key": slot_key,
                "answer": _answer_text(answers, questions, slot_key, option_id),
                **provenance,
                "implication": "Suggested option, test it before treating it as evidence." if provenance.get("provenance") == "suggested_choice"
                               else (chosen or {}).get("implication") or spec["why"],
            }
        )

    copy = VERDICT_COPY[verdict]
    inferred = {
        "gtm_order": gtm,
        "monetization": monetization,
        "channels": channels,
        "brand_tone": "clear",
        "integrations": [],
        "non_goals": "; ".join(wont),
        "mvp_scope": wedge_line,
        "audience": first_user,
        "llm_runtime": "Use ours, DeepSeek v4 Flash (included)",
    }
    return {
        "process": "gstack_office_hours",
        "product": product,
        "title": f"GStack plan for {product}",
        "verdict": verdict,
        "verdict_label": copy["label"],
        "thesis": copy["thesis"],
        "wedge": wedge_line,
        "first_user": first_user,
        "status_quo": status_quo,
        "observation": observation,
        "future_fit": future_fit,
        "wont_build": wont,
        "next_five": next_five[:5],
        "stages": stages,
        "assumptions": [{"slot_key": stage["slot_key"], "answer": stage["answer"],
                         "note": stage["assumption_note"]} for stage in stages if stage.get("provenance") == "suggested_choice"],
        "inferred": inferred,
    }


def apply_inferred_answers(answers: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Fill charter slots the founder was not asked. Office-hours answers win."""
    merged = dict(answers)
    for key, value in (plan.get("inferred") or {}).items():
        if key not in merged or not merged[key]:
            merged[key] = value
    return merged
