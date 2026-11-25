"""Founder quiz / validation gates must not reuse leftover MEM from a prior run.

MEM is durable SQLite (doc 72 §9). Venture-scoped review/decision keys therefore
survive a crash and a failed retry, which used to skip the founder cards in ~10ms.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import OnboardingStep
from app.state import db


def _mem():
    from app import state as state_mod

    return state_mod.MEM

VALIDATION_MEM_KINDS = (
    "validation_review",
    "validation_decision",
    "validation_mode",
    "gstack_plan",
    "product_brief",
    "builder_choice",
    "builder_session",
)
QUIZ_MEM_KINDS = (
    "answers",
    "answers_source",
    "gstack_plan",
    "product_answers",
    "product_understanding",
    "builder_choice",
    "builder_session",
)
FOUNDER_GATE_STEPS = (
    "understand.product_ask",
    "understand.product_brief",
    "validate.ask",
    "validate.office_hours_plan",
    "validation.review_choice",
    "validation.decision",
)


def run_gate_key(kind: str, venture_id: str, run_id: str) -> str:
    return f"{kind}:{venture_id}:{run_id}"


def get_run_gate(kind: str, venture_id: str, run_id: str):
    """Read a founder-gate value bound to this run only. Never fall back to leftover venture keys."""
    return _mem().get(run_gate_key(kind, venture_id, run_id))


def set_run_gate(kind: str, venture_id: str, run_id: str, value) -> None:
    store = _mem()
    store[run_gate_key(kind, venture_id, run_id)] = value
    # Dashboard / charter still read the venture-scoped key.
    store[f"{kind}:{venture_id}"] = value


def clear_founder_gates(venture_id: str, run_id: str | None = None, *, include_quiz: bool = False) -> None:
    store = _mem()
    kinds = VALIDATION_MEM_KINDS + (QUIZ_MEM_KINDS if include_quiz else ())
    for kind in kinds:
        store.pop(f"{kind}:{venture_id}", None)
        if run_id:
            store.pop(run_gate_key(kind, venture_id, run_id), None)


def void_founder_gate_steps(run_id: str) -> None:
    """Drop completed quiz/validation steps so a failed retry must re-ask."""
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(OnboardingStep).where(
                OnboardingStep.run_id == run_id,
                OnboardingStep.step_id.in_(FOUNDER_GATE_STEPS),
                OnboardingStep.status.in_(("done", "skipped")),
            )
        ).all()
        for row in rows:
            session.delete(row)
        session.commit()


def prepare_failed_resume(run) -> None:
    """Continue the failed run without restarting from orient, but re-ask founder gates."""
    from app.onboarding.registry import phase_index
    from app.onboarding.state import persist_run

    has_landing = False
    try:
        from app.firstlook.service import landing_exists

        has_landing = landing_exists(run.venture_id, getattr(run, "workspace_id", None))
    except Exception:
        has_landing = False
    if not has_landing:
        clear_founder_gates(run.venture_id, run.id, include_quiz=True)
        void_founder_gate_steps(run.id)
        entry = getattr(run, "entry_mode", None) or "idea"
        if entry == "idea" and phase_index(run.phase, entry) >= phase_index("validate", entry):
            run.phase = "validate"
            run.phases_done = phase_index("validate", entry)
    run.error = None
    persist_run(run)
