"""Actionable chat cards for the saved product-build path."""
from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard


def local_build_reply(venture_id, brief=None, *, plan_task_id=None, skip_startup_review=False):
    from fastapi import HTTPException
    from app.builder.ctl import local_tool
    from app.ventures.build_prerequisites import remaining
    from app.ventures.state_brief import get_venture_state_brief

    brief = brief if brief is not None else get_venture_state_brief(venture_id, force=True)
    steps = remaining(venture_id, brief)
    if skip_startup_review:
        steps = [step for step in steps if step[0] != "startup.review"]
    if not steps and not (brief.get("idea") or {}).get("confirmed"):
        steps = [("idea.review", "Review and confirm your idea")]
    if steps:
        return reply(venture_id, steps)
    try:
        provider = local_tool(venture_id)
    except HTTPException as exc:
        return {"kind": "needs_you", "body_md": exc.detail["message"] + " No build has started.",
                "actions": [{"label": "Connect your coding tool", "href": "/connections", "style": "primary"}], "citations": []}
    instruction = f"Build the app with {provider} from my saved idea and plan."
    if plan_task_id:
        instruction = f"Implement plan task {plan_task_id} with {provider}."
    return {"kind": "text", "body_md": "Your saved scope is ready for your coding tool. Choose Build my app to start. Nothing has started or been published.",
            "actions": [{"label": "Build my app", "action": "build.tool.start", "payload": {"text": instruction,
                **({"plan_task_id": plan_task_id} if plan_task_id else {})}, "style": "primary"}], "citations": []}


def startup_review_reply(venture_id):
    from app.db.models import Venture
    from app.state import db
    from app.ventures.startup_review import enabled, get

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None or venture.archived_at is not None:
            raise KeyError(venture_id)
        needs_review = enabled(session, venture_id)
    context = get(venture_id) if needs_review else None
    if context and not context["complete"]:
        question = context.get("question")
        body = (question["text"] + " Review your saved answers below, then choose your priorities."
                if question else "What should come first for you? Choose your priorities below before research or a build starts.")
        return {"kind": "startup_review", "body_md": body + " Nothing new has started.",
                "actions": [], "citations": []}
    from app.chat.router import _product_build_reply
    from app.ventures.state_brief import get_venture_state_brief

    next_step = _product_build_reply(venture_id, get_venture_state_brief(venture_id, force=True), skip_startup_review=True)
    return {**next_step, "body_md": "You have no saved answers or priorities left to review.\n\n" + next_step["body_md"]}


def research_prerequisites(venture_id):
    from app.research.context import pending_steps

    steps = pending_steps(venture_id)
    if not steps:
        return None
    action, label = steps[0]
    lead = "One quick step before research:" if len(steps) == 1 else f"{len(steps)} quick steps before research:"
    return {"kind": "needs_you", "action_started": False,
            "body_md": lead + "\n\n" + "\n".join(f"{number}. {text}." for number, (_, text) in enumerate(steps, 1))
                + "\n\nNothing was started.",
            "actions": [{"label": label, "action": action, "style": "primary"}], "citations": []}


def research_card(venture_id):
    from app.ventures.journey_overrides import read as journey_choices
    from app.research.local_providers import setup_reply

    blocked = research_prerequisites(venture_id)
    if blocked:
        return blocked
    if setup := setup_reply():
        return setup
    skipped = bool(journey_choices(venture_id)["skipped"])
    first = ("Uses your saved idea and website context. Skipped checks stay assumptions, not validation."
             if skipped else "Uses your confirmed idea and any saved answers or priorities.")
    note = " Skipped checks stay assumptions." if skipped else ""
    card = ConfirmationCard.for_action(get_action("research.start"),
        interpretation="Start research using your saved context.",
        consequences=[first, "Nothing is built or published. Missing evidence stays unknown."], resulting_state={})
    return {"kind": "confirmation", "body_md": f"Your research is ready to start.{note} No new research has started yet.",
            "actions": [card.as_action()], "citations": []}


def reply(venture_id, steps):
    lines = ["Here is what remains before I can build your app:"]
    for number, (action, label) in enumerate(steps, 1):
        lines.append(f"{number}. {label}.")
    lines.append(f"{len(steps) + 1}. Build the app.")
    lines.append("Nothing was started or published.")
    action, label = steps[0]
    return {"kind": "navigation", "body_md": "\n\n".join(lines),
            "actions": [{"label": label, "action": action, "style": "primary",
                         **({"payload": {"regenerate": True}} if action == "plan.generate" and label == "Update the build plan" else {})}], "citations": []}
