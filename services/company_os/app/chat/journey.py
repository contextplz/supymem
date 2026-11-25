"""Read-only chat guidance from the selected venture's saved workflow."""

from __future__ import annotations

import re
from typing import Any


_GUIDANCE_RE = re.compile(
    r"\b(?:where are we|where (?:do we|does this) stand|"
    r"what(?:'s| is) (?:going on|next|the status)|what should i do next|"
    r"what do i need to do|what needs my attention|(?:current|overall) status|"
    r"status update|any blockers|what(?:'s| is) blocking us|"
    r"why is execution blocked|what(?:['’]s| is) blocking execution|"
    r"how (?:do we|can we) get unstuck|what do you need from me|"
    r"what is the single next approved step|"
    r"(?:the|this|my|our) job is stuck|check its saved status|"
    r"where am i|what did (?:you|we) do(?: so far)?|what (?:have|has) (?:you|we) done|"
    r"what now|i(?:['’]m| am) stuck|(?:is|are) (?:it|this|we) (?:done|ready|there)(?: yet)?)\b",
    re.I,
)
_PROGRESS_RECAP_RE = re.compile(
    r"(?:(?:please|can you|could you|would you)\s+)?(?:recap|catch me up)"
    r"(?:\s+(?:on\s+)?(?:(?:our|the|my|this venture['’]s)\s+)?(?:progress|status|saved work)"
    r"|\s+what (?:we|you)(?: have|'ve)? (?:done|did)(?: so far)?)?\s*[.!?]*",
    re.I,
)
_BUILD_STATE_WORDS = {
    "queued": "waiting to start", "reserving_credits": "preparing to start",
    "provisioning_sandbox": "preparing its build environment", "running": "running",
    "syncing_artifacts": "saving its files", "needs_input": "waiting for your input",
    "cancelling": "being stopped", "succeeded": "finished", "failed": "failed",
    "cancelled": "cancelled", "rejected": "not accepted",
}


def build_state_words(state: str) -> str:
    """Plain wording for a build state; unknown states read as underscore-free text."""
    return _BUILD_STATE_WORDS.get(state) or str(state).replace("_", " ")


_CONTINUE_RE = re.compile(r"(?:(?:ok|okay|so|please)\W+)*(?:continue|keep going|carry on|go on)\W*", re.I)


def asks_for_guidance(question: str) -> bool:
    """Reserve the guided answer for broad venture questions, not specific facts."""
    text = (question or "").strip()
    if _CONTINUE_RE.fullmatch(text):
        return True
    # A read-only constraint can mention an action without asking about that
    # action. Keep "do not start a build" out of the subject classifier.
    subject = re.sub(r"\b(?:do not|don't|never)\s+(?:start|run|approve|deploy|publish)\b[^.!?]*", "", text, flags=re.I)
    return bool((_PROGRESS_RECAP_RE.fullmatch(subject.strip()) or _GUIDANCE_RE.search(subject)) and not re.search(
        r"\b(?:gmail|email|waitlist|signups?|users?|revenue|content agent|"
        r"latest mac|last mac|browser task|build|research|plan|"
        r"working authenticated product|public url|results?|means?|meaning|explain)\b", subject, re.I,
    ))


def _venture_action(venture_id: str, action: Any) -> dict[str, str] | None:
    if not isinstance(action, dict):
        return None
    href = action.get("href")
    prefix = f"/v/{venture_id}/"
    if not isinstance(href, str) or not href.startswith(prefix):
        return None
    return {"label": str(action.get("label") or "Review saved work")[:80],
            "href": href, "style": "primary"}


def _plain_saved_text(value: Any, limit: int = 180) -> str:
    """Keep saved diagnosis data from becoming formatting or instructions."""
    text = re.sub(r"[\r\n`*\[\]<>]", " ", str(value or ""))
    return " ".join(text.split())[:limit].rstrip()


def _pilot_next_step(venture_id: str, brief: dict[str, Any]) -> dict[str, Any] | None:
    """Give a startup with a live waitlist a specific validation checkpoint."""
    if (brief.get("idea") or {}).get("project_intent") == "personal":
        return None
    website = brief.get("website") or {}
    if not (website.get("live") and website.get("waitlist_enabled")):
        return None
    gtm = brief.get("gtm") or {}
    count = website.get("waitlist_count")
    requests = (f"{count} recorded request{'s' if count != 1 else ''}" if type(count) is int and count >= 0
                else "an unverified number of requests")
    body = (f"The landing page and managed waitlist are live, with {requests}. "
            "A request is interest, not a verified product user. ")
    bottleneck = _plain_saved_text(gtm.get("bottleneck")) if gtm.get("present") else ""
    gap = next((_plain_saved_text(item) for item in gtm.get("evidence_gaps") or []
                if _plain_saved_text(item)), "")
    if bottleneck:
        body += f"The saved GTM diagnosis names this bottleneck: {bottleneck}. "
    if gap:
        body += f"The first evidence gap to test is: {gap}. "
    if not bottleneck and not gap:
        body += "The first audience and its demand have not been validated in the saved diagnosis. "
    body += ("Next, choose one narrow pilot cohort and prepare one question or invitation that tests this gap. "
             "Review the exact audience, channel and wording before any outreach; nothing was sent.")
    return {"kind": "text", "body_md": body,
            "actions": [{"label": "Review pilot in GTM", "href": f"/gtm?venture={venture_id}", "style": "primary"}],
            "citations": []}


_RECAP_RE = re.compile(r"\b(?:what did (?:you|we) do(?: so far)?|what (?:have|has) (?:you|we) done|catch me up|recap)\b", re.I)
_STUCK_RE = re.compile(r"\b(?:i(?:['’]m| am)|im|we(?:['’]re| are)) stuck\b", re.I)
# next_step key prefix -> blocker guide for "Explain in detail".
_STUCK_GUIDES = (("idea.validation_failed", "job_failed"), ("plan.", "plan_needs_approval"), ("research.", "research_incomplete"),
                 ("idea.", "validation_incomplete"), ("build.", "build_not_accepted"))


def guidance_kind(question: str) -> str | None:
    text = (question or "").strip()
    if _CONTINUE_RE.fullmatch(text):
        return "continue"
    if _RECAP_RE.search(text):
        return "recap"
    if _STUCK_RE.search(text):
        return "stuck"
    return None


_MAC_STATUS = {"completed": "result needs your review", "stopped": "result needs your review",
               "needs_you": "needs you", "failed": "did not finish"}


def _mac_task_line(item: dict[str, Any]) -> str:
    """The founder's own request, not the raw order title ("Mac task in com.apple.Safari")."""
    app = re.sub(r"^Mac task in ", "", str(item.get("title") or "")).rsplit(".", 1)[-1] or "your Mac"
    what = _plain_saved_text(item.get("request"), 100) or f"A task in {app}"
    return f"Mac task: {what} - {_MAC_STATUS.get(str(item.get('status')), str(item.get('status') or 'saved'))}"


def _done_list(brief: dict[str, Any], venture_id: str = "") -> list[str]:
    """Saved milestones, finished experiments and recent Mac runs: what has actually happened."""
    idea, research = brief.get("idea") or {}, brief.get("research") or {}
    plan, website = brief.get("plan") or {}, brief.get("website") or {}
    last = (brief.get("builds") or {}).get("last") or {}
    skipped = ((brief.get("next_step") or {}).get("journey_overrides") or {}).get("skipped") or {}
    done = []
    if idea.get("confirmed") and not ({"clarification", "validation"} & skipped.keys()):
        done.append("the idea is confirmed")
    if research.get("present") and "research" not in skipped:
        done.append("research is saved")
    if plan.get("present"):
        done.append(f"an execution plan is {plan.get('state') or 'saved'}")
    if last.get("state"):
        done.append(f"the latest build is {build_state_words(last['state'])}")
    if website.get("live"):
        done.append("the landing page is live")
    try:
        from app.experiments.ledger import list_for_venture

        rows = [r for r in list_for_venture(venture_id) if r["state"] == "concluded"][-3:] if venture_id else []
        for row in rows:
            done.append(f"experiment {row['outcome']}: {row['hypothesis'][:80]}")
    except Exception as exc:  # noqa: BLE001 - the ledger never blocks a recap
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "journey.experiments")
    recent = [i for i in ((brief.get("device_work") or {}).get("recent") or []) if isinstance(i, dict) and i.get("title")]
    if recent:  # newest first: say the latest in plain words, collapse the rest
        done.append(_mac_task_line(recent[0]))
        if len(recent) > 1:
            done.append(f"{len(recent) - 1} earlier Mac task{'s' if len(recent) > 2 else ''}")
    return done


def _done_so_far(brief: dict[str, Any], venture_id: str = "") -> str:
    done = _done_list(brief, venture_id)
    return ("Done so far: " + ", ".join(done) + ".") if done else ""


def _saved_chat_work(venture_id: str, question: str) -> list[str] | None:
    import json

    from sqlalchemy import select

    from app.chat.compaction import context_for_question
    from app.chat.conversations import actor_id, current_conversation, list_conversations
    from app.chat.idea_bridge import written_request
    from app.chat.intent import _split_clauses
    from app.chat.store import _REPLY_TO
    from app.db.models import ChatMessage
    from app.org.characters import parse_speaker
    from app.state import db
    from app.tenancy import current_or_none

    try:
        items = context_for_question(venture_id, question)["items"]
        active, scope = current_conversation(), current_or_none()
        if (active and scope and active.visibility == "private" and active.actor_id
                and active.actor_id == actor_id() and scope.venture_id == venture_id
                and scope.workspace_id == active.workspace_id):
            from app.authz.workspace import assert_venture_access

            assert_venture_access(venture_id, scope.actor)
            for archived in (False, True):
                for conversation in list_conversations(venture_id, current=active.id, archived=archived)["items"]:
                    if conversation["id"] != active.id:
                        items.extend(context_for_question(venture_id, question, conversation_id=conversation["id"])["items"])
        candidates = [item for item in items if item["role"] == "assistant" and item["kind"] in {"text", "document"}]
        non_work = set()
        with db()() as session:
            for message in session.scalars(select(ChatMessage).where(
                    ChatMessage.venture_id == venture_id,
                    ChatMessage.id.in_([item["source_message_id"] for item in candidates]))):
                request_id = next((item[_REPLY_TO] for item in json.loads(message.citations_json or "[]")
                                   if isinstance(item, dict) and _REPLY_TO in item), None)
                request = session.get(ChatMessage, request_id) if request_id else None
                if request and (request.venture_id, request.workspace_id, request.conversation_id) == (
                        message.venture_id, message.workspace_id, message.conversation_id):
                    text = parse_speaker(request.body_md)[1]
                    if asks_for_guidance(text) and not any(written_request(clause) for clause in _split_clauses(text)):
                        non_work.add(message.id)
        saved = [_plain_saved_text(parse_speaker(item["body_md"])[1]) for item in candidates
                 if item["source_message_id"] not in non_work]
        return list(dict.fromkeys(text for text in saved if text))[:4]
    except Exception as exc:  # noqa: BLE001 - unreadable history is not evidence that no work happened
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, venture_id, "journey.chat_history")
        return None


def _next_step_card(venture_id: str, step: dict[str, Any]) -> dict[str, Any] | None:
    """Confirmation card for a next step whose chat action is consequential."""
    from app.chat.actions import REGISTRY
    from app.chat.confirm import ConfirmationCard

    action = REGISTRY.get(str(step.get("chat_action") or ""))
    if action is None or not action.consequential:
        return None
    if action.name == "research.start":
        from app.chat.build_path import research_card
        return research_card(venture_id)["actions"][0]
    if action.name == "job.retry":  # the card is built from the real failed job, not from the step text
        from app.chat import failed_jobs

        card = next((a for a in failed_jobs.prepare(venture_id, (step.get("payload") or {}).get("kind")).get("actions") or []
                     if a.get("confirmation")), None)
        return card
    if action.name in {"site.publish", "waitlist.enable"}:  # full public-consequence copy lives with the action
        from app.chat import site_actions

        return (site_actions.publish_card if action.name == "site.publish" else site_actions.waitlist_card)(venture_id)
    return ConfirmationCard.for_action(
        action, interpretation=str(step["title"])[:200],
        consequences=[str(step.get("detail") or "")[:300]] if step.get("detail") else [],
        resulting_state=dict(step.get("payload") or {}),
    ).as_action()


def guided_answer(venture_id: str, brief: dict[str, Any], workflow: dict[str, Any],
                  question: str = "") -> dict[str, Any]:
    """State, next action and recovery. "Recap", "I'm stuck" and "continue" each get their own answer
    on top of the same saved state; links only to existing review screens."""
    kind = guidance_kind(question)
    step = brief.get("next_step") if isinstance(brief.get("next_step"), dict) else {}
    if step.get("chat_action") == "research.start" and workflow.get("state") not in {"paused", "queued", "working"}:
        from app.chat.build_path import research_prerequisites
        blocked = research_prerequisites(venture_id)
        if blocked:
            return blocked
    if kind == "recap":
        reply = _guided_base(venture_id, brief, workflow, include_done=False)
        done = _done_list(brief, venture_id)
        saved = _saved_chat_work(venture_id, question)
        lead = ("Here is what has been done so far:\n" + "\n".join(f"- {d}" for d in done)) if done or saved else (
            "I could not read your saved chat work. Ask me to recall the document you need." if saved is None else
            "Nothing has been completed yet; we are at the start.")
        if saved:
            lead += "\nYour saved chat includes these replies, not verification of completed external work:\n" + "\n".join(f"- {text}" for text in saved)
        return {**reply, "body_md": f"{lead}\n\nWhere it stands now: {reply['body_md']}"}
    reply = _guided_base(venture_id, brief, workflow)
    if kind == "stuck":
        from app.chat.blockers import guide_code, with_explain

        code = "job_failed" if workflow.get("state") == "failed" else next(
            (g for prefix, g in _STUCK_GUIDES if str(step.get("key") or "").startswith(prefix)), "stuck")
        return with_explain({**reply, "body_md": (
            "That is a frustrating spot, and it is fixable. The blocker is likely here: "
            f"{reply['body_md']} The one thing to do now is the button below; tap Explain in detail "
            "for the exact clicks.")}, guide_code(code) or "stuck")
    if kind == "continue" and step.get("title") and workflow.get("state") != "paused":
        card = _next_step_card(venture_id, step)
        if card:
            return {"kind": "confirmation", "citations": [], "actions": [card], "body_md": (
                f"Picking up where we left off: {step['title']}. {step.get('detail') or ''} "
                "Confirm below and I will run it; nothing else starts.").replace("  ", " ")}
        return {**reply, "body_md": f"Picking up where we left off. {reply['body_md']}"}
    return reply


def _guided_base(venture_id: str, brief: dict[str, Any], workflow: dict[str, Any],
                 include_done: bool = True) -> dict[str, Any]:
    name = str((brief.get("idea") or {}).get("name") or "This venture")
    state = str(workflow.get("state") or "idle")
    activity = str(workflow.get("activity") or "No active background work is recorded.")
    next_step = str(workflow.get("next_step") or "")
    attention = brief.get("attention") or {}
    needs = attention.get("needs_you") or []
    plan = brief.get("plan") or {}
    last_build = (brief.get("builds") or {}).get("last")
    action = _venture_action(venture_id, workflow.get("action"))
    if (workflow.get("landing_page") or {}).get("available") and action is None:
        action = {"label": "Review page", "href": f"/v/{venture_id}/site", "style": "primary"}
    if state == "paused":
        return {"kind": "text", "body_md": (
            f"{name}: this venture is paused. Review its saved work, then use Resume on the Ventures page "
            "when you are ready to continue. No work was restarted."),
            "actions": [{"label": "Resume", "action": "resume", "style": "primary"},
                        {"label": "Open Ventures", "href": "/", "style": "ghost"}],
            "citations": []}

    from app.ventures.current_idea import (
        current_revision,
        intake_pending,
        revision_fields,
    )
    if (brief.get("idea") or {}).get("confirmed") is False and intake_pending(venture_id):
        from sqlalchemy import select

        from app.chat.blockers import with_explain
        from app.db.models import Venture
        from app.db.supymem_models import ClarifierTurn, IdeaSession
        from app.state import db

        with db()() as session:
            revision = current_revision(session.get(Venture, venture_id), session)
            idea = session.get(IdeaSession, revision.session_id) if revision else None
            latest = session.scalar(select(ClarifierTurn).where(
                ClarifierTurn.session_id == idea.id,
                ClarifierTurn.workspace_id == idea.workspace_id,
                ClarifierTurn.venture_id == venture_id,
            ).order_by(ClarifierTurn.turn.desc()).limit(1)) if idea else None
            skipped = bool(revision_fields(revision).get("_questions_skipped"))
            question_pending = bool(idea and idea.state not in {"confirmed", "abandoned"} and latest
                                    and latest.role == "agent" and latest.kind == "question" and not skipped)
            review_ready = bool(idea and idea.state == "reviewing" and latest
                                and latest.role == "agent" and latest.kind == "proposal")
            question = _plain_saved_text(latest.text, 1000) if question_pending else ""

        if question:
            return with_explain({"kind": "text", "body_md":
                f"{name}: Your next step is to answer your saved idea question: {question} "
                "You can also skip the questions and review what you have shared. "
                "Use Explain in detail if you want help. No new work was started.",
                "actions": [], "citations": []}, "stuck")
        if not skipped and (not latest or idea.state == "recon"):
            return with_explain({"kind": "text", "body_md":
                f"{name}: Your next step is to review your idea while its question is being prepared. "
                "Use Explain in detail if you want help, or skip this step. No new work was started.",
                "actions": [], "citations": []}, "stuck")
        body = f"{name}: Your saved idea is ready to review." if review_ready else f"{name}: You can review your saved idea."
        if skipped:
            body += " The questions you skipped stay unanswered."
        body += " Review or edit your idea when you are ready, or skip this step. No new work was started."
        return {"kind": "text", "body_md": body,
                "actions": [{"label": "Review idea", "href": f"/v/{venture_id}/idea", "style": "primary"}],
                "citations": []}

    if state in {"queued", "working"}:
        body = f"{name}: {activity}"
        body += f" Next: {next_step}" if next_step else ""
    elif state in {"needs_you", "failed", "cancelled", "paused", "ready"}:
        body = f"{name}: {activity}"
        body += f" Next: {next_step}" if next_step else ""
    else:
        body = f"{name}: no company job is in progress."

    landing = workflow.get("landing_page")
    landing_action = isinstance(workflow.get("action"), dict) and "/site" in str(workflow["action"].get("href") or "")
    build_blocked = not (landing and landing.get("available")) and isinstance(last_build, dict) and last_build.get("state") in {"failed", "cancelled", "rejected"}
    if build_blocked:
        reason = last_build.get("failure_reason") or last_build.get("error_code")
        if reason:
            # Failure text is saved data, not an instruction or Markdown.
            reason = re.sub(r"[\r\n`*\[\]<>]", " ", str(reason))[:240].strip()
            body += f" The latest build stopped: {reason}."
        body += " Review its saved result and usage before choosing a new estimate; builds do not retry automatically."
        if not landing_action and (action is None or state not in {"working", "queued", "needs_you"}):
            action = {"label": "Review build result", "href": f"/v/{venture_id}/builds", "style": "primary"}

    if needs and state not in {"needs_you", "failed"}:
        body += f" The saved attention item is: {str(needs[0])[:160]}."

    # A live waitlist may coexist with an old open landing-page task. Keep
    # that task review visible while giving the founder the next pilot move.
    if (not build_blocked and state in {"idle", "completed", "needs_you"}
            and action is not None and action["label"] == "Open saved work"
            and (brief.get("website") or {}).get("live")):
        pilot = _pilot_next_step(venture_id, brief)
        if pilot is not None:
            pilot["body_md"] = (body + " Reconcile the saved task against the live landing page first. "
                                + pilot["body_md"])
            pilot["actions"] = [action, *pilot["actions"]]
            return pilot

    if action is None:
        if state in {"idle", "completed"}:
            pilot = _pilot_next_step(venture_id, brief)
            if pilot is not None:
                pilot["body_md"] = body + " " + pilot["body_md"]
                return pilot
        step = brief.get("next_step")
        if isinstance(step, dict) and step.get("title") and str(step.get("href") or "").startswith(("/v/", "/gtm")):
            body += f" Next: {step['title']}. {step.get('detail') or ''}".rstrip()
            card = _next_step_card(venture_id, step)
            if card:
                return {"kind": "confirmation", "body_md": body, "actions": [card], "citations": []}
            action = {"label": str(step.get("cta_label") or "Open")[:80], "href": step["href"], "style": "primary"}
        elif plan.get("present") and plan.get("state") in {"approved", "executing"} and not plan.get("stale_dependencies") and not plan.get("newer_plan_id"):
            body += " Review the approved milestone before starting a product build."
            action = {"label": "Review build", "href": f"/v/{venture_id}/builds", "style": "primary"}
        elif plan.get("present") or (brief.get("research") or {}).get("present"):
            body += " Review the execution plan and its prerequisites before approving work."
            action = {"label": "Review plan", "href": f"/v/{venture_id}/plan", "style": "primary"}
        else:
            body += " Start with the idea review, then validate it and prepare an execution plan."
            action = {"label": "Review idea", "href": f"/v/{venture_id}/idea", "style": "primary"}

    done = _done_so_far(brief, venture_id) if include_done else ""
    if done:
        body += " " + done
    actions = [action]
    if build_blocked and not landing_action and action["href"] != f"/v/{venture_id}/builds":
        actions.append({"label": "Review build result", "href": f"/v/{venture_id}/builds", "style": "ghost"})
    return {"kind": "text", "body_md": body, "actions": actions, "citations": []}
