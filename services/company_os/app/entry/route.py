"""Infer which of the four doors a founder is walking through (35 §5).

Cheapest and most certain first. Explicit clicks never call a model. Low
confidence asks before creating anything — a wrongly-created venture costs
trust and clutters the sidebar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

Mode = Literal["idea", "surprise", "engagement", "existing_company", "product"]
WorkspaceKind = Literal["venture", "company", "desk"]

MODES: tuple[Mode, ...] = (
    "idea",
    "surprise",
    "engagement",
    "existing_company",
    "product",
)

# Stage UI → entry mode aliases (plan: 4 stage buttons).
STAGE_TO_MODE: dict[str, Mode] = {
    "scratch": "idea",
    "idea": "idea",
    "product": "product",
    "company": "existing_company",
    "existing_company": "existing_company",
    "other": "idea",
}

MODE_META: dict[Mode, dict[str, str]] = {
    "idea": {
        "label": "Starting from scratch",
        "workspace_kind": "venture",
        "blurb": "I have an idea, nothing built yet.",
    },
    "surprise": {
        "label": "Surprise me",
        "workspace_kind": "desk",
        "blurb": "Three evidence-backed openings to compare.",
    },
    "engagement": {
        "label": "Something came up",
        "workspace_kind": "desk",
        "blurb": "Bounded work with a deliverable, no ceremony.",
    },
    "existing_company": {
        "label": "I already run a company",
        "workspace_kind": "company",
        "blurb": "Live ops; I want agents / automation.",
    },
    "product": {
        "label": "I already have a product",
        "workspace_kind": "venture",
        "blurb": "Code or MVP exists; I need company OS around it.",
    },
}

# Confidence below this shows a confirm chip and creates nothing (35 §5).
CONFIRM_THRESHOLD = 0.75

_SURPRISE_RE = re.compile(
    r"\b("
    r"surprise\s*me|surprise|no\s+idea|give\s+me\s+ideas?|"
    r"inspire\s+me|what\s+should\s+i\s+build|suggest\s+(?:an?\s+)?ideas?"
    r")\b",
    re.I,
)

_COMPANY_URL_RE = re.compile(
    r"git@[\w.-]+|"
    r"\bgithub\.com/[\w.-]+/[\w.-]+|"
    r"\bgitlab\.com/[\w.-]+|"
    r"\bbitbucket\.org/[\w.-]+",
    re.I,
)
_ANY_URL_RE = re.compile(r"https?://[\w.-]+", re.I)

_COMPANY_CUE_RE = re.compile(
    r"\b("
    r"connect\s+(?:my\s+|our\s+|the\s+)?(?:company|repo|github|org)|"
    r"existing\s+company|bring\s+your\s+own|"
    r"our\s+(?:repo|codebase|github)|"
    r"we\s+already\s+have\s+(?:a\s+)?(?:company|repo)"
    r")\b",
    re.I,
)

_EXISTING_PRODUCT_RE = re.compile(
    r"\b(?:i|we)\s+(?:already\s+)?(?:have|built|launched|run)\s+(?:an?\s+|our\s+|my\s+)?"
    r"(?:existing\s+)?(?:product|app|mvp|saas|website)\b|"
    r"\b(?:my|our)\s+(?:existing\s+)?(?:product|app|mvp|saas)\b",
    re.I,
)

_PERSONAL_INTENT_RE = re.compile(
    r"\b(?:for\s+(?:my|our)\s+own\s+use|just\s+for\s+me|only\s+for\s+myself|"
    r"(?:my|a)\s+(?:personal|hobby|side)\s+project|not\s+(?:a\s+)?(?:startup|business)|"
    r"no\s+(?:customers|business|commercial)\s+(?:plan|goal|intent)|"
    r"internal\s+(?:tool|dashboard|ops)|for\s+(?:my|our)\s+team|back[- ]office|"
    r"automat(?:e|ion)\s+(?:my|our))\b",
    re.I,
)


def infer_project_intent(text: str | None) -> Literal["startup", "personal"]:
    """Keep the startup path unless the founder clearly describes own-use work.

    This is an interpretation of their words, not a separate product mode. The
    persisted value keeps older own-use ventures on their shorter review path.
    """
    return "personal" if _PERSONAL_INTENT_RE.search(text or "") else "startup"


def wants_gtm_only(text: str | None) -> bool:
    body = text or ""
    if not _EXISTING_PRODUCT_RE.search(body):
        return False
    if re.search(r"\b(?:build|create|develop)\s+(?:a\s+|an\s+|the\s+|my\s+|our\s+)?(?:new\s+)?(?:app|product|mvp)\b", body, re.I):
        return False
    return bool(re.search(
        r"\b(?:gtm|go[- ]to[- ]market|marketing|customer\s+acquisition|"
        r"first\s+users|growth|find\s+(?:my\s+|our\s+|the\s+|some\s+)?(?:first\s+)?(?:users|customers)|"
        r"(?:get|need|want)\s+(?:my\s+|our\s+|the\s+|some\s+)?(?:first\s+)?(?:users|customers))\b",
        body, re.I,
    ))

# Fix / change verbs that mean "do this to something that exists".
_ENGAGEMENT_VERB_RE = re.compile(
    r"\b("
    r"fix|broken|bug|crash(?:es|ing)?|error|typo|"
    r"update|change|add|remove|bump|rewrite|refactor|"
    r"migrate|deploy|ship|debug|investigate|"
    r"write\s+(?:me\s+)?a|teardown|tear\s+down|"
    r"what\s+does\s+this|explain\s+this|help\s+(?:me\s+)?with"
    r")\b",
    re.I,
)

_ENGAGEMENT_SHAPE_RE = re.compile(
    r"\b("
    r"is\s+broken|goes?\s+to\s+spam|on\s+safari|by\s+friday|"
    r"competitor\s+teardown|signup\s+email"
    r")\b",
    re.I,
)


@dataclass(frozen=True)
class EntryDecision:
    mode: Mode
    confidence: float
    confirm_needed: bool
    workspace_kind: WorkspaceKind
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "confidence": self.confidence,
            "confirm_needed": self.confirm_needed,
            "confirm_required": self.confirm_needed,  # doc 35 alias
            "workspace_kind": self.workspace_kind,
            "reason": self.reason,
        }


def _kind_for(mode: Mode) -> WorkspaceKind:
    return MODE_META[mode]["workspace_kind"]  # type: ignore[return-value]


def _decide(
    mode: Mode, confidence: float, reason: str, *, force_confirm: bool = False
) -> EntryDecision:
    confirm = force_confirm or confidence < CONFIRM_THRESHOLD
    return EntryDecision(
        mode=mode,
        confidence=round(confidence, 3),
        confirm_needed=confirm,
        workspace_kind=_kind_for(mode),
        reason=reason,
    )


def route(
    text: str | None = None,
    *,
    explicit_mode: str | None = None,
    active_workspace_kind: str | None = None,
) -> EntryDecision:
    """Route a front-door request. Explicit modes cost zero LLM calls."""
    if explicit_mode:
        raw = explicit_mode.strip().lower()
        mode = STAGE_TO_MODE.get(raw, raw if raw in MODES else "")
        if mode not in MODES:
            return _decide(
                "idea",
                0.4,
                "I didn't recognise that mode, treating this as a new idea.",
                force_confirm=True,
            )
        meta = MODE_META[mode]  # type: ignore[index]
        return _decide(
            mode,  # type: ignore[arg-type]
            1.0,
            f"You chose {meta['label']}.",
        )

    body = (text or "").strip()
    if not body:
        # Empty composer + no click is Surprise Me's natural home (35 §5.2).
        return _decide(
            "surprise",
            0.85,
            "Nothing typed, I'll put three openings on the bench.",
        )

    if _SURPRISE_RE.search(body):
        return _decide(
            "surprise",
            0.95,
            "Sounds like you want openings to compare, not a company yet.",
        )

    if _EXISTING_PRODUCT_RE.search(body):
        return _decide(
            "product", 0.92,
            "You described a product that already exists; we'll work from that state.",
        )

    if _COMPANY_URL_RE.search(body) or _COMPANY_CUE_RE.search(body):
        return _decide(
            "existing_company",
            0.92,
            "That looks like a repo or company you already have.",
        )

    if _ANY_URL_RE.search(body):
        return _decide(
            "product", 0.9,
            "You shared a link; I'll read it and work from that product.",
        )

    engagement_hit = bool(
        _ENGAGEMENT_VERB_RE.search(body) or _ENGAGEMENT_SHAPE_RE.search(body)
    )
    if engagement_hit:
        # Inside an active venture/company, a fix-shaped request is an engagement
        # with high confidence — never a new venture (35 §5, test 6).
        if active_workspace_kind in {"venture", "company", "desk"}:
            return _decide(
                "engagement",
                0.93,
                "Reading this as a fix or bounded ask inside what you have.",
            )
        # No active workspace: still prefer engagement over inventing a venture,
        # but ask when the text could also be a product idea.
        confidence = 0.82 if _ENGAGEMENT_SHAPE_RE.search(body) else 0.7
        return _decide(
            "engagement",
            confidence,
            "Looks like bounded work rather than a new company.",
        )

    # Short product-shaped sentences are ideas with high confidence.
    if len(body) >= 12 and not engagement_hit:
        return _decide(
            "idea",
            0.88,
            "Reading this as a product idea to build from scratch.",
        )

    return _decide(
        "idea",
        0.55,
        "Not sure yet, I'll treat this as an idea unless you say otherwise.",
    )


def list_modes() -> list[dict[str, str]]:
    return [
        {
            "mode": mode,
            "label": meta["label"],
            "workspace_kind": meta["workspace_kind"],
            "blurb": meta["blurb"],
        }
        for mode, meta in MODE_META.items()
    ]
