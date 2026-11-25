"""Question generation with invariants (06 §1).

Produces founder-facing copy only — never expose slot keys like goal_primary.
"""

from __future__ import annotations

import re
from typing import Any

from core.ids import new_id

from app.clarify.slots import LLM_RUNTIME_SLOT, SlotDef
from app.gstack.methodology import (
    FORCING_QUESTIONS,
    office_hours_options,
    office_hours_prompt,
    office_hours_why,
)


# Human options per slot. Exactly one recommended. Always 3 structured choices.
_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "goal_primary": [
        {
            "id": "opt_a",
            "label": "Get 20 paying customers in 90 days",
            "recommended": True,
            "implication": "We’ll optimize the MVP and messaging for early revenue.",
        },
        {
            "id": "opt_b",
            "label": "Validate demand before charging",
            "recommended": False,
            "implication": "We’ll prioritize waitlists and interviews over payments.",
        },
        {
            "id": "opt_c",
            "label": "Ship a polished product people love",
            "recommended": False,
            "implication": "We’ll invest more in UX before pushing growth.",
        },
    ],
    "audience": [
        {
            "id": "opt_a",
            "label": "A narrow first niche I already know",
            "recommended": True,
            "implication": "Faster learning, clearer messaging, smaller initial market.",
        },
        {
            "id": "opt_b",
            "label": "Anyone in the broad category",
            "recommended": False,
            "implication": "Bigger reach, but messaging and features stay generic longer.",
        },
        {
            "id": "opt_c",
            "label": "Businesses / operators, not consumers",
            "recommended": False,
            "implication": "Sales cycles get longer; pricing and onboarding change.",
        },
    ],
    "monetization": [
        {
            "id": "opt_a",
            "label": "Simple subscription",
            "recommended": True,
            "implication": "Predictable revenue; needs a clear paid value moment.",
        },
        {
            "id": "opt_b",
            "label": "Free first, monetize later",
            "recommended": False,
            "implication": "Easier adoption; revenue is deferred.",
        },
        {
            "id": "opt_c",
            "label": "Take a cut of each transaction",
            "recommended": False,
            "implication": "Needs payments + trust; better for marketplaces.",
        },
    ],
    "mvp_scope": [
        {
            "id": "opt_a",
            "label": "One core workflow end-to-end",
            "recommended": True,
            "implication": "Smallest shippable product; everything else waits.",
        },
        {
            "id": "opt_b",
            "label": "A thin demo that looks complete",
            "recommended": False,
            "implication": "Great for market tests; weaker for real retention.",
        },
        {
            "id": "opt_c",
            "label": "Several features for different user types",
            "recommended": False,
            "implication": "Slower to ship; higher coordination cost.",
        },
    ],
    "market_reach": [
        {
            "id": "opt_a",
            "label": "A tiny beachhead I can reach this month",
            "recommended": True,
            "implication": "Sharper messaging; we ignore everyone outside that niche for now.",
        },
        {
            "id": "opt_b",
            "label": "The whole category, cast a wide net",
            "recommended": False,
            "implication": "More traffic potential; copy and features stay generic longer.",
        },
        {
            "id": "opt_c",
            "label": "Out-compete the named alternatives head-on",
            "recommended": False,
            "implication": "We’ll study listed competitors and differentiate explicitly.",
        },
    ],
    "gtm_order": [
        {
            "id": "opt_a",
            "label": "Product first, build, then launch",
            "recommended": True,
            "implication": "You can demo something real; market learning comes after.",
        },
        {
            "id": "opt_b",
            "label": "Market first, test demand, then build",
            "recommended": False,
            "implication": "Cheaper learning; you may pivot before writing much code.",
        },
        {
            "id": "opt_c",
            "label": "Parallel, light landing page while building",
            "recommended": False,
            "implication": "More moving parts; needs tighter focus.",
        },
    ],
    "data_sensitivity": [
        {
            "id": "opt_a",
            "label": "Normal account data only",
            "recommended": True,
            "implication": "Standard auth and privacy practices are enough for MVP.",
        },
        {
            "id": "opt_b",
            "label": "Payments or financial data",
            "recommended": False,
            "implication": "We’ll add payment providers and stricter controls.",
        },
        {
            "id": "opt_c",
            "label": "Health, kids, or highly regulated data",
            "recommended": False,
            "implication": "Compliance work becomes part of the MVP.",
        },
    ],
    "channels": [
        {
            "id": "opt_a",
            "label": "Start with one channel I can actually post on",
            "recommended": True,
            "implication": "Focused experiments; less setup and spend.",
        },
        {
            "id": "opt_b",
            "label": "Landing page + email waitlist",
            "recommended": False,
            "implication": "Good for market-first; needs an email provider setup.",
        },
        {
            "id": "opt_c",
            "label": "Several social channels at once",
            "recommended": False,
            "implication": "More reach, more credentials and approval work for you.",
        },
    ],
    "brand_tone": [
        {
            "id": "opt_a",
            "label": "Clear and friendly",
            "recommended": True,
            "implication": "Easy to trust; works for most early products.",
        },
        {
            "id": "opt_b",
            "label": "Bold and opinionated",
            "recommended": False,
            "implication": "Stands out; can alienate cautious buyers.",
        },
        {
            "id": "opt_c",
            "label": "Professional and formal",
            "recommended": False,
            "implication": "Better for B2B; may feel stiff for consumers.",
        },
    ],
    "success_metric": [
        {
            "id": "opt_a",
            "label": "Weekly active users who complete the core action",
            "recommended": True,
            "implication": "We’ll judge the MVP by real usage, not vanity metrics.",
        },
        {
            "id": "opt_b",
            "label": "Signups / waitlist growth",
            "recommended": False,
            "implication": "Good early signal; doesn’t prove retention.",
        },
        {
            "id": "opt_c",
            "label": "Revenue",
            "recommended": False,
            "implication": "Clearest business signal; needs payments in the MVP.",
        },
    ],
    "budget_ceiling": [
        {
            "id": "opt_a",
            "label": "Keep spend under $50/month for now",
            "recommended": True,
            "implication": "We’ll prefer free tiers and dry-run marketing.",
        },
        {
            "id": "opt_b",
            "label": "Up to a few hundred dollars if it unlocks learning",
            "recommended": False,
            "implication": "We can arm paid channels carefully.",
        },
        {
            "id": "opt_c",
            "label": "Spend what it takes to move fast",
            "recommended": False,
            "implication": "Higher burn; needs tighter founder oversight.",
        },
    ],
    "llm_runtime": [
        {
            "id": "opt_a",
            "label": "Use ours, DeepSeek v4 Flash (included)",
            "recommended": True,
            "implication": "We pay for DeepSeek v4 Flash. You can switch or add your own key later in Settings.",
        },
        {
            "id": "opt_b",
            "label": "I'll use my own API key",
            "recommended": False,
            "implication": "Pick a provider and model, then paste the key. We store an encrypted vault reference, never the raw key in chat or the database.",
        },
        {
            "id": "opt_c",
            "label": "Local model on this machine (Ollama)",
            "recommended": False,
            "implication": "Calls stay on your computer. Set the model name after you choose this.",
        },
    ],
}

_WHY: dict[str, str] = {
    "goal_primary": "This decides what “validated” means before we spend build time.",
    "audience": "A clear first user keeps messaging and features focused.",
    "monetization": "This changes architecture (payments) and what we ship first.",
    "mvp_scope": "Locks the build boundary so the first version stays shippable.",
    "market_reach": "Sets how narrow or wide the first market test should be.",
    "gtm_order": "Chooses whether we build first or test the market first.",
    "data_sensitivity": "Sets how careful we must be with auth, privacy, and compliance.",
    "channels": "Determines which accounts you’ll need to create.",
    "brand_tone": "Guides copy, landing pages, and marketing agents.",
    "success_metric": "What the company will measure when it improves itself.",
    "budget_ceiling": "Caps token spend and channel spend so nothing runs away overnight.",
    "integrations": "Keeps day-one wiring honest, only must-haves.",
    "non_goals": "Protects focus by naming what we will not build yet.",
    "llm_runtime": "The company thinks with a model. Ours is DeepSeek v4 Flash. You can bring your own key instead.",
}


def _fallback_options(slot: SlotDef) -> list[dict[str, Any]]:
    label = slot.key.replace("_", " ")
    return [
        {
            "id": "opt_a",
            "label": f"Recommended choice for {label}",
            "recommended": True,
            "implication": "We’ll treat this as the default and move on.",
        },
        {
            "id": "opt_b",
            "label": f"More cautious approach to {label}",
            "recommended": False,
            "implication": "Slower, lower risk.",
        },
        {
            "id": "opt_c",
            "label": f"More ambitious approach to {label}",
            "recommended": False,
            "implication": "Faster signal, more complexity.",
        },
    ]


_LEAD_IN = re.compile(
    r"^(?:i\s+)?(?:(?:was|am|'m|’m)\s+)?(?:thinking of|looking to|hoping to|planning to|"
    r"trying to|going to|gonna|wanna|want to|would like to|i'd like to|i’d like to)\s+"
    r"(?:build(?:ing)?|creat(?:e|ing)|mak(?:e|ing)|launch(?:ing)?|start(?:ing)?)\s+"
    r"(?:a|an|the)?\s*",
    re.IGNORECASE,
)
_BARE_BUILD = re.compile(
    r"^(?:build(?:ing)?|creat(?:e|ing)|mak(?:e|ing)|launch(?:ing)?)\s+(?:a|an|the)?\s+",
    re.IGNORECASE,
)
_CLAUSE = re.compile(
    r"\s*(?:,|\.|—|–|\s+offering\b|\s+where\b|\s+that\b|\s+which\b|\s+because\b|\s+and then\b)",
    re.IGNORECASE,
)
_GENERIC_NAMES = {"untitled", "untitled venture", "new venture", "this idea", "this product"}


def product_referent(*, idea: str = "", name: str = "", one_liner: str = "") -> str:
    """Noun phrase questions can talk about — never a chopped first-person dump."""
    named = " ".join((name or "").split())
    if named and named.lower() not in _GENERIC_NAMES:
        return named
    liner = " ".join((one_liner or "").split())
    if liner and len(liner) <= 72 and not liner.lower().startswith("i "):
        return liner
    text = " ".join((idea or "").split())
    if not text:
        return "this product"
    stripped = _LEAD_IN.sub("", text, count=1)
    stripped = _BARE_BUILD.sub("", stripped, count=1).strip(" \"'")
    if not stripped:
        return "this product"
    cut = _CLAUSE.split(stripped, maxsplit=1)[0].strip(" \"'")
    if not cut or cut.lower().startswith("i ") or len(cut) < 3:
        return "this product"
    if len(cut) > 56:
        words = cut.split()
        kept: list[str] = []
        for word in words:
            trial = (" ".join(kept + [word])).strip()
            if len(trial) > 56:
                break
            kept.append(word)
        cut = " ".join(kept) or cut[:56].rsplit(" ", 1)[0]
    return cut or "this product"


def _personalize(
    slot: SlotDef,
    *,
    idea: str,
    research: dict[str, Any] | None,
    name: str = "",
    one_liner: str = "",
) -> tuple[str, str, list[dict[str, Any]]]:
    """Rewrite prompt / why / options using a comprehended product phrase + research."""
    product = product_referent(idea=idea, name=name, one_liner=one_liner)
    def _name(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("name") or item.get("website") or "").strip()
        return str(item or "").strip()

    competitors = [n for n in (_name(c) for c in (research or {}).get("competitors") or []) if n][:3]
    demand = str((research or {}).get("demand") or "unknown")
    options = [dict(o) for o in (_OPTIONS.get(slot.key) or _fallback_options(slot))]
    prompt = office_hours_prompt(slot.key, product, slot.prompt)
    why = office_hours_why(slot.key, _WHY.get(slot.key, "This shapes the plan for your MVP."))
    gstack_opts = office_hours_options(slot.key, {"idea": idea})
    if gstack_opts is not None:
        options = gstack_opts

    if slot.key == "market_reach":
        if competitors:
            named = ", ".join(competitors[:2])
            why = (
                f"{why} Early research already surfaced {named}, the status quo "
                "may be those products or the workaround underneath them."
            )
            if not options:
                prompt = f"Research surfaced {named}. {prompt}"
            if options:
                options[2]["label"] = f"Differentiate hard against {competitors[0]}"
                options[2]["implication"] = (
                    f"We’ll treat {competitors[0]} as the foil in copy and the plan."
                )
        else:
            why = f"{why} Current demand read is {demand}."
    elif slot.key == "monetization":
        prompt = f"How should {product} make money in the first version?"
    elif slot.key == "channels":
        prompt = f"Where will the first 20 users of {product} come from?"

    return prompt, why, options


def _gstack_meta(slot_key: str) -> dict[str, Any]:
    spec = FORCING_QUESTIONS.get(slot_key)
    if not spec:
        return {}
    return {
        "gstack_id": spec["id"],
        "gstack_name": spec["name"],
        "process": "gstack_office_hours",
    }


def generate_questions(
    slots: list[SlotDef],
    *,
    idea: str = "",
    name: str = "",
    one_liner: str = "",
    research: dict[str, Any] | None = None,
    include_llm_runtime: bool = True,
) -> list[dict[str, Any]]:
    rows = list(slots)
    if include_llm_runtime and not any(slot.key == "llm_runtime" for slot in rows):
        rows.append(LLM_RUNTIME_SLOT)
    questions: list[dict[str, Any]] = []
    for i, slot in enumerate(rows):
        prompt, why, options = _personalize(
            slot, idea=idea, name=name, one_liner=one_liner, research=research
        )
        # Harden invariants
        if slot.key not in FORCING_QUESTIONS and sum(1 for o in options if o.get("recommended")) != 1:
            options[0]["recommended"] = True
            for o in options[1:]:
                o["recommended"] = False
        questions.append(
            {
                "id": new_id("qst"),
                "slot_key": slot.key,
                "kind": slot.kind.value,
                "position": i,
                "prompt": prompt,
                "why_asked": why,
                "options": options,
                **({"free_text_only": True} if not options else {}),
                "allow_other": True,
                "selection_mode": "single",
                "evpi": slot.decision_impact,
                "ask_cost": 0.1,
                "answerability": 0.9,
                **_gstack_meta(slot.key),
            }
        )
    return questions


def refresh_question_copy(
    questions: list[dict[str, Any]] | None,
    *,
    idea: str = "",
    name: str = "",
    one_liner: str = "",
    research: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Keep question ids; rewrite prompts so they talk about the product, not a chopped dump."""
    from app.clarify.slots import SLOT_TAXONOMY
    from app.clarify.dilemmas import _founder_request

    by_key = {slot.key: slot for slot in SLOT_TAXONOMY}
    rows = list(questions or [])
    for q in rows:
        slot = by_key.get(str(q.get("slot_key") or ""))
        if not slot:
            continue
        prompt, why, _options = _personalize(
            slot, idea=idea, name=name, one_liner=one_liner, research=research
        )
        q["prompt"] = prompt
        q["why_asked"] = why
        if slot.key in FORCING_QUESTIONS and (not _options or any(
                _founder_request(str(option.get("label") or "")) for option in q.get("options") or [])):
            q.update(options=_options, free_text_only=not _options)
        q.update(_gstack_meta(slot.key))
    return rows


def ensure_llm_runtime_question(
    questions: list[dict[str, Any]] | None,
    *,
    idea: str = "",
) -> list[dict[str, Any]]:
    """Add the model/key question to an already-generated round."""
    rows = list(questions or [])
    if any(q.get("slot_key") == "llm_runtime" for q in rows):
        return rows
    extra = generate_questions([LLM_RUNTIME_SLOT], idea=idea)
    if extra:
        extra[0]["position"] = len(rows)
        rows.append(extra[0])
    return rows


def assert_invariants(questions: list[dict[str, Any]]) -> None:
    seen_slots: set[str] = set()
    for q in questions:
        opts = q["options"]
        assert 3 <= len(opts) <= 5 or not opts and q.get("free_text_only") and q["slot_key"] in FORCING_QUESTIONS
        recommended = sum(1 for option in opts if option.get("recommended"))
        assert recommended <= 1 if q["slot_key"] in FORCING_QUESTIONS else recommended == 1
        assert q.get("allow_other") is True
        assert all(o.get("implication") for o in opts)
        assert q.get("why_asked")
        assert "goal_primary" not in q["why_asked"]
        assert q["slot_key"] not in seen_slots
        seen_slots.add(q["slot_key"])
    kinds = [q["kind"] for q in questions]
    if "goal" in kinds:
        assert kinds[0] == "goal"
