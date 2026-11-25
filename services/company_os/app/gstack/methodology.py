"""Ported gstack algorithms (MIT © 2026 Garry Tan / garrytan/gstack).

The office-hours forcing questions and the CEO-review prime directives are LOADED
from the vendored upstream skill files in third_party/gstack/ (LICENSE, NOTICE and
PINNED_SHA live there); nothing about them is paraphrased here. Other constants
below (investigate, QA, CSO...) are older ports from vendor/skills/gstack.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# app/gstack/methodology.py -> repo root (also /workspace in the container image)
THIRD_PARTY = Path(__file__).resolve().parents[4] / "third_party" / "gstack"
_SKILLS = THIRD_PARTY / "openclaw" / "skills"


@lru_cache(maxsize=None)
def _skill(name: str) -> str:
    return (_SKILLS / f"gstack-openclaw-{name}" / "SKILL.md").read_text(encoding="utf-8")


def pinned_sha() -> str:
    return (THIRD_PARTY / "PINNED_SHA").read_text().strip()


def _between(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head : text.index(end, head)].strip()


def office_hours_text() -> str:
    """Upstream startup-mode diagnostic: principles, anti-sycophancy, the six questions."""
    return _between(_skill("office-hours"), "### Operating Principles", "## Phase 2B")


def ceo_review_text() -> str:
    """Upstream CEO review philosophy, prime directives and scope-challenge steps."""
    return _between(_skill("ceo-review"), "## Philosophy", "## Step 0: Nuclear")


def upstream_forcing_questions() -> dict[str, dict[str, str]]:
    """{"Q1": {name, ask, push, flags}} parsed from the vendored office-hours skill."""
    out: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"#### (Q\d+): ([^\n]+)\n(.*?)(?=\n#### Q|\n\*\*Smart-skip)", office_hours_text(), re.S):
        body = match.group(3)
        field = lambda label: (re.search(rf"\*\*{label}:\*\*\s*(.+)", body) or [None, ""])[1].strip()  # noqa: E731
        out[match.group(1)] = {"name": match.group(2).strip(), "ask": field("Ask").strip('"'),
                               "push": field("Push until you hear"), "flags": field("Red flags")}
    return out

# ---------------------------------------------------------------------------
# Plain words founders read; the upstream names stay internal (also in apps/console/src/lib/validationCopy.ts).
FOUNDER_TOPICS = {
    "Demand Reality": "Customer demand",
    "Status Quo": "Current workarounds",
    "Desperate Specificity": "Who needs this most",
    "Narrowest Wedge": "Smallest useful version",
    "Observation": "What you have seen firsthand",
    "Future-Fit": "Future needs",
}


def founder_topic(name: str) -> str:
    return FOUNDER_TOPICS.get(name, name)


# Office Hours — six forcing questions (startup mode)
# ---------------------------------------------------------------------------

_APP_OPTIONS: dict[str, dict[str, Any]] = {
    "goal_primary": {
        "id": "Q1",
        "name": "Demand Reality",
        "prompt": (
            "What's the strongest evidence of real demand for {product}? "
            "Not a waitlist or “that's interesting”, someone who would be upset if it disappeared tomorrow."
        ),
        "why": (
            "Interest is not demand. Waitlists and 'that's interesting' don't count. "
            "Behavior, money, and panic when it breaks count."
        ),
        "options": [
            {
                "id": "opt_a",
                "label": "Someone already pays, begs for it, or built a workflow around a hack",
                "recommended": True,
                "implication": "Real demand. We'll prove the narrowest paid wedge first.",
            },
            {
                "id": "opt_b",
                "label": "People say it's interesting / signed a waitlist, no one has paid",
                "recommended": False,
                "implication": "Treat as a hypothesis. Interviews and a concierge test before build.",
            },
            {
                "id": "opt_c",
                "label": "Mostly my conviction, I haven't watched a real user struggle yet",
                "recommended": False,
                "implication": "Status-quo research first. Don't build the platform.",
            },
        ],
    },
    "market_reach": {
        "id": "Q2",
        "name": "Status Quo",
        "prompt": (
            "What are people doing right now, even badly, instead of {product}? "
            "What does that workaround cost them?"
        ),
        "why": (
            "The status quo is the real competitor: the spreadsheet-and-Slack workaround. "
            "If nobody is cobbling anything, the pain may not be real."
        ),
        "options": [
            {
                "id": "opt_a",
                "label": "A named workaround (spreadsheet, inbox, agency, duct-taped tools)",
                "recommended": True,
                "implication": "We'll position against that workaround, not a category.",
            },
            {
                "id": "opt_b",
                "label": "Named products already in the space, we'll differentiate hard",
                "recommended": False,
                "implication": "Copy and plan treat those products as the foil.",
            },
            {
                "id": "opt_c",
                "label": "You have not checked the current workaround yet",
                "recommended": False,
                "implication": "Red flag unless we can name a person who loses sleep over it.",
            },
        ],
    },
    "audience": {
        "id": "Q3",
        "name": "Desperate Specificity",
        "prompt": (
            "Who needs {product} most as a real person? Title, what gets them "
            "promoted or fired, not a category."
        ),
        "why": "You can't email 'healthcare enterprises'. A first user has a name and a job.",
        "options": [
            {
                "id": "opt_a",
                "label": "A person I can name or whose exact role I already talk to",
                "recommended": True,
                "implication": "Messaging and MVP target that one role this week.",
            },
            {
                "id": "opt_b",
                "label": "A tight niche I know, even if I don't have a name yet",
                "recommended": False,
                "implication": "We'll force a design-partner list of 5 named humans next.",
            },
            {
                "id": "opt_c",
                "label": "A broad category (SMBs, marketers, developers)",
                "recommended": False,
                "implication": "Too wide. We'll refuse generic copy until the niche is named.",
            },
        ],
    },
    "mvp_scope": {
        "id": "Q4",
        "name": "Narrowest Wedge",
        "prompt": (
            "What's the smallest version of {product} someone would pay for this week, "
            "not after a full platform exists?"
        ),
        "why": (
            "Narrow beats wide, early. 'We need the full platform first' usually means "
            "the value proposition isn't clear yet."
        ),
        "options": [
            {
                "id": "opt_a",
                "label": "One workflow end-to-end that a design partner would pay for this week",
                "recommended": True,
                "implication": "Everything else waits. Concierge is allowed.",
            },
            {
                "id": "opt_b",
                "label": "A thin landing + waitlist to test demand before building",
                "recommended": False,
                "implication": "Market-first. Coding agents stay idle until demand evidence.",
            },
            {
                "id": "opt_c",
                "label": "A broader first version for a few user types",
                "recommended": False,
                "implication": "We'll still cut to one wedge unless you override in writing.",
            },
        ],
    },
    "gtm_order": {
        "id": "Q5",
        "name": "Observation",
        "prompt": (
            "Have you watched someone try to solve this without helping them? "
            "What did they do that surprised you?"
        ),
        "why": (
            "Surveys and demo calls teach nothing. Sitting behind someone while they "
            "struggle teaches everything. Surprise is the gold."
        ),
        "options": [
            {
                "id": "opt_a",
                "label": "Yes, I watched unscripted use and it changed what I think the product is",
                "recommended": True,
                "implication": "We'll build the surprising behavior, not the pitch.",
            },
            {
                "id": "opt_b",
                "label": "I've done demos or surveys, not silent observation",
                "recommended": False,
                "implication": "Next five includes a watch-session with a real user.",
            },
            {
                "id": "opt_c",
                "label": "Not yet, I'm going on the pitch",
                "recommended": False,
                "implication": "Observation is a kill-criteria item before we scale build.",
            },
        ],
    },
    "non_goals": {
        "id": "Q6",
        "name": "Future-Fit",
        "prompt": (
            "If the user's world looks different in three years, does {product} "
            "become more essential or less? What specifically changes?"
        ),
        "why": "Growth rate is not a vision. Every competitor can cite the same TAM slide.",
        "options": [
            {
                "id": "opt_a",
                "label": "A specific change in the user's world that makes this more essential",
                "recommended": True,
                "implication": "We'll write that thesis into the charter and keep scope pointed at it.",
            },
            {
                "id": "opt_b",
                "label": "The market is growing, that's enough for now",
                "recommended": False,
                "implication": "We'll still require a user-world thesis before expanding scope.",
            },
            {
                "id": "opt_c",
                "label": "This is a now-problem; 3-year fit is unknown",
                "recommended": False,
                "implication": "Fine for a wedge. Explicitly not a platform bet.",
            },
        ],
    },
}


def _load_forcing_questions() -> dict[str, dict[str, Any]]:
    upstream = upstream_forcing_questions()
    result = {}
    for slot, spec in _APP_OPTIONS.items():
        up = upstream[spec["id"]]
        result[slot] = {**spec, "name": up["name"], "prompt": up["ask"],
                        "why": f"Push until you hear: {up['push']} Red flags: {up['flags']}"}
    return result


FORCING_QUESTIONS: dict[str, dict[str, Any]] = _load_forcing_questions()


def office_hours_prompt(slot_key: str, product: str, fallback: str) -> str:
    spec = FORCING_QUESTIONS.get(slot_key)
    if not spec:
        return fallback
    # The upstream question is generic; name the venture so the founder sees what it is about.
    return f"About {product or 'this product'}: {spec['prompt']}"


def office_hours_why(slot_key: str, fallback: str) -> str:
    spec = FORCING_QUESTIONS.get(slot_key)
    return str(spec["why"]) if spec else fallback


def office_hours_options(slot_key: str, snapshot: dict | None = None) -> list[dict[str, Any]] | None:
    spec = FORCING_QUESTIONS.get(slot_key)
    if not spec:
        return None
    from app.clarify.dilemmas import contextual_choices, venture_context
    candidates = contextual_choices(spec["id"], snapshot or {})
    if not candidates:
        return []
    audience, problem = venture_context(snapshot or {})
    options = [dict(item) for item in spec["options"]]
    for index, option in enumerate(options):
        option.update(label=candidates[index], recommended=False,
            implication=f"Your description of {problem} for {audience} matters, not selecting this option.")
        option["requires_detail"] = index > 0 if spec["id"] == "Q1" else index < (2 if spec["id"] == "Q2" else 1)
        option["detail_prompt"] = "Describe the evidence: who, what happened, and what did they do?" if spec["id"] == "Q1" else "Name the tools or habits and describe how they are used." if spec["id"] == "Q2" else "Describe that moment: who, when, what happened?"
    return options


# ---------------------------------------------------------------------------
# CEO review — four scope modes + prime directives
# ---------------------------------------------------------------------------

CEO_MODES = (
    "scope_expansion",
    "selective_expansion",
    "hold_scope",
    "scope_reduction",
)

CEO_PRIME_DIRECTIVES = (
    *(m.group(1).strip() for m in re.finditer(
        r"^\d+\. (.+)$", _between(_skill("ceo-review"), "## Prime Directives", "## Cognitive"), re.M)),
    # Upstream's "Critical rule" for every mode.
    "The founder is 100% in control of every scope change, never silent add or cut.",
)

CEO_REVIEW_SECTIONS = (
    "architecture",
    "error_rescue",
    "security",
    "data_flow_edges",
    "code_quality",
    "tests",
    "observability",
    "state",
    "api_contract",
    "performance",
    "design_ux",
)


def default_ceo_mode(*, stage: str | None = None, files_touched: int = 0) -> str:
    """gstack context defaults: greenfield → expansion; hotfix → hold; large plan → reduce."""
    stage = (stage or "").lower()
    if files_touched > 15:
        return "scope_reduction"
    if stage in {"draft", "intake", "classifying", "clarifying", "org_planning"}:
        return "scope_expansion"
    if stage in {"building", "demand_testing"}:
        return "selective_expansion"
    return "hold_scope"


# ---------------------------------------------------------------------------
# Autoplan — six decision principles
# ---------------------------------------------------------------------------

AUTOPLAN_PRINCIPLES = {
    "P1": "Highest completeness wins when approaches tie on outcome.",
    "P2": "Approve scope that sits inside the blast radius and ships in <1 day.",
    "P3": "Defer work outside the blast radius to a written backlog, do not sneak it in.",
    "P4": "Reject duplicates of work already on the slate or in the charter.",
    "P5": "When in doubt, pick the simpler explicit design over the clever one.",
    "P6": "Accept reasonable premises; challenge only the ones that are clearly wrong. Dual-voice when a second model is available.",
}


def pick_approach(approaches: list[dict[str, Any]]) -> dict[str, Any]:
    """Autoplan override: highest completeness, then simplest (P1 then P5)."""
    if not approaches:
        raise ValueError("autoplan requires at least one approach")

    def score(item: dict[str, Any]) -> tuple[int, int]:
        completeness = int(item.get("completeness") or 0)
        # Lower effort rank is simpler.
        effort = str(item.get("effort") or "M").upper()
        simple = {"S": 3, "M": 2, "L": 1, "XL": 0}.get(effort, 1)
        return (completeness, simple)

    return max(approaches, key=score)


# ---------------------------------------------------------------------------
# Investigate — Iron Law
# ---------------------------------------------------------------------------

INVESTIGATE_IRON_LAW = "NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST."

INVESTIGATE_PHASES = (
    "collect_symptoms",
    "read_the_code",
    "check_recent_changes",
    "reproduce",
    "hypothesis",
    "test_hypothesis",
    "fix_root_cause",
    "regression_test",
    "verify",
)

INVESTIGATE_PATTERNS = (
    "race_condition",
    "nil_propagation",
    "state_corruption",
    "integration_failure",
    "configuration_drift",
    "stale_cache",
)

STRIKE_LIMIT = 3


# ---------------------------------------------------------------------------
# Design anti-slop (design-consultation)
# ---------------------------------------------------------------------------

DESIGN_SLOP = (
    "purple/violet gradients as the default accent",
    "3-column feature grid with icons in colored circles",
    "centered everything with uniform spacing",
    "uniform bubbly border-radius on all elements",
    "gradient buttons as the primary CTA",
    "generic stock-photo-style hero",
    "system-ui / Inter as the only typefaces",
    '"Built for X" / "Designed for Y" marketing copy',
    "AI-powered / seamless / frictionless filler",
)


# ---------------------------------------------------------------------------
# CSO — zero-noise gates
# ---------------------------------------------------------------------------

CSO_GATES = {
    "daily_confidence": 8,
    "comprehensive_confidence": 2,
    "require_exploit_scenario": True,
    "false_positive_exclusions": 17,
}


# ---------------------------------------------------------------------------
# QA — test → fix → verify
# ---------------------------------------------------------------------------

QA_LOOP = ("test", "fix_atomic", "reverify", "regression_test", "ship_readiness")


# ---------------------------------------------------------------------------
# Role specialists — map Company OS roles onto gstack seats
# ---------------------------------------------------------------------------

_ROLE_SPECIALISTS: dict[str, str] = {
    "orchestrator": (
        "You are the YC office-hours partner AND autoplan lead. Demand reality over "
        "interest. Narrowest wedge first. Never rubber-stamp a platform vision. "
        "Every session ends with one concrete next action, not a strategy deck."
    ),
    "architect": (
        "You are the eng-manager reviewer. Lock architecture, data flow (happy path "
        "plus nil/empty/upstream-error), named errors, and tests before code. "
        "Diagrams are mandatory for non-trivial flows."
    ),
    "builder": (
        "You are a careful implementer under the Investigate Iron Law: no fixes "
        "without root cause. Minimal diff. Regression test that fails without the "
        "fix. Stop after 3 failed hypotheses and escalate."
    ),
    "reviewer": (
        "You are the staff-engineer pre-landing reviewer. Find bugs that pass CI "
        "but blow up in production: SQL safety, LLM trust boundaries, conditional "
        "side effects, completeness gaps. Auto-fix the obvious; ask on the rest."
    ),
    "qa": (
        "You are the QA lead: test → fix → re-verify. Atomic commits per fix. "
        "Auto-generate a regression test for every fix. Report ship-readiness, "
        "not a pile of unverified opinions."
    ),
    "designer": (
        "You are the design partner. Rate each dimension 0-10 and say what a 10 "
        "looks like. Reject AI slop: purple gradients, 3-column icon grids, "
        "centered-everything, Inter-only type, 'Built for X' copy."
    ),
    "copywriter": (
        "Write like a human who knows this exact user. No 'seamless', 'AI-powered', "
        "or 'Built for X'. Specificity is the only currency."
    ),
    "researcher": (
        "Demand evidence over interest. Status quo is the competitor. Name the "
        "human, the workaround, and the surprise from watching real use."
    ),
    "growth": (
        "Wedge first. One channel you can actually post on. Design partners before "
        "paid spend. Love is not demand."
    ),
    "ops": (
        "You are the release engineer / SRE: sync, tests, coverage, PR, canary. "
        "Nothing is shipped that cannot be verified."
    ),
    "analyst": (
        "Proxy skepticism: are the metrics still serving users or have they become "
        "self-referential? Instrument the wedge, not vanity."
    ),
    "support": (
        "The user's words beat the founder's pitch. Watch, don't demo. File the "
        "surprise; that is often the real product."
    ),
}


def specialist_for_role(role: str) -> str:
    return _ROLE_SPECIALISTS.get(role, "")
