"""Canonical fact keys and bounded founder-only quotation handling."""
from __future__ import annotations

import re


def enabled(flag: str) -> bool:
    from app.credits.policy import get_policy
    try:
        flags = get_policy("feature_flags")
        return isinstance(flags, dict) and flags.get(flag) is True
    except Exception:
        return False


def facts_enabled() -> bool:
    from app.config import settings
    return settings.company_os_memory_facts_enabled or enabled("memory_capture_v1") or enabled("memory_conflicts_v1")


_ALIASES = {
    "idea.icp": "idea.audience", "idea.customers": "idea.audience",
    "idea.users": "idea.audience", "idea.target_users": "idea.audience",
    "idea.target_audience": "idea.audience", "idea.customer": "idea.audience",
    "idea.price": "idea.pricing", "idea.pricing_hypothesis": "idea.pricing",
    "idea.business_model": "idea.pricing", "idea.product": "idea.solution",
    "idea.offer": "idea.solution", "idea.making": "idea.solution",
    "preference.response_length": "preference.answer_length",
    "metric.mrr": "metric.revenue", "metric.users": "metric.customers",
}


def subject_key(subject: str) -> str:
    return _ALIASES.get(subject, subject)


def subject_aliases(subject: str) -> list[str]:
    subject = subject_key(subject)
    return [subject, *(alias for alias, canonical in _ALIASES.items() if canonical == subject)]


def founder_text(text: str) -> str:
    lines, fenced, quoted = [], False, False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if re.match(r"\s*(?:website|web page|document|email|mail|quoted text)\s*:", line, re.I):
            quoted = True
        if not fenced and not quoted and not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines)


def value_key(value: str) -> str:
    value = re.sub(r"^(?:actually[, ]+|correction[:, ]+)", "", value.strip(), flags=re.I)
    value = re.sub(r"^(?:(?:our|my|the)\s+(?:target\s+)?(?:audience|customers?|users?|icp|price|pricing|problem|product|solution)\s+(?:is|are|will be|has changed to|have changed to)\s+|we\s+(?:now\s+)?(?:target|charge)\s+)", "", value, flags=re.I)
    return " ".join(value.casefold().strip().rstrip(".! ").split())


def retirement(value: str) -> bool:
    # shortcut: withdrawal matching is English-only; extend when localized extraction is enabled.
    return bool(re.match(r"(?:actually[, ]+|correction[:, ]+)?(?:we\s+(?:no longer|do not|don't)\s+(?:target|charge)\b|(?:our|my|the)\s+(?:audience|customers?|users?|icp|pricing|price|problem|solution|product)\s+(?:is|are)\s+(?:no longer|not)\b|(?:forget|retire)\s+(?:my|our|the)\s+(?:audience|pricing|price|problem|solution|product)\b)", value.strip(), re.I))
