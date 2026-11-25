"""Turn a vague idea + problem into a tagged founder brief (gtm-cofounder start-here)."""

from __future__ import annotations

import re
from typing import Any

ASSUMPTION = "assumption"
VALIDATED = "validated"


def _tag(text: str, *, validated: bool = False) -> dict[str, str]:
    return {"text": text.strip(), "tag": VALIDATED if validated else ASSUMPTION}


def extract_brief(
    *,
    idea: str,
    problem: str = "",
    who: str = "",
    users_count: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Five core questions. Everything inferred from a one-liner is an assumption."""

    idea_s = re.sub(r"\s+", " ", (idea or "").strip())
    problem_s = re.sub(r"\s+", " ", (problem or "").strip()) or idea_s
    who_s = re.sub(r"\s+", " ", (who or "").strip())
    extra = extra or {}
    if not idea_s:
        raise ValueError("idea_required")

    product = idea_s.split(".")[0][:120]
    icp_guess = who_s or _guess_who(idea_s, problem_s)
    stage = "pre_users" if int(users_count or 0) <= 0 else "has_users"
    return {
        "product": _tag(product, validated=False),
        "problem": _tag(problem_s, validated=False),
        "who": _tag(icp_guess, validated=bool(who_s)),
        "why_now": _tag(
            str(extra.get("why_now") or "Founder is starting GTM from a vague idea."),
            validated=False,
        ),
        "stage": stage,
        "users_count": int(users_count or 0),
        "evidence_notes": str(extra.get("evidence_notes") or ""),
        "assumption_heavy": not who_s and int(users_count or 0) <= 0,
    }


def _guess_who(idea: str, problem: str) -> str:
    blob = f"{idea} {problem}".lower()
    if "developer" in blob or "engineer" in blob or "devtool" in blob:
        return "Engineering managers at product companies drowning in context switching"
    if "founder" in blob or "startup" in blob:
        return "Technical founders who still do their own outbound"
    return "The people who feel this problem every week"


def assumption_count(brief: dict[str, Any]) -> int:
    n = 0
    for key in ("product", "problem", "who", "why_now"):
        item = brief.get(key) or {}
        if isinstance(item, dict) and item.get("tag") == ASSUMPTION:
            n += 1
    return n
