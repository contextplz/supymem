"""Prompt builders that apply gstack algorithms without replacing Company OS control flow."""

from __future__ import annotations

from app.gstack.methodology import (
    AUTOPLAN_PRINCIPLES,
    CEO_PRIME_DIRECTIVES,
    DESIGN_SLOP,
    INVESTIGATE_IRON_LAW,
    QA_LOOP,
    default_ceo_mode,
    specialist_for_role,
)

_WRAP_SENTINEL = "GSTACK METHODOLOGY"


def wrap_coding_prompt(prompt: str, *, kind: str = "code") -> str:
    """Prefix a work-order prompt with investigate / review / QA / CSO rules.

    Idempotent: will not double-wrap. Does not change the founder's task text.
    """
    text = (prompt or "").strip()
    if not text or _WRAP_SENTINEL in text:
        return prompt
    rules = [
        f"{_WRAP_SENTINEL} (garrytan/gstack, MIT)",
        f"Iron Law: {INVESTIGATE_IRON_LAW}",
        "Investigate → hypothesize → confirm with evidence → then change code.",
        "3 failed hypotheses: stop and escalate; do not shotgun-fix.",
        "Minimal diff. Root cause, not symptom. Regression test that fails without the fix.",
        "QA loop: " + " → ".join(QA_LOOP) + ".",
        "CSO: name a concrete exploit scenario for any security claim; skip theater.",
        "Review before done: SQL safety, trust boundaries, conditional side effects, missing tests.",
        "If this touches UI, refuse AI slop: " + "; ".join(DESIGN_SLOP[:4]) + ".",
        "Autoplan: " + AUTOPLAN_PRINCIPLES["P5"],
    ]
    if kind in {"debug", "fix", "investigate"}:
        rules.insert(2, "You are in investigate mode. Do not implement until root cause is written.")
    return "\n".join(rules) + "\n\nTASK:\n" + text


def overlay_system_prompt(role: str, base: str) -> str:
    specialist = specialist_for_role(role)
    if not specialist:
        return base
    if specialist in (base or ""):
        return base
    return f"{base.rstrip()}\n\n{specialist}".strip()


def design_copy_rules() -> str:
    banned = "; ".join(DESIGN_SLOP)
    return (
        "Design bar (gstack): a human designer would not be embarrassed to sign this. "
        f"Never: {banned}."
    )


def ceo_review_brief(*, stage: str | None = None, idea: str = "") -> str:
    mode = default_ceo_mode(stage=stage)
    directives = " ".join(CEO_PRIME_DIRECTIVES[:4])
    return (
        f"CEO review mode: {mode.replace('_', ' ')}. {directives} "
        f"Idea under review: {idea[:160]}"
    ).strip()
