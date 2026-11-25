"""Saved prerequisites shared by build guidance and research admission."""
from app.db.models import Venture
from app.state import db


def answers_pending(session, venture):
    from app.ventures.current_idea import skipped_questions_confirmed
    from app.ventures.startup_review import enabled, initialize
    from app.ventures.journey_overrides import read

    return ("clarification" not in read(venture.id, session=session)["skipped"]
            and enabled(session, venture.id) and not skipped_questions_confirmed(venture, session) and not initialize(session, venture)["complete"])


def remaining(venture_id, brief=None):
    from app.ventures.state_brief import get_venture_state_brief

    brief = brief if brief is not None else get_venture_state_brief(venture_id, force=True)
    idea, research, plan = (brief.get(key) or {} for key in ("idea", "research", "plan"))
    if idea.get("paused"):
        return [("resume", "Resume")]
    if idea.get("project_intent") == "personal":
        return []
    steps = []
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is not None and answers_pending(session, venture):  # no row: nothing saved to confirm, never a 500
            steps.append(("startup.review", "Confirm your saved answers and priorities"))
    if not idea.get("confirmed"):
        steps.append(("idea.review", "Review and confirm your idea"))
    if research.get("plan_ready") is not True and not brief.get("journey_overrides", {}).get("skipped"):
        steps.append(("research.prepare", "Research competitors and demand"))
    if not plan.get("present"):
        steps.append(("plan.generate", "Make a build plan"))
    else:
        from app.planning.compiler import get_plan

        try:
            detail = get_plan(plan["id"]) if plan.get("id") else {}
        except (KeyError, ValueError):
            detail = {"stale_dependencies": True}
        if detail.get("stale_dependencies") or detail.get("newer_plan_id") or detail.get("method") != "metered_foundry_plan":
            steps.append(("plan.generate", "Update the build plan"))
        elif plan.get("state") not in {"approved", "executing"}:
            steps.append(("plan.approve", "Review and approve the build plan"))
    return steps
