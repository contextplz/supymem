"""Content linter for first-look copy (45 §6, LD-60).

Deterministic. Hard-fails fabricated social proof, metrics, testimonials, and
vacuous AI vocabulary. Never trusts a prompt instruction to stay honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

HEADLINE_MAX = 90
SUBHEAD_MAX = 140
POINT_TITLE_MAX = 36
POINT_BODY_MAX = 180
CTA_LABEL_MAX = 22
POINTS_REQUIRED = 3


@dataclass(frozen=True)
class LintFailure:
    rule: str
    detail: str


@dataclass
class LintResult:
    ok: bool
    failures: list[LintFailure] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{f.rule}: {f.detail}" for f in self.failures]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


@lru_cache(maxsize=1)
def _lint_cfg() -> dict[str, Any]:
    path = _repo_root() / "config" / "firstlook" / "lint.yaml"
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


@lru_cache(maxsize=1)
def _idealab_banned() -> dict[str, Any]:
    path = _repo_root() / "config" / "idealab_banned.yaml"
    if not path.exists():
        return {}
    with path.open() as handle:
        return yaml.safe_load(handle) or {}


def _phrases(*groups: list[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    for group in groups:
        for item in group or ():
            text = str(item).strip().lower()
            if text:
                out.append(text)
    # Longer first so "award-winning" wins over "award".
    return tuple(sorted(set(out), key=len, reverse=True))


@lru_cache(maxsize=1)
def _vocab() -> dict[str, tuple[str, ...]]:
    cfg = _lint_cfg()
    banned = _idealab_banned()
    fab = tuple(str(p) for p in (banned.get("fabrication_patterns") or ()))
    return {
        "social_proof": _phrases(cfg.get("social_proof")),
        "superlatives": _phrases(cfg.get("superlatives")),
        "banned_vocabulary": _phrases(
            cfg.get("banned_vocabulary"), banned.get("banned_phrases")
        ),
        "commitments": _phrases(cfg.get("commitments")),
        "third_parties": _phrases(cfg.get("third_parties")),
        "attribution_roles": _phrases(cfg.get("attribution_roles")),
        "metric_nouns": _phrases(cfg.get("metric_nouns")),
        "english_markers": _phrases(cfg.get("english_markers")),
        "fabrication_patterns": fab,
    }


_WORD_RE = re.compile(r"[a-z0-9']+", re.I)
_NUMBER_RE = re.compile(
    r"(?:"
    r"\$\s?\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:%|percent|k|m|bn|million|billion|trillion)?\b"
    r")",
    re.I,
)
_QUOTE_RE = re.compile(r"[\"“”]([^\"“”]{8,160})[\"“”]")
_NAME_ROLE_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s*,?\s+(?:CEO|CTO|COO|CFO|Founder|"
    r"Co-?Founder|Director|Manager|Customer|User|Client)\b"
)


def _flatten(copy: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("headline", "subhead", "audience"):
        val = copy.get(key)
        if val:
            parts.append(str(val))
    for point in copy.get("points") or []:
        if isinstance(point, dict):
            parts.append(str(point.get("title") or ""))
            parts.append(str(point.get("body") or ""))
        else:
            parts.append(str(point))
    for step in copy.get("how_it_works") or []:
        if isinstance(step, dict):
            parts.append(str(step.get("text") or ""))
        else:
            parts.append(str(step))
    cta = copy.get("cta")
    if isinstance(cta, dict):
        parts.append(str(cta.get("label") or ""))
    elif cta:
        parts.append(str(cta))
    for item in copy.get("assumptions") or []:
        parts.append(str(item))
    for name in copy.get("name_candidates") or []:
        parts.append(str(name))
    return "\n".join(parts)


def _contains_phrase(text: str, phrases: tuple[str, ...]) -> str | None:
    low = f" {text.lower()} "
    for phrase in phrases:
        needle = f" {phrase} "
        # Also allow start/end without requiring spaces around hyphenated forms.
        if needle in low or phrase in text.lower():
            # Prefer word-boundary-ish for short tokens like "best".
            if len(phrase) <= 5:
                if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text, re.I):
                    return phrase
            else:
                if phrase in text.lower():
                    return phrase
    return None


# Concept-page honesty (beta PR-1): a first-look page describes an idea, not shipped software.
# A sentence that states what the product does today ("Acme tracks your invoices", "you can book in
# seconds") is flagged unless it is hedged as plan/intent or matches the allowlist.
_CAPABILITY_RE = re.compile(
    r"\b(?P<subject>(?:(?i:we|our\s+\w+|it|this\s+(?:product|app|tool|platform|service)|you\s+can)|[A-Z][\w-]+))\s+"
    r"(?i:(?P<adverb>\w+ly\s+)?(?:can\s+)?"
    r"(?P<verb>helps?|lets?|gives?|automates?|tracks?|sends?|finds?|matches?|connects?|handles?|manages?|generates?|saves?|"
    r"reminds?|delivers?|provides?|offers?|turns?|makes?|creates?|detects?|syncs?|schedules?|books?|monitors?|"
    r"analy[sz]es?|recommends?|notif(?:y|ies)|integrates?|reaches|book|use|try|get|start|sign\s+up|download|log\s*in)\b)")
_CAPABILITY_HEDGE_RE = re.compile(
    r"\b(?:will|would|could|might|planning|plan(?:ned)?|building|exploring|working on|want(?:s)? to|hope[sd]? to|aim[s]? to|"
    r"soon|idea|concept|early|waitlist|if|once|not (?:built|ready|live)|yet|imagine|vision|goal|try(?:ing)? to|built to|designed to)\b", re.I)
CAPABILITY_ALLOWLIST = (
    "join the waitlist", "we're building", "we are building", "we're exploring", "we are exploring",
    "we want to", "we plan to", "not built yet", "nothing here is final",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def capability_claims(text: str) -> list[str]:
    """Sentences that read as present-tense claims about software that may not exist. Deterministic heuristic."""
    hits: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        low = sentence.lower()
        if not sentence.strip() or any(a in low for a in CAPABILITY_ALLOWLIST) or _CAPABILITY_HEDGE_RE.search(sentence):
            continue
        if _CAPABILITY_RE.search(sentence):
            hits.append(sentence.strip())
    return hits


def capability_offer_rewrite(sentence: str, product_name: str) -> str:
    """Rephrase a product action as intent, never rewrite evidence or integration claims."""
    def intent_verb(verb):
        if verb.lower().endswith("ies"):
            return verb[:-3] + "y"
        if verb.lower() in {"matches", "reaches"}:
            return verb[:-2]
        return verb[:-1] if verb.lower().endswith("s") else verb

    def coordinated_verb(match):
        verb = match.group(2)
        if not _CAPABILITY_RE.fullmatch("It " + verb):
            return match.group()
        return match.group(1) + intent_verb(verb)

    if re.search(r"\b(?:customers?|certif\w*|complian\w*|accredited|partners?|partnerships?|integrat\w*|guarantee\w*)\b",
                 sentence, re.I):
        return sentence
    for match in _CAPABILITY_RE.finditer(sentence):
        subject = match.group("subject")
        if subject.casefold() == "we":
            prefix = "We're building " + product_name + " to "
        elif subject.casefold() == "you can":
            prefix = "Join the waitlist to "
        elif (subject.casefold() == product_name.casefold() or subject.casefold() == "it"
              or subject.casefold().startswith(("our ", "this "))):
            prefix = subject + " is built to "
        else:
            continue
        tail = re.sub(r"\b((?:and|or)\s+(?:\w+ly\s+)?)(\w+)\b", coordinated_verb, sentence[match.end():], flags=re.I)
        return sentence[:match.start()] + prefix + (match.group("adverb") or "") + intent_verb(match.group("verb")) + tail
    return sentence


def _idea_is_english(idea_text: str) -> bool:
    markers = _vocab()["english_markers"]
    words = {w.lower() for w in _WORD_RE.findall(idea_text)}
    hits = sum(1 for m in markers if m in words)
    return hits >= 2 or len(words) < 4


def _copy_is_english(text: str) -> bool:
    markers = _vocab()["english_markers"]
    words = {w.lower() for w in _WORD_RE.findall(text)}
    if not words:
        return True
    hits = sum(1 for m in markers if m in words)
    return hits >= 2


def lint_copy(copy: dict[str, Any], *, idea_text: str = "") -> LintResult:
    """Return hard failures. Empty failures ⇒ ok to render."""
    failures: list[LintFailure] = []
    vocab = _vocab()
    blob = _flatten(copy)

    # --- schema ---
    headline = str(copy.get("headline") or "").strip()
    if not headline:
        failures.append(LintFailure("schema", "missing headline"))
    elif len(headline) > HEADLINE_MAX:
        failures.append(LintFailure("schema", f"headline > {HEADLINE_MAX} chars"))

    subhead = str(copy.get("subhead") or "").strip()
    if subhead and len(subhead) > SUBHEAD_MAX:
        failures.append(LintFailure("schema", f"subhead > {SUBHEAD_MAX} chars"))

    points = copy.get("points")
    if not isinstance(points, list) or len(points) != POINTS_REQUIRED:
        failures.append(LintFailure("schema", f"points must be exactly {POINTS_REQUIRED}"))
    else:
        for i, point in enumerate(points):
            if not isinstance(point, dict):
                failures.append(LintFailure("schema", f"point {i} not an object"))
                continue
            title = str(point.get("title") or "")
            body = str(point.get("body") or "")
            if len(title) > POINT_TITLE_MAX:
                failures.append(LintFailure("schema", f"point {i} title too long"))
            if len(body) > POINT_BODY_MAX:
                failures.append(LintFailure("schema", f"point {i} body too long"))

    assumptions = copy.get("assumptions")
    if not isinstance(assumptions, list) or not assumptions:
        failures.append(LintFailure("schema", "missing assumptions"))

    cta = copy.get("cta")
    if isinstance(cta, dict):
        label = str(cta.get("label") or "")
        if not label:
            failures.append(LintFailure("schema", "missing cta.label"))
        elif len(label) > CTA_LABEL_MAX:
            failures.append(LintFailure("schema", f"cta.label > {CTA_LABEL_MAX} chars"))
    elif not cta:
        failures.append(LintFailure("schema", "missing cta"))

    # --- social proof / superlatives / banned / commitments ---
    hit = _contains_phrase(blob, vocab["social_proof"])
    if hit:
        failures.append(LintFailure("social_proof", hit))

    hit = _contains_phrase(blob, vocab["superlatives"])
    if hit:
        failures.append(LintFailure("superlatives", hit))

    hit = _contains_phrase(blob, vocab["banned_vocabulary"])
    if hit:
        failures.append(LintFailure("banned_vocabulary", hit))

    hit = _contains_phrase(blob, vocab["commitments"])
    if hit:
        failures.append(LintFailure("commitments", hit))

    claims = capability_claims(blob)
    failures.extend(LintFailure("capability_claim", claim) for claim in claims)

    # --- numeric claims about traction / market ---
    idea_low = idea_text.lower()
    for match in _NUMBER_RE.finditer(blob):
        window_start = max(0, match.start() - 40)
        window_end = min(len(blob), match.end() + 40)
        window = blob[window_start:window_end].lower()
        if any(noun in window for noun in vocab["metric_nouns"]):
            claim = match.group(0)
            # Allow numbers that the founder themselves wrote.
            if claim.lower() not in idea_low and not any(
                claim.lower() in tok for tok in idea_low.split()
            ):
                failures.append(LintFailure("numeric_claim", claim))
                break

    for pattern in vocab["fabrication_patterns"]:
        if re.search(pattern, blob, re.I) and not re.search(pattern, idea_text, re.I):
            failures.append(LintFailure("numeric_claim", pattern))
            break

    # --- testimonials / attributed quotes ---
    if _QUOTE_RE.search(blob) and _contains_phrase(blob, vocab["attribution_roles"]):
        failures.append(LintFailure("testimonial", "quoted attribution"))
    if _NAME_ROLE_RE.search(blob):
        failures.append(LintFailure("testimonial", "named person with role"))

    # --- third-party integrations not in the idea ---
    for party in vocab["third_parties"]:
        if party in blob.lower() and party not in idea_low:
            failures.append(LintFailure("third_party", party))
            break

    # --- language drift ---
    if idea_text and _idea_is_english(idea_text) and not _copy_is_english(blob):
        failures.append(LintFailure("language", "non-English copy for English idea"))

    return LintResult(ok=not failures, failures=failures)


__all__ = [
    "LintFailure",
    "LintResult",
    "lint_copy",
    "capability_claims",
    "HEADLINE_MAX",
    "POINTS_REQUIRED",
]
