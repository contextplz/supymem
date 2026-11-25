"""Deterministic honesty flags for drafts (OT-17). Wraps ``tone.lint_draft``."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.content.safety.linter import BAIT_PHRASES, SLOP_PHRASES
from app.gtm.relationships.tone import lint_draft

_CALENDAR = re.compile(
    r"(calendly\.com|cal\.com|calendar\.google|book a time|book a call|15[- ]?min)",
    re.I,
)
_LOGISTICS = re.compile(r"\s*(?:please\s+)?(?:reply|respond|let me know)\b(?:\s+with)?\s+(?:a|the|your|some)?\s*(?:good\s+)?(?:time|times|slot|slots|availability)\b", re.I)
_GENERIC_COMPLIMENT = re.compile(
    r"\b(great work|nice post|inspiring leader|love your content|congrats on everything)\b",
    re.I,
)
_FAKE_ADVICE = re.compile(
    r"\b(you should try|my advice is|here'?s what i'?d do|just hire a consultant)\b",
    re.I,
)
_MERGE_FIELD = re.compile(r"\{\{[^{}\n]+\}\}|\[[\w -]+\](?!\()|<[\w -]+>")
# ponytail: English clause heuristics, semantic review for indirect or quoted asks.
_ASK = re.compile(
    r"\b(?:can|could|would|will|do|are)\s+(?:you|we)\b|"
    r"(?:^|[,:—]|\b(?:and|also|then)\s+)\s*(?:please\s+)?"
    r"(?:open to|interested in|let me know|reply|respond|book|schedule|send|share|try|visit|join|connect|"
    r"tell|click|download|register|review|fill out|check out|take a look|sign up)\b",
    re.I,
)

NON_OVERRIDABLE = frozenset({"premature_calendar", "pitch_in_connection_request"})


@dataclass(frozen=True)
class HonestyResult:
    blocked: bool
    flags: tuple[str, ...]
    overridable: bool


def check_draft(
    *,
    body: str,
    kind: str,
    ask_rung: str,
    product_name: str | None,
    hook: str | None,
) -> HonestyResult:
    flags: list[str] = []
    text = body or ""
    step = kind if kind not in {"connect", "question", "value_return"} else kind
    lint = lint_draft(text, hook=hook or "pasted evidence", step=step, product_name=product_name)
    if not lint.ok:
        flags.extend(f"lint:{r}" for r in lint.reasons)

    early = kind in {"connect", "question"} or ask_rung in {"none", "connect", "question"}
    if early and _CALENDAR.search(text):
        flags.append("premature_calendar")

    product = (product_name or "").strip()
    if product and kind == "connect" and re.search(re.escape(product), text, re.I):
        flags.append("pitch_in_connection_request")

    if _FAKE_ADVICE.search(text):
        flags.append("fake_advice")
    if _GENERIC_COMPLIMENT.search(text) and not (hook or "").strip():
        flags.append("generic_compliment")
    if _MERGE_FIELD.search(text):
        flags.append("merge_field")
    # A scheduling follow-up right after an ask is that ask's logistics, not a second request
    # ("Open to a call? Reply with a time." is one ask).
    asks, previous = 0, False
    for sentence, terminal in re.findall(r"([^.!?;\n]+)([.!?;\n]|$)", text):
        count = max(terminal == "?", len(_ASK.findall(sentence)))
        if previous and count == 1 and _LOGISTICS.match(sentence):
            count = 0
        asks += count
        previous = count > 0 or (previous and bool(_LOGISTICS.match(sentence)))
    if asks > 1:
        flags.append("multiple_asks")
    lowered = text.lower()
    for label, phrases in (("slop_phrase", SLOP_PHRASES), ("engagement_bait", BAIT_PHRASES)):
        hits = sorted({phrase for phrase in phrases if phrase in lowered})
        if hits:
            flags.append(label + ":" + ",".join(hits[:4]))

    unique = tuple(dict.fromkeys(flags))
    hard = tuple(f for f in unique if f in NON_OVERRIDABLE)
    blocked = bool(hard)
    overridable = bool(unique) and not blocked
    return HonestyResult(blocked=blocked, flags=unique, overridable=overridable)
