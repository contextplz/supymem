"""Injection-attempt classifier + trust labelling."""

from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"ignore (all )?previous instructions",
        r"disregard (the )?system prompt",
        r"you are now",
        r"act as (a |an )?(admin|root|sudo)",
        r"reveal (your |the )?(system|hidden) prompt",
        r"jailbreak",
        r"dan mode",
        r"developer mode",
        r"override (your |the )?policy",
        r"exfiltrate",
        r"send (me )?(the )?api[_ ]?key",
        r"dump (the )?vault",
        r"disable (the )?kill[- ]?switch",
        r"raise autonomy",
        r"promote (this|me|yourself)",
        r"skip (peer )?review",
        r"write to policy\.baseline",
        r"rm -rf",
        r"<script>",
        r"BEGIN SYSTEM",
        r"ignore (all )?(prior|above) (instructions|prompts)",
        r"you are no longer",
        r"new (system|developer) prompt",
        r"from now on you (will|must|are)",
        r"role[- ]?swap",
        r"pretend (you are|to be) (the )?(system|developer)",
        r"<tool_call\b",
        r"</?untrusted\b",
        r"tool_calls\s*:\s*\[",
        r"spoof (a |the )?tool (call|result)",
        r"execute this tool call",
    ]
]


@dataclass
class Classification:
    trust: str  # untrusted|internal|founder
    injection_attempt: bool
    matched: str | None = None


def classify_text(text: str, *, source: str = "public") -> Classification:
    trust = "untrusted" if source in {"public", "scraped", "customer", "channel"} else "internal"
    for pat in INJECTION_PATTERNS:
        if pat.search(text or ""):
            return Classification(trust=trust, injection_attempt=True, matched=pat.pattern)
    return Classification(trust=trust, injection_attempt=False)
