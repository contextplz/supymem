"""The seven deterministic filters, run before anything reaches a founder (37 §5).

Nothing here calls a model. A card the founder never sees costs nothing to
drop, and a generic card costs trust, so every rule below fails closed: when a
claim cannot be checked, the opportunity goes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.idealab.corpus import RESEARCHED_KINDS

MIN_EVIDENCE = 2
DUPLICATE_THRESHOLD = 0.8
NIGHTS_AND_WEEKENDS_MAX_DAYS = 45
ONE_LINER_MAX = 140

POSTURES = ("nights_and_weekends", "full_time", "funded")

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class Candidate:
    """An opportunity before it is persisted. Filters only ever read it."""

    title: str
    one_liner: str
    first_customer: str
    wedge: str
    why_now: str
    kill_criteria: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    shape_tags: list[str] = field(default_factory=list)
    effort_days: int = 30
    confidence: float = 0.5
    cluster_key: str = ""

    @property
    def key(self) -> str:
        return _normalize(f"{self.wedge} {self.first_customer}")

    @property
    def shape(self) -> frozenset[str]:
        return frozenset(t.strip().lower() for t in self.shape_tags if t.strip())

    @property
    def researched(self) -> bool:
        return bool(sourced_evidence(self.evidence))

    def text(self) -> str:
        return " ".join(
            [
                self.title,
                self.one_liner,
                self.first_customer,
                self.wedge,
                self.why_now,
                self.kill_criteria,
            ]
        )


@dataclass(frozen=True)
class Drop:
    """Why one candidate never reached the founder."""

    title: str
    rule: str
    detail: str


@dataclass(frozen=True)
class Outcome:
    kept: list[Candidate]
    dropped: list[Drop]

    def rules_hit(self) -> set[str]:
        return {d.rule for d in self.dropped}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


@lru_cache(maxsize=1)
def config() -> dict[str, Any]:
    import yaml

    path = _repo_root() / "config" / "idealab_banned.yaml"
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


@lru_cache(maxsize=1)
def _fabrication_res() -> tuple[re.Pattern[str], ...]:
    return tuple(
        re.compile(p, re.I) for p in config().get("fabrication_patterns") or ()
    )


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _has_marker(text: str, markers: Iterable[str]) -> str | None:
    low = f" {text.lower()} "
    for marker in markers:
        if marker.lower() in low:
            return marker
    return None


def sourced_evidence(evidence: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Evidence a founder could go and check: real kind, real source, a claim."""
    out: list[Mapping[str, Any]] = []
    for item in evidence or ():
        if not isinstance(item, Mapping):
            continue
        claim = str(item.get("claim") or "").strip()
        source = str(item.get("source") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if claim and source and kind in RESEARCHED_KINDS:
            out.append(item)
    return out


# --- the seven rules ------------------------------------------------------


def evidence_thin(cand: Candidate, *, corpus_fresh: bool) -> str | None:
    """1 — fewer than two sourced evidence items, while the cache is fresh."""
    if not corpus_fresh:
        return None
    found = len(sourced_evidence(cand.evidence))
    if found < MIN_EVIDENCE:
        return f"{found} sourced evidence items, needs {MIN_EVIDENCE}"
    return None


def matches_rejected_shape(
    cand: Candidate, rejected_shapes: Sequence[frozenset[str]]
) -> str | None:
    """2 — the founder already told us this shape is not for them."""
    shape = cand.shape
    if not shape:
        return None
    for rejected in rejected_shapes:
        if rejected and rejected <= shape:
            return "matches a shape you already turned down"
    return None


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def duplicates(cand: Candidate, others: Sequence[Candidate]) -> str | None:
    """3 — same wedge and first customer as something else in this slate."""
    for other in others:
        score = similarity(
            f"{cand.wedge} {cand.first_customer}",
            f"{other.wedge} {other.first_customer}",
        )
        if score >= DUPLICATE_THRESHOLD:
            return f"too close to “{other.title}” ({score:.2f})"
    return None


def banned_phrasing(cand: Candidate) -> str | None:
    """4 — confident phrasing that says nothing."""
    hit = _has_marker(cand.text(), config().get("banned_phrases") or ())
    return f"uses “{hit}”" if hit else None


def generic_customer(cand: Candidate) -> str | None:
    """5 — a market category is not a group anyone can go and find."""
    customer = cand.first_customer.strip()
    if not customer:
        return "no first customer named"
    normalized = _normalize(customer)
    if normalized in {
        _normalize(c) for c in config().get("category_only") or ()
    }:
        return f"“{customer}” is a category, not a group"
    qualifiers = config().get("customer_qualifiers") or {}
    for markers in qualifiers.values():
        if _has_marker(customer, markers or ()):
            return None
    return f"“{customer}” has no geography, size, role or behaviour attached"


def needs_licence_or_capital(cand: Candidate, posture: str) -> str | None:
    """6 — a licence, a certification, or money this founder has not got."""
    text = cand.text()
    licence = _has_marker(text, config().get("licence_markers") or ())
    if licence:
        return f"needs a licence or certification (“{licence}”)"
    if posture != "funded":
        capital = _has_marker(text, config().get("capital_markers") or ())
        if capital:
            return f"needs capital beyond your posture (“{capital}”)"
    return None


def effort_beyond_posture(cand: Candidate, posture: str) -> str | None:
    """7 — nights and weekends cannot absorb a 45-day-plus wedge."""
    if posture == "nights_and_weekends" and cand.effort_days > NIGHTS_AND_WEEKENDS_MAX_DAYS:
        return (
            f"{cand.effort_days} days to something sellable, "
            f"over {NIGHTS_AND_WEEKENDS_MAX_DAYS}"
        )
    return None


# --- anti-goals (§6), applied to generated copy ---------------------------


def fabricated_number(text: str) -> str | None:
    """A market size, a user count or borrowed credibility we cannot back."""
    for pattern in _fabrication_res():
        match = pattern.search(text or "")
        if match:
            return match.group(0).strip()
    return None


def copy_is_usable(title: str, one_liner: str) -> bool:
    if not title.strip() or not one_liner.strip():
        return False
    if len(one_liner) > ONE_LINER_MAX:
        return False
    joined = f"{title} {one_liner}"
    if _has_marker(joined, config().get("banned_phrases") or ()):
        return False
    return fabricated_number(joined) is None


# --- the pipeline ---------------------------------------------------------


def apply(
    candidates: Sequence[Candidate],
    *,
    posture: str = "nights_and_weekends",
    rejected_shapes: Sequence[frozenset[str]] = (),
    corpus_fresh: bool = True,
    limit: int | None = None,
) -> Outcome:
    """Run all seven in order. Order matters only for the duplicate rule."""
    kept: list[Candidate] = []
    dropped: list[Drop] = []
    for cand in candidates:
        number = fabricated_number(cand.text())
        checks = (
            ("evidence_thin", evidence_thin(cand, corpus_fresh=corpus_fresh)),
            ("rejected_shape", matches_rejected_shape(cand, rejected_shapes)),
            ("banned_phrasing", banned_phrasing(cand)),
            ("generic_customer", generic_customer(cand)),
            ("licence_or_capital", needs_licence_or_capital(cand, posture)),
            ("effort_beyond_posture", effort_beyond_posture(cand, posture)),
            (
                "fabricated_number",
                f"prints a number we cannot back: {number}" if number else None,
            ),
            ("duplicate_in_slate", duplicates(cand, kept)),
        )
        reason = next(((rule, why) for rule, why in checks if why), None)
        if reason is not None:
            dropped.append(Drop(title=cand.title, rule=reason[0], detail=reason[1]))
            continue
        kept.append(cand)
        if limit is not None and len(kept) >= limit:
            break
    return Outcome(kept=kept, dropped=dropped)
