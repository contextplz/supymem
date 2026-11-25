"""Prompt-injection deterministic pre-filter.

Classifiers (Llama Prompt Guard) load only when GUARDRAILS_MODE=full.
Local/CI default is deterministic_only — no 400 MB model download.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_PATTERNS: tuple[str, ...] = (
    r"ignore (all )?previous instructions",
    r"ignore (all )?(prior|above) (instructions|prompts)",
    r"disregard (the )?system prompt",
    r"you are now",
    r"you are no longer",
    r"act as (a |an )?(admin|root|sudo)",
    r"reveal (your |the )?(system|hidden) prompt",
    r"new (system|developer) prompt",
    r"from now on you (will|must|are)",
    r"role[- ]?swap",
    r"pretend (you are|to be) (the )?(system|developer)",
    r"jailbreak",
    r"dan mode",
    r"developer mode",
    r"override (your |the )?policy",
    r"<tool_call\b",
    r"tool_calls\s*:\s*\[",
    r"spoof (a |the )?tool (call|result)",
    r"execute this tool call",
    r"</?untrusted\b",
    r"BEGIN SYSTEM",
)

_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS)


@dataclass(frozen=True)
class InjectionHit:
    pattern: str
    excerpt: str


def detect_injection(text: str) -> list[InjectionHit]:
    raw = text or ""
    hits: list[InjectionHit] = []
    seen: set[str] = set()
    for rx in _COMPILED:
        m = rx.search(raw)
        if m is None or rx.pattern in seen:
            continue
        seen.add(rx.pattern)
        start = max(0, m.start() - 24)
        hits.append(InjectionHit(pattern=rx.pattern, excerpt=raw[start : start + 160]))
    return hits


def classify_injection_score(text: str) -> float | None:
    """Optional Prompt Guard score. None when classifiers are not loaded."""
    from guardrails.classifiers import score_injection

    return score_injection(text)
