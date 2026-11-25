"""Right-sizing a request into an errand, a task or a project (36 §4, LD-50).

The rubric is deterministic first. A model call only fills in wording, and every
sizing rule is re-asserted afterwards by :func:`clamp`, because a model that
sizes wrong is expensive in both directions: an over-sized errand buries a typo
fix under ceremony, and an under-sized project spends real money before anyone
agreed to it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any

SIZES: tuple[str, ...] = ("errand", "task", "project")
SIZE_RANK: dict[str, int] = {"errand": 0, "task": 1, "project": 2}

# Per-size budgets (35 §6).
COST_CAP_USD: dict[str, float] = {"errand": 0.05, "task": 0.15, "project": 0.60}

# A project may not spend past triage before the founder says go (36 §4).
TRIAGE_COST_CAP_USD = 0.02

NOTHING_LEFT_OUT = "nothing was left out"

TRIAGE_PURPOSE = "engagement.triage"
_MAX_TITLE = 60

# Categories that are at least a `task` and always reviewed, however small the
# diff. Cheap to state, and it prevents the worst category of accident (36 §4).
SENSITIVE_PATTERNS: dict[str, tuple[str, ...]] = {
    "auth": (
        r"\bauth\b",
        r"\bauthentication\b",
        r"\bauthorisation\b|\bauthorization\b",
        r"\blog[- ]?in\b",
        r"\bsign[- ]?in\b",
        r"\bpassword\b",
        r"\bsession\b",
        r"\boauth\b",
        r"\bpermissions?\b",
        r"\brole[s]?\b",
        r"\bsso\b",
    ),
    "payments": (
        r"\bpayments?\b",
        r"\bbilling\b",
        r"\bstripe\b",
        r"\bcheckout\b",
        r"\binvoices?\b",
        r"\brefunds?\b",
        r"\bsubscriptions?\b",
        r"\bpayouts?\b",
    ),
    "secrets": (
        r"\bsecrets?\b",
        r"\bapi key\b",
        r"\bcredentials?\b",
        r"\baccess key\b",
        r"\bprivate key\b",
        r"\.env\b",
        r"\bvault\b",
    ),
    "migrations": (
        r"\bmigrations?\b",
        r"\balembic\b",
        r"\bschema\b",
        r"\bnew (?:table|column)\b",
        r"\badd a (?:table|column)\b",
    ),
    "deletion": (
        r"\bdelete\b",
        r"\bdeletion\b",
        r"\bdrop (?:the |a )?(?:table|column|database)\b",
        r"\bpurge\b",
        r"\bwipe\b",
        r"\btruncate\b",
        r"\bremove all\b",
    ),
}

# Rule 3 from 36 §4: any one of these makes it a project.
PROJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\band also\b", "two separate outcomes in one request"),
    (r"\bmigrat(?:e|es|ing|ion)\b", "a migration"),
    (r"\bre-?write\b", "a rewrite"),
    (r"\bredesign\b", "a redesign"),
    (r"\b(?:mobile|ios|android) app\b", "a new product surface"),
    (r"\bmulti[- ]?tenan(?:t|cy)\b", "a new data boundary"),
    (r"\bnew (?:service|dependency|library|package|table|column)\b", "a new dependency"),
    (r"\breplace (?:our|the|my) \w+ with\b", "swapping a dependency"),
    (r"\bswitch (?:to|from|over to)\b", "swapping a dependency"),
    (r"\bschema\b", "a schema change"),
    (r"\btables?\b", "a schema change"),
    (r"\bacross (?:every|all|the whole)\b", "more than five files"),
    (r"\b(?:every|all) pages?\b", "more than five files"),
    (r"\bwhole (?:app|site|codebase|product)\b", "more than five files"),
    (r"\bintegrat(?:e|ion) with\b", "a new integration"),
    (r"\bfrom scratch\b", "greenfield work"),
    (r"\bend[- ]to[- ]end\b", "more than one acceptance criterion"),
)

# Bigger than a project is not an engagement at all (36 §2, 35 §5.1).
VENTURE_PATTERNS: tuple[str, ...] = (
    r"\bnew product\b",
    r"\bown product\b",
    r"\bgo[- ]to[- ]market\b",
    r"\bpricing (?:model|strategy|tiers?)\b",
    r"\bbusiness model\b",
    r"\bmoneti[sz]e\b",
    r"\b(?:start|launch|build me) a (?:company|business|startup)\b",
)

# One answer or one tiny diff (rule 1 from 36 §4).
QUESTION_STARTS: tuple[str, ...] = (
    "what",
    "why",
    "how",
    "does",
    "do",
    "did",
    "is",
    "are",
    "can",
    "could",
    "should",
    "which",
    "who",
    "when",
    "where",
    "explain",
    "tell me",
)

TINY_CHANGE_PATTERNS: tuple[str, ...] = (
    r"\bbump\b",
    r"\btypos?\b",
    r"\bspelling\b",
    r"\bcopyright\b",
    r"\brename the\b",
    r"\bwording\b",
    r"\blabel\b",
    r"\bplaceholder\b",
    r"\bbroken link\b",
    r"\bfix the link\b",
    r"\bwhitespace\b",
    r"\bcomment\b",
)

# Where the request is genuinely two-way, pick the safer reading and say so
# rather than spending the one clarification question on it (36 §4).
ASSUMPTION_RULES: tuple[tuple[str, str, str], ...] = (
    (
        r"\bemails?\b",
        r"\b(?:transactional|marketing|newsletter|campaign)\b",
        "Assuming the transactional email, not the marketing list.",
    ),
    (
        r"\busers?\b",
        r"\b(?:signed[- ]in|logged[- ]in|anonymous|guest|admin)\b",
        "Assuming signed-in users, not anonymous visitors.",
    ),
    (
        r"\bthe site\b|\bthe app\b",
        r"\b(?:staging|production|local|preview)\b",
        "Assuming production, not staging.",
    ),
)

HEDGE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bnot sure\b", "you are not sure where this lives"),
    (r"\bsomewhere\b", "the exact place is unknown"),
    (r"\bsomehow\b", "the mechanism is unknown"),
    (r"\bi think\b", "the cause is a guess so far"),
    (r"\bmaybe\b", "the cause is a guess so far"),
    (r"\bno idea\b", "the cause is unknown"),
    (r"\bcan.?t reproduce\b", "the fault is not reproducible yet"),
)

_TARGET_PATTERNS: tuple[str, ...] = (
    r"\bhttps?://\S+",
    r"\b[\w.-]+/[\w./-]+\.\w{1,5}\b",
    r"\b[\w-]+\.(?:py|ts|tsx|js|jsx|yaml|yml|json|toml|md|html|css|sql|env)\b",
    r"\bgithub:[\w.-]+/[\w.-]+\b",
)


@dataclass(frozen=True)
class Triage:
    """The sizing decision plus everything the phases downstream need."""

    size: str
    title: str
    definition_of_done: str
    not_doing: list[str] = field(default_factory=list)
    target_ref: str | None = None
    needs_clarification: str | None = None
    escalate_to_venture: bool = False
    unknowns: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    requires_review: bool = False
    sensitive: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # Set whenever the enforced size differs from the size we were handed, so a
    # resize is always something the founder is told about, never silent.
    resized_from: str | None = None
    cost_cap_usd: float = COST_CAP_USD["task"]
    source: str = "rules"
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "title": self.title,
            "definition_of_done": self.definition_of_done,
            "not_doing": list(self.not_doing),
            "target_ref": self.target_ref,
            "needs_clarification": self.needs_clarification,
            "escalate_to_venture": self.escalate_to_venture,
            "unknowns": list(self.unknowns),
            "assumptions": list(self.assumptions),
            "requires_review": self.requires_review,
            "sensitive": list(self.sensitive),
            "reasons": list(self.reasons),
            "resized_from": self.resized_from,
            "cost_cap_usd": self.cost_cap_usd,
            "source": self.source,
            "cost_usd": self.cost_usd,
        }


# ---------------------------------------------------------------------------
# deterministic rubric
# ---------------------------------------------------------------------------


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text) for p in patterns)


def sensitive_classes(text: str) -> list[str]:
    lowered = text.lower()
    return [
        name
        for name, patterns in SENSITIVE_PATTERNS.items()
        if _matches(lowered, patterns)
    ]


def project_reasons(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for pattern, reason in PROJECT_PATTERNS:
        if re.search(pattern, lowered) and reason not in found:
            found.append(reason)
    return found


def escalates_to_venture(text: str) -> bool:
    return _matches(text.lower(), VENTURE_PATTERNS)


def is_question(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return False
    if not any(stripped.startswith(w + " ") or stripped == w for w in QUESTION_STARTS):
        return False
    # "how do I add a wishlist button" asks; "how about you add one" instructs.
    return not re.search(r"\babout you\b", stripped)


def is_tiny_change(text: str) -> bool:
    return _matches(text.lower(), TINY_CHANGE_PATTERNS)


def size_from_rules(text: str) -> tuple[str, list[str]]:
    """The sizing rubric of 36 §4, in rule order. Rule 3 outranks rule 1."""
    reasons = project_reasons(text)
    if reasons:
        return "project", reasons
    sensitive = sensitive_classes(text)
    if sensitive:
        return "task", [f"touches {name}" for name in sensitive]
    if is_question(text):
        return "errand", ["one answer, no change"]
    if is_tiny_change(text):
        return "errand", ["one tiny change"]
    return "task", ["a single observable outcome"]


def find_target_ref(text: str) -> str | None:
    for pattern in _TARGET_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(0).rstrip(".,;:")
    return None


def find_unknowns(text: str) -> list[str]:
    lowered = text.lower()
    out: list[str] = []
    for pattern, unknown in HEDGE_PATTERNS:
        if re.search(pattern, lowered) and unknown not in out:
            out.append(unknown)
    return out


def find_assumptions(text: str) -> list[str]:
    lowered = text.lower()
    return [
        assumption
        for trigger, qualifier, assumption in ASSUMPTION_RULES
        if re.search(trigger, lowered) and not re.search(qualifier, lowered)
    ]


def derive_title(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", text.strip(), maxsplit=1)[0].strip()
    first = re.sub(r"\s+", " ", first).strip(" .!?,;:")
    if not first:
        first = "Untitled request"
    if len(first) > _MAX_TITLE:
        first = first[: _MAX_TITLE - 1].rstrip() + "…"
    return first[0].upper() + first[1:]


def derive_definition_of_done(text: str, size: str) -> str:
    subject = re.sub(r"\s+", " ", text.strip().rstrip(" .!?")) or "the request"
    if is_question(text):
        return f"You have a written answer to: {subject}."
    if size == "project":
        return f"Every milestone for '{subject}' is finished and shown to you."
    return f"'{subject}' is done, and you can see the evidence for it."


def default_not_doing(size: str, sensitive: list[str], *, escalate: bool) -> list[str]:
    """Never empty. Silent scope reduction is the fastest way to lose trust."""
    if size == "errand" and not sensitive and not escalate:
        return [NOTHING_LEFT_OUT]
    items = ["nothing outside what you asked for"]
    if sensitive:
        items.append(f"no {' or '.join(sorted(sensitive))} change without your review")
    if size == "project":
        items.append("no work starts before you say go")
    if escalate:
        items.append("no new product is created without you asking for one")
    return items


# ---------------------------------------------------------------------------
# hard rules, applied whatever the model said
# ---------------------------------------------------------------------------


def clamp(
    proposed: Triage,
    *,
    text: str,
    workspace_kind: str = "desk",
    observe_level: str = "O0",
) -> Triage:
    """Re-assert every sizing rule after the model call (36 §4).

    Only ever moves a size upward, and records what it moved from so the change
    can be narrated instead of happening quietly.
    """
    rule_size, rule_reasons = size_from_rules(text)
    sensitive = sensitive_classes(text)
    reasons = list(dict.fromkeys([*proposed.reasons, *rule_reasons]))

    size = proposed.size if proposed.size in SIZE_RANK else rule_size
    if SIZE_RANK[rule_size] > SIZE_RANK[size]:
        size = rule_size

    unknowns = list(proposed.unknowns) or find_unknowns(text)
    # An errand with unknowns is a guess.
    if unknowns and size == "errand":
        size = "task"
        reasons.append("an errand with unknowns is a guess")

    # A connected company we are only allowed to look at cannot be handed a
    # project: cap the ceremony and route the write through a founder review.
    capped_by_observe = False
    if (
        workspace_kind == "company"
        and observe_level in {"O0", "O1", "O2"}
        and size == "project"
    ):
        size = "task"
        capped_by_observe = True
        reasons.append(f"capped at a task while you are at {observe_level}")

    requires_review = (
        proposed.requires_review
        or bool(sensitive)
        or size in {"task", "project"}
        or capped_by_observe
    )

    not_doing = [n for n in proposed.not_doing if n and n.strip()]
    if not not_doing:
        not_doing = default_not_doing(
            size, sensitive, escalate=proposed.escalate_to_venture
        )
    if size != "errand" and not_doing == [NOTHING_LEFT_OUT]:
        not_doing = default_not_doing(
            size, sensitive, escalate=proposed.escalate_to_venture
        )

    # Errands ask nothing: if it needs a question it was never an errand.
    needs_clarification = proposed.needs_clarification or None
    if size == "errand":
        needs_clarification = None

    return replace(
        proposed,
        size=size,
        not_doing=not_doing,
        needs_clarification=needs_clarification,
        unknowns=unknowns,
        sensitive=sensitive,
        requires_review=requires_review,
        reasons=list(dict.fromkeys(reasons)),
        resized_from=(
            proposed.size if proposed.size in SIZE_RANK and proposed.size != size else None
        ),
        cost_cap_usd=COST_CAP_USD[size],
    )


# ---------------------------------------------------------------------------
# the one model call
# ---------------------------------------------------------------------------

_RUBRIC = """You size one founder request. Reply with JSON only.

Sizes, in order of ceremony:
1. errand, the answer is knowledge already available, or the change touches at
   most one file and ten lines and needs no new dependency.
2. task, one observable outcome and the path is known.
3. project, any of: more than five files, a new dependency or service, a schema
   change, more than one acceptance criterion, or "and also" joining two
   distinct outcomes.
Set escalate_to_venture when the request needs its own product surface,
pricing, or go-to-market.

Keys: size, title (<=60 chars, no trailing period), definition_of_done (one
observable sentence), not_doing (list of explicit exclusions), target_ref
(file, url or null), needs_clarification (one question or null),
escalate_to_venture (bool), unknowns (list of strings)."""


def _model_available() -> bool:
    offline = os.getenv("LLM_OFFLINE", "true").strip().lower() in {"1", "true", "yes"}
    return not offline and bool(os.getenv("LLM_API_KEY"))


def _parse_json(content: str) -> dict[str, Any]:
    body = content.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no object in model reply")
    parsed = json.loads(body[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model reply was not an object")
    return parsed


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


async def _refine_with_model(text: str, base: Triage) -> Triage:
    from llm_gateway import sole
    from llm_gateway.ledger import cost_usd
    from llm_gateway.routing import resolve_tier

    tier = resolve_tier(TRIAGE_PURPOSE)
    resp = await sole.env_chat_completion(
        [
            {"role": "system", "content": _RUBRIC},
            {"role": "user", "content": text},
        ],
        tier=tier,
        max_tokens=400,
    )
    content = resp.choices[0].message.content or ""
    data = _parse_json(content)

    usage = getattr(resp, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

    title = str(data.get("title") or base.title).strip()
    clarification = data.get("needs_clarification")
    return replace(
        base,
        size=str(data.get("size") or base.size).strip().lower(),
        title=derive_title(title),
        definition_of_done=str(
            data.get("definition_of_done") or base.definition_of_done
        ).strip(),
        not_doing=_str_list(data.get("not_doing")),
        target_ref=(str(data["target_ref"]).strip() or None)
        if data.get("target_ref")
        else base.target_ref,
        needs_clarification=str(clarification).strip() if clarification else None,
        escalate_to_venture=bool(data.get("escalate_to_venture"))
        or base.escalate_to_venture,
        unknowns=_str_list(data.get("unknowns")) or base.unknowns,
        source="model",
        cost_usd=cost_usd(tier, tokens_in, tokens_out),
    )


def triage_deterministic(text: str) -> Triage:
    """The rules-only reading. Costs nothing and always answers."""
    size, reasons = size_from_rules(text)
    sensitive = sensitive_classes(text)
    escalate = escalates_to_venture(text)
    return Triage(
        size=size,
        title=derive_title(text),
        definition_of_done=derive_definition_of_done(text, size),
        not_doing=default_not_doing(size, sensitive, escalate=escalate),
        target_ref=find_target_ref(text),
        needs_clarification=None,
        escalate_to_venture=escalate,
        unknowns=find_unknowns(text),
        assumptions=find_assumptions(text),
        requires_review=bool(sensitive) or size in {"task", "project"},
        sensitive=sensitive,
        reasons=reasons,
        cost_cap_usd=COST_CAP_USD[size],
        source="rules",
    )


async def triage(
    text: str,
    *,
    workspace_kind: str = "desk",
    observe_level: str = "O0",
) -> Triage:
    """Size one request. One model call at most; the rules decide on failure."""
    base = triage_deterministic(text)
    refined = base
    if _model_available():
        try:
            refined = await _refine_with_model(text, base)
        except Exception:  # noqa: BLE001 — wording is not worth failing a run for
            refined = base
    refined = replace(refined, assumptions=base.assumptions)
    return clamp(
        refined,
        text=text,
        workspace_kind=workspace_kind,
        observe_level=observe_level,
    )
