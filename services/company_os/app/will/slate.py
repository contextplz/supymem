"""Deterministic, evidence-backed Slate generation (doc 79 §3)."""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from app.state import MEM, db
from app.will.model import Slate, SlateItem
from app.will.validate import SlateValidationError, validate_slate
from core.ids import new_id

_METRICS = (
    "landing_conversion_rate",
    "qualified_signups_per_usd",
    "engagement_rate",
    "first_response_minutes",
    "arpu",
    "sourced_claim_ratio",
    "payment_success_rate",
)
_ACTIONS = (
    ("Interview five {focus} prospects", "qualified_signups_per_usd", "probe"),
    ("Publish a {focus} problem landing page", "landing_conversion_rate", "phase"),
    ("Run a {focus} demand probe", "engagement_rate", "probe"),
    ("Map the {focus} customer journey", "first_response_minutes", "phase"),
    ("Test a paid {focus} offer", "arpu", "probe"),
    ("Collect sourced {focus} competitor claims", "sourced_claim_ratio", "phase"),
    ("Instrument the {focus} activation funnel", "payment_success_rate", "phase"),
    ("Draft the {focus} founder outreach", "qualified_signups_per_usd", "phase"),
    ("Recruit {focus} design partners", "engagement_rate", "probe"),
    ("Prototype the {focus} core workflow", "landing_conversion_rate", "phase"),
    ("Validate {focus} pricing assumptions", "arpu", "probe"),
    ("Review {focus} onboarding friction", "first_response_minutes", "phase"),
    ("Write the {focus} value proposition", "sourced_claim_ratio", "phase"),
    ("Launch a {focus} waitlist test", "qualified_signups_per_usd", "probe"),
    ("Audit {focus} conversion evidence", "landing_conversion_rate", "insight"),
    ("Compare {focus} alternatives", "engagement_rate", "insight"),
    ("Set a {focus} success threshold", "payment_success_rate", "phase"),
    ("Run a {focus} concierge test", "first_response_minutes", "probe"),
    ("Define the {focus} retention loop", "arpu", "phase"),
    ("Capture {focus} objection patterns", "sourced_claim_ratio", "insight"),
)


_FOCUS_SKIP = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "that",
    "the",
    "this",
    "to",
    "who",
    "whose",
    "with",
    "build",
    "create",
    "develop",
    "launch",
    "make",
}


def _focus_from_idea(idea: str | None, venture_id: str) -> str:
    """Short noun phrase for task titles.

    Templates already supply their own articles ("Run a {focus} probe"), so a
    focus that keeps the idea's leading "a" renders as "Run a a brand kit…".
    Three words also keeps titles under the 60-char clip.
    """
    source = (idea or "").strip()
    named_product = re.match(
        r"^(?:build|create|develop|launch|make)\s+([A-Za-z0-9][A-Za-z0-9'-]{1,39})\s*[,.:]",
        source,
        re.IGNORECASE,
    )
    if named_product:
        return named_product.group(1)
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", source)
    cleaned = [word.lower() for word in words if word.lower() not in _FOCUS_SKIP]
    return " ".join(cleaned[:3]) or f"venture {venture_id[-6:]}"


def _clip_title(text: str, limit: int = 60) -> str:
    """Clip on a word boundary so a title never ends mid-word."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1].rsplit(" ", 1)[0] or text[: limit - 1]
    return f"{head.rstrip(' ,;:-')}…"


def _venture_brief(venture_id: str) -> str:
    for key in (f"brief:{venture_id}", f"venture:{venture_id}", f"idea:{venture_id}"):
        value = MEM.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            for field in ("raw_idea", "idea", "one_liner", "brief", "description"):
                if value.get(field):
                    return str(value[field])
    try:
        from app.db.models import Venture

        with db()() as session:
            venture = session.get(Venture, venture_id)
            if venture:
                return venture.raw_idea
    except (SQLAlchemyError, OSError):
        pass
    return ""


def _playbook_actions() -> tuple[tuple[str, str, str], ...]:
    """Load reviewable phase/probe templates; static entries keep MVP dependency-free."""
    path = Path(__file__).resolve().parents[4] / "config" / "will_playbook.yaml"
    try:
        import yaml  # type: ignore[import-not-found]

        templates = (yaml.safe_load(path.read_text()) or {}).get("templates") or []
        actions = tuple(
            (str(item["title"]), str(item["metric"]), str(item["source"]))
            for item in templates
            if {"title", "metric", "source"} <= item.keys()
        )
        if len(actions) >= 5:
            return actions
    except (ImportError, OSError, TypeError, ValueError):
        pass
    return _ACTIONS


def gather_candidates(venture_id: str, *, idea: str | None = None) -> list[SlateItem]:
    """Create candidates from phase/probe templates with venture-specific wording."""
    focus = _focus_from_idea(idea or _venture_brief(venture_id), venture_id)
    evidence = f"phase:{venture_id}:discovery"
    candidates: list[SlateItem] = []
    for index, (template, metric, origin) in enumerate(_playbook_actions()):
        candidates.append(
            SlateItem(
                rank=1,
                title=_clip_title(template.format(focus=focus)),
                why_now=f"No falsifiable read on {metric.replace('_', ' ')} for {focus} yet.",
                evidence=[evidence],
                expected_outcome=(
                    f"A measurable move in {metric.replace('_', ' ')} "
                    f"would support the {focus} hypothesis."
                ),
                expected_metric=metric,
                expected_delta=0.1 + index / 100,
                capability_id="venture.discovery",
                effort="hours",
                cost_usd_estimate=Decimal("0"),
                autonomy="auto",
                origin=origin,  # type: ignore[arg-type]
                phase=1,
                kill_criteria="Stop if the test receives no qualified response.",
            )
        )
    return candidates


def _rank(candidates: list[SlateItem], venture_id: str) -> list[SlateItem]:
    """Stable score: distinct metrics first, then a venture-seeded deterministic order."""
    seed = int(hashlib.sha256(venture_id.encode()).hexdigest()[:8], 16)
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.origin != "probe",
            (seed + sum(map(ord, item.title))) % 997,
        ),
    )
    selected: list[SlateItem] = []
    metrics: set[str] = set()
    for item in ordered:
        if item.expected_metric not in metrics:
            selected.append(item)
            metrics.add(item.expected_metric)
        if len(selected) == 5:
            break
    return [item.model_copy(update={"rank": rank}) for rank, item in enumerate(selected, 1)]


def deterministic_fallback(venture_id: str, because: str, *, idea: str | None = None) -> Slate:
    """Return five phase-derived items when richer candidate selection fails."""
    candidates = gather_candidates(venture_id, idea=idea)
    items = _rank(candidates, venture_id)
    return Slate(
        id=new_id("slt"),
        venture_id=venture_id,
        items=items,
        generated_because=because,
    )


def generate_slate(venture_id: str, *, because: str, idea: str | None = None) -> Slate:
    """Generate, validate, and retain the current Slate without an LLM dependency."""
    previous = MEM.get(f"slate_latest:{venture_id}") or {}
    try:
        slate = Slate(
            id=new_id("slt"),
            venture_id=venture_id,
            items=_rank(gather_candidates(venture_id, idea=idea), venture_id),
            generated_because=because,
            supersedes=previous.get("id") if isinstance(previous, dict) else None,
        )
        validate_slate(slate)
    except (SlateValidationError, ValueError):
        slate = deterministic_fallback(venture_id, because, idea=idea)
        validate_slate(slate)
    payload = slate.model_dump(mode="json")
    MEM[f"slate:{slate.id}"] = payload
    MEM[f"slate_latest:{venture_id}"] = payload
    return slate


def latest_slate(venture_id: str) -> Slate | None:
    """Read the current slate, if generation has happened."""
    payload = MEM.get(f"slate_latest:{venture_id}")
    return Slate.model_validate(payload) if payload else None
