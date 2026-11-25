"""Deterministic agent packs from onboarding stage answers (plan §Agents)."""

from __future__ import annotations

from typing import Any


# Named employees for the cockpit — first name is what you @mention.
AGENT_CATALOG: dict[str, dict[str, str]] = {
    "researcher": {"id": "researcher", "name": "Maya", "role": "research", "title": "Researcher"},
    "founder_concierge": {
        "id": "founder_concierge",
        "name": "Samira",
        "role": "concierge",
        "title": "Concierge",
    },
    "designer": {"id": "designer", "name": "Lina", "role": "design", "title": "Designer"},
    "builder": {"id": "builder", "name": "Cole", "role": "build", "title": "Builder"},
    "waitlist_ops": {"id": "waitlist_ops", "name": "Bo", "role": "ops", "title": "Waitlist ops"},
    "reviewer": {"id": "reviewer", "name": "Jonah", "role": "review", "title": "Reviewer"},
    "coding_dispatcher": {
        "id": "coding_dispatcher",
        "name": "Ellis",
        "role": "engineering",
        "title": "Coding dispatcher",
    },
    "outreach": {"id": "outreach", "name": "Nia", "role": "sales", "title": "Outreach"},
    "support": {"id": "support", "name": "Eva", "role": "support", "title": "Support"},
}


def _agents(*keys: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in keys:
        if key in seen or key not in AGENT_CATALOG:
            continue
        seen.add(key)
        out.append(dict(AGENT_CATALOG[key]))
    return out


def pack_for_stage(stage: str, answers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map stage + button answers → agent roster and autonomy hint."""
    answers = answers or {}
    autonomy = "L1"
    agents: list[dict[str, str]]

    if stage in {"scratch", "idea", ""}:
        agents = _agents("researcher", "designer", "builder", "founder_concierge")
        return {
            "stage": stage or "scratch",
            "agents": agents,
            "autonomy_level": autonomy,
            "summary": "Maya researches. Lina plans the page. Cole builds it. Samira keeps you in the loop.",
        }

    if stage == "product":
        want = str(answers.get("want_first") or answers.get("goal") or "")
        where = str(answers.get("where") or answers.get("location") or "")
        keys = ["founder_concierge"]
        if want in {"landing", "landing_waitlist", "Landing + waitlist"}:
            keys.extend(["builder", "waitlist_ops"])
        elif want == "gtm":
            keys.extend(["researcher", "outreach", "waitlist_ops"])
        elif want in {"research", "Research/validate"}:
            keys.extend(["researcher", "builder"])
        elif want in {"agents", "Agents to run ops"}:
            keys.extend(["builder", "outreach", "support"])
        elif want in {"payments", "Connect payments"}:
            keys.extend(["builder", "waitlist_ops"])
        else:
            keys.extend(["builder", "researcher"])
        if where in {"github", "GitHub"}:
            keys.append("reviewer")
        agents = _agents(*keys)
        return {
            "stage": "product",
            "agents": agents,
            "autonomy_level": autonomy,
            "summary": "These agents are live for you.",
            "answers": answers,
        }

    if stage in {"company", "existing_company"}:
        company_type = str(answers.get("company_type") or "").lower()
        automate = answers.get("automate") or answers.get("automate_what") or []
        if isinstance(automate, str):
            automate = [automate]
        how_far = str(answers.get("how_far") or answers.get("autonomy") or "")
        keys = ["founder_concierge"]

        if company_type in {"ai-native", "ai_native", "AI-native company"}:
            if any(
                x in {str(a).lower() for a in automate}
                for x in {"engineering", "eng", "entire", "entire company", "entire_company"}
            ):
                keys.extend(["coding_dispatcher", "researcher"])
            if any(
                x in {str(a).lower() for a in automate}
                for x in {"sales", "support", "marketing"}
            ):
                keys.extend(["outreach", "support"])
            if any(
                x in {str(a).lower() for a in automate}
                for x in {"research", "entire", "entire company", "entire_company"}
            ):
                keys.append("researcher")
            if not automate:
                keys.extend(["researcher", "coding_dispatcher"])
            # Entire company → Assist (L2) with confirmations, never full autopilot in v1.
            if any(
                "entire" in str(a).lower() for a in automate
            ) or how_far.lower() in {"full autopilot", "full_autopilot", "autopilot"}:
                autonomy = "L2"
            elif how_far.lower() in {
                "act with approval",
                "act_with_approval",
                "draft + ask me",
                "draft_ask",
            }:
                autonomy = "L2"
        else:
            # Regular company
            pain = str(answers.get("pain") or answers.get("biggest_pain") or "").lower()
            if "lead" in pain:
                keys.extend(["outreach", "waitlist_ops"])
            elif "support" in pain:
                keys.append("support")
            elif "hir" in pain or "build" in pain:
                keys.extend(["builder", "researcher"])
            else:
                keys.extend(["outreach", "support", "researcher"])

        agents = _agents(*keys)
        return {
            "stage": "company",
            "agents": agents,
            "autonomy_level": autonomy,
            "summary": "These agents are live for you.",
            "answers": answers,
            "observe_mode": True,
        }

    # something else — map to nearest scratch pack
    agents = _agents("researcher", "designer", "builder", "founder_concierge")
    return {
        "stage": "other",
        "agents": agents,
        "autonomy_level": autonomy,
        "summary": "Mapped to research-first path.",
        "answers": answers,
    }


__all__ = ["AGENT_CATALOG", "pack_for_stage"]
