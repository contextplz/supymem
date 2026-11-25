"""Chat-native plan chain: resources -> quote -> generate -> approve -> next work item.

Every step calls the same service the Plan page uses. Anything that spends credits
or commits (generate, approve, start work) is a ConfirmationCard; a button without
a card only ever *prepares* that card, it never acts.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from sqlalchemy import select

from app.chat.actions import get_action, question_reply
from app.chat.blockers import with_explain
from app.chat.confirm import ConfirmationCard
from app.db.models import Job, Venture
from app.state import db

_ACTIVE = ["scheduled", "leased", "retry_wait"]
_RESEARCH_CODES = {"plan_research_required", "confirmed_revision_required"}


def _text(body: str, *actions: dict, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": list(actions), "citations": []}


def _workspace(venture_id: str) -> str:
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise KeyError("venture")
        return venture.workspace_id


def _open(label: str, href: str, style: str = "secondary") -> dict:
    return {"label": label, "href": href, "style": style}


def button(label: str, action: str, style: str = "primary", payload: dict | None = None) -> dict:
    """A card-less button: ChatDock posts it to /chat/confirm with its payload (empty by default)."""
    return {"label": label, "action": action, "style": style, **({"payload": payload} if payload else {})}


def _generating(venture_id: str) -> bool:
    with db()() as session:
        return session.scalar(select(Job.id).where(Job.venture_id == venture_id,
            Job.kind == "plan.compile", Job.status.in_(_ACTIVE)).limit(1)) is not None


def _plans(venture_id: str, *states: str) -> list[dict]:
    from app.planning.compiler import list_plans

    return [p for p in list_plans(venture_id) if p["state"] in states]


# ---------------------------------------------------------------- resources

def resources_none(venture_id: str) -> dict:
    from app.planning import resources

    wid = _workspace(venture_id)
    inventory = resources.update_inventory(wid, venture_id, [], True)
    if inventory["complete"] and not inventory["assets"]:
        from app.autopilot.receipts import persisted_record
        from app.db.supymem_models import ExistingAsset

        with db()() as session:
            marker = session.get(ExistingAsset, resources._marker_id(wid, venture_id))
            if marker and marker.kind == resources.MARKER_KIND and marker.source == resources.SOURCE:
                persisted_record(ExistingAsset, marker.id,
                                 {"detail": marker.detail, "source": resources.SOURCE, "kind": resources.MARKER_KIND})
    nxt = generate_prepare(venture_id)
    nxt["body_md"] = "Saved: you have no existing tools or resources, so the plan starts from scratch. " + nxt["body_md"]
    return nxt


# ---------------------------------------------------------------- quote / generate

def _quote(venture_id: str, **revision: str) -> dict | dict[str, str]:
    from app.planning.compiler import generation_quote

    try:
        return generation_quote(venture_id, _workspace(venture_id), **revision)
    except ValueError as exc:
        return {"blocked": str(exc)}


def _blocked(venture_id: str, code: str) -> dict:
    if code == "plan_model_required":
        from app.core.local_mode import is_local

        if is_local():
            from app.core.local_capabilities import _model_missing

            missing = [field for _, field in _model_missing()]
            return _text(f"Open Keys and connections > Your AI provider and save your {', '.join(missing)}, then ask me to generate your plan again." if missing else
                         "Open Keys and connections > Your AI provider, check your Provider URL, API key and Chat model, then ask me to generate your plan again.",
                         _open("Keys and connections", "/settings/local"))
        return _text("Open Keys and connections and save your model-provider key, then ask me to generate your plan again.",
                     _open("Keys and connections", "/settings"))
    if code == "plan_resource_inventory_required":
        return question_reply("Before I plan, do you already have any tools, accounts or assets I should reuse? "
                     "If not, tell me you have no existing resources and I will prepare the plan.",
                     [button("No existing resources", "plan.resources_none"),
                      _open("Add resources", f"/v/{venture_id}/plan")])
    if code in _RESEARCH_CODES:
        need = ("confirm your idea" if code == "confirmed_revision_required" else "finish research")
        href = "idea" if code == "confirmed_revision_required" else "research"
        from app.ventures.current_idea import intake_pending
        if intake_pending(venture_id):
            return with_explain(_text(f"The standard plan uses evidence: {need} is recommended, not mandatory. "
                "Your idea still needs clarification. Use Explain in detail for the next step. Nothing was started."),
                "validation_incomplete")
        return with_explain(_text(f"The standard plan uses evidence: {need} is recommended, not mandatory. "
                     "Confirm a jump to plan from saved context with lower confidence. Nothing was started.",
                     button("Skip to planning", "journey.jump_to", "primary",
                            {"step": "landing_page", "why": "Plan from existing context without earlier checks."}),
                     _open(f"Open {href}", f"/v/{venture_id}/{href}"), kind="navigation"),
                     "validation_incomplete" if href == "idea" else "research_incomplete")
    return _text("I could not prepare a plan estimate right now. Nothing was started or charged.",
                 _open("Open plan", f"/v/{venture_id}/plan"))


def quote_reply(venture_id: str) -> dict:
    q = _quote(venture_id)
    if "blocked" in q:
        return _blocked(venture_id, q["blocked"])
    body = "Your saved context is ready for plan generation. You review the draft before work starts."
    if not q["sufficient_now"]:
        return with_explain(_text("You have reached your usage limit. Open Settings to get more, then retry.",
                                  _open("Open settings", f"/v/{venture_id}/settings", "primary")), "insufficient_credits")
    return _text(body, button("Prepare plan generation", "plan.generate"))


_REGENERATE = re.compile(r"\b(?:re-?generat\w*|re-?do|re-?run|redone|again|(?:new|fresh|another|different)\s+plan)\b", re.I)


def asks_regenerate(text: str) -> bool:
    return bool(_REGENERATE.search(text or ""))


def _blocked_draft(draft_id: str) -> bool:
    """A waiting draft that cannot be approved as is: open review findings, stale inputs or superseded."""
    from app.planning.compiler import get_plan

    try:
        plan = get_plan(draft_id)
    except (KeyError, ValueError):
        return True
    return bool(plan.get("review_findings") or plan.get("stale_dependencies") or not plan.get("reviewable"))


def generate_prepare(venture_id: str, regenerate: bool = False) -> dict:
    """Show the generate card with the exact ceiling; never spends anything itself.

    A draft waiting for review normally blocks a second generation. It does not when the draft has blocking
    review findings (or is stale), or the founder explicitly asked for a new one: then the card regenerates.
    """
    if _generating(venture_id):
        return _text("The plan is already being generated. I will post here when it is ready.")
    existing = _plans(venture_id, "founder_review", "approved")
    replacing = False
    if existing:
        if existing[0]["state"] == "approved" and not regenerate:
            return _text("Your plan is already approved.", _open("Open plan", f"/v/{venture_id}/plan"))
        blocked = _blocked_draft(existing[0]["id"])
        if not (regenerate or blocked):
            return _text("A plan draft is already waiting for your review, so I did not generate another.",
                         button("Review and approve", "plan.approve"), button("Regenerate plan", "plan.generate", "secondary", {"regenerate": True}))
        replacing = True
    q = _quote(venture_id)
    if "blocked" in q:
        return _blocked(venture_id, q["blocked"])
    if not q["sufficient_now"]:
        return with_explain(_text("You have reached your usage limit. Open Settings to get more, then retry.",
                     _open("Open settings", f"/v/{venture_id}/settings", "primary")), "insufficient_credits")
    from app.ventures.journey_overrides import NOTE, read as journey_choices
    note = " " + NOTE if journey_choices(venture_id)["skipped"] else ""
    card = ConfirmationCard.for_action(get_action("plan.generate"),
        interpretation=("Generate a replacement execution plan for your review." if replacing
                        else "Generate your execution plan from the available saved context."),
        consequences=["Nothing is approved or started. You review the plan before any work runs." + note],
        resulting_state={"ceiling_micro": q["ceiling_micro"], **({"regenerate": True} if replacing else {})})
    if replacing:
        why = ("The saved plan needs an update before new build work. "
               if _blocked_draft(existing[0]["id"]) else "")
        return _text(f"{why}Ready to regenerate your plan.",
                     replace(card, confirm_label="Regenerate plan").as_action(), kind="confirmation")
    return _text("Ready to generate your plan." + note,
                 replace(card, confirm_label="Generate plan").as_action(), kind="confirmation")


def revise_prepare(venture_id: str, request: str) -> dict:
    """A revision of the latest plan from the founder's own words: quote first, then a card. The old plan stays in history."""
    if _generating(venture_id):
        return _text("The plan is already being generated. I will post here when it is ready.")
    base = (_plans(venture_id, "approved", "founder_review") or [None])[0]
    if base is None:
        return generate_prepare(venture_id)
    revision = {"base_plan_id": base["id"], "instruction": " ".join(request.split())[:8000]}
    q = _quote(venture_id, **revision)
    if "blocked" in q:
        return _blocked(venture_id, q["blocked"])
    if not q["sufficient_now"]:
        return with_explain(_text("You have reached your usage limit. Open Settings to get more, then retry."),
                            "insufficient_credits")
    card = ConfirmationCard.for_action(get_action("plan.generate"),
        interpretation=f"Revise your current plan: {revision['instruction'][:300]}",
        consequences=["Your current plan stays in history. You review the new draft before any work runs."],
        resulting_state={"ceiling_micro": q["ceiling_micro"], **revision})
    return _text("Ready to revise your plan with your request.",
                 replace(card, confirm_label="Revise plan").as_action(), kind="confirmation")


def generate_run(venture_id: str, payload: dict) -> dict:
    from app.planning import compiler
    from app.chat.milestone_hooks import started_reply

    if _generating(venture_id):
        return generate_prepare(venture_id)  # repeat confirm: never a second generation
    if payload.get("base_plan_id") and payload.get("instruction"):
        revision = {"base_plan_id": str(payload["base_plan_id"]), "instruction": str(payload["instruction"])[:8000]}
        q = _quote(venture_id, **revision)
        if "blocked" in q or q["ceiling_micro"] != payload.get("ceiling_micro"):
            return revise_prepare(venture_id, revision["instruction"])  # inputs or price changed since the card
        try:
            started = compiler.start(venture_id, _workspace(venture_id), **revision)
        except ValueError as exc:
            return _blocked(venture_id, str(exc))
        return started_reply(_text("Plan revision is running. I will post the new draft here for your review."), started.get("job_id"))
    existing = _plans(venture_id, "founder_review", "approved")
    if existing and not payload.get("regenerate"):
        return generate_prepare(venture_id)
    q = _quote(venture_id)
    if "blocked" in q or q["ceiling_micro"] != payload.get("ceiling_micro"):
        return generate_prepare(venture_id, bool(payload.get("regenerate")))  # inputs or price changed since the card
    try:
        started = compiler.start(venture_id, _workspace(venture_id))
    except ValueError as exc:
        return _blocked(venture_id, str(exc))
    return started_reply(_text("Plan generation is running. I will post here when the draft is ready for your review.",
                 _open("Watch progress", f"/v/{venture_id}/plan")), started.get("job_id"))


# ---------------------------------------------------------------- approve

def _summary(plan: dict) -> list[str]:
    titles = [m["title"] for m in plan.get("milestones") or [] if m.get("included", True)]
    lines = [f"{len(titles)} milestone{'' if len(titles) == 1 else 's'}: " + "; ".join(titles[:3]) + (" ..." if len(titles) > 3 else "")]
    limits = []
    if plan.get("evidence_gaps"):
        limits.append(f"{len(plan['evidence_gaps'])} evidence gap{'' if len(plan['evidence_gaps']) == 1 else 's'}")
    if plan.get("out_of_scope"):
        limits.append(f"{len(plan['out_of_scope'])} step(s) unavailable")
    if plan.get("conditional"):
        limits.append(f"{len(plan['conditional'])} conditional")
    return lines + ["Open limits: " + ", ".join(limits) + "."]


def approve_prepare(venture_id: str, actor: str | None) -> dict:
    from app.authz.workspace import subject_person_id
    from app.chat import proposals
    from app.planning.compiler import get_plan

    drafts = _plans(venture_id, "founder_review")
    if not drafts:
        if _plans(venture_id, "approved"):
            return _text("Your plan is already approved.", _open("Open plan", f"/v/{venture_id}/plan"))
        if _generating(venture_id):
            return _text("The plan is still being generated. I will post here when it is ready to approve.")
        return _text("No plan is saved yet. Prepare one from the available context; skipped checks remain assumptions.",
                     button("Prepare plan generation", "plan.generate"))
    try:
        proposal = proposals.prepare_plan_approval(venture_id, subject_person_id(actor), drafts[0]["id"])
        plan = get_plan(drafts[0]["id"])
    except (ValueError, KeyError):
        return with_explain(_text("This plan needs changes before it can be approved (it is stale or has open review findings). "
                     "Nothing was approved.", button("Regenerate plan", "plan.generate", payload={"regenerate": True}),
                     _open("Review plan", f"/v/{venture_id}/plan", "secondary")), "plan_needs_approval")
    card = ConfirmationCard.for_action(get_action("plan.approve"),
        interpretation="Approve this exact reviewed plan.",
        consequences=_summary(plan) + ["Approving records the plan and adds its first work items. It does not start work, publish or send messages."],
        resulting_state={"proposal_id": proposal["id"]})
    return _text("Here is the plan as reviewed. Confirm to approve it.",
                 replace(card, confirm_label="Approve plan").as_action(),
                 _open("Read full plan", f"/v/{venture_id}/plan#plan-review"), kind="confirmation")


def approve_run(venture_id: str, actor: str | None, payload: dict) -> dict:
    from app.authz.workspace import subject_person_id
    from app.chat import proposals

    pid = str(payload.get("proposal_id") or "")
    if not pid:
        return approve_prepare(venture_id, actor)
    try:
        confirmed = proposals.confirm(venture_id, subject_person_id(actor), pid)
    except (ValueError, KeyError):
        return _text("I could not approve that review: it expired or the plan changed. Nothing was approved; ask me again for a fresh review.",
                     _open("Review plan", f"/v/{venture_id}/plan"))
    from app.autopilot.receipts import persisted_record
    from app.db.supymem_models import ChatProposal, ExecutionPlan

    receipt = confirmed.get("receipt") or {}
    plan_id = receipt.get("plan_id")
    with db()() as session:
        plan = session.get(ExecutionPlan, plan_id) if plan_id else None
        proposal = session.get(ChatProposal, pid)
        if (confirmed.get("state") == "succeeded" and plan and proposal
                and plan.venture_id == venture_id and plan.state == "approved"
                and plan.approved_at is not None and plan.approved_by == subject_person_id(actor)):
            persisted_record(ExecutionPlan, plan.id,
                             {"state": "approved", "approved_by": plan.approved_by, "structured": plan.structured})
            persisted_record(ChatProposal, proposal.id,
                             {"state": "succeeded", "actor_id": subject_person_id(actor), "receipt_json": proposal.receipt_json})
    task = next_task(venture_id)
    if not task:
        return _text("Plan approved. It has no open work items yet.", _open("Open work", f"/v/{venture_id}/tasks"))
    reply = task_card(venture_id, task, actor)
    reply["body_md"] = f"Plan approved. Next: **{task['title']}**. " + reply["body_md"]
    return reply


# ---------------------------------------------------------------- work items

def open_tasks(venture_id: str) -> list[dict]:
    from app.planning.store import list_current_tasks

    return [t for t in list_current_tasks(venture_id) if str(t.get("source") or "").startswith("approved_plan:")]


def next_task(venture_id: str) -> dict | None:
    return next(iter(open_tasks(venture_id)), None)


def _managed_build_reply(venture_id: str, task: dict, actor: str | None) -> dict | None:
    """A managed build goes straight to the one build card (no second 'confirm to prepare' card)."""
    from app.chat.router import _product_build_reply
    from app.ventures.state_brief import get_venture_state_brief

    plan_task = next((t[len("plan_task:"):] for t in task.get("tags") or [] if str(t).startswith("plan_task:")), None)
    try:
        return _product_build_reply(venture_id, get_venture_state_brief(venture_id, force=True), actor,
                                    hint=str(task.get("title") or ""), task_id=plan_task)
    except Exception as exc:  # noqa: BLE001 - fall back to the plain Work card
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "plan.managed_build_card")
        return None


def task_card(venture_id: str, task: dict, actor: str | None = None) -> dict:
    title = str(task["title"])[:120]
    tags = task.get("tags") or []
    managed = "capability:build.run" in tags
    if managed and actor and (built := _managed_build_reply(venture_id, task, actor)) is not None:
        return built
    if not task.get("runnable", True) and not managed:
        return _text(f"**{title}** needs you to do it yourself, so I cannot run it. Open it in Work to see what is needed.",
                     _open("Open work", f"/v/{venture_id}/tasks", "primary"))
    if managed:
        how = ["This is a managed build of the exact saved milestone.",
               "Nothing is published by this step."]
    elif re.search(r"landing|web\s*site", title, re.I):
        how = ["Uses the existing approved-plan item; no new task is created.",
               "Builds a private preview for your review. Nothing is published."]
    else:
        how = ["Uses the existing approved-plan item; no new task is created.",
               "Runs it now and saves the result in Work for your review. Nothing is published or sent."]
    if task.get("est_minutes"):
        how.append(f"Estimated effort: about {task['est_minutes']} minutes.")
    card = ConfirmationCard.for_action(get_action("work.start"),
        interpretation=f"Start the planned Work item: {title}.", consequences=how,
        resulting_state={"task_id": task["id"]})
    return _text(f"Your approved plan already has **{title}** in Work. Confirm to start that item. "
                 "Nothing has started or been published.", card.as_action(), kind="confirmation")


def next_task_reply(venture_id: str, actor: str | None = None) -> dict:
    task = next_task(venture_id)
    if task:
        return task_card(venture_id, task, actor)
    if _plans(venture_id, "founder_review"):
        return _text("There is no work to start until you approve the plan.", button("Review and approve", "plan.approve"))
    if _generating(venture_id):
        return _text("The plan is still being generated; work items appear after you approve it.")
    if _plans(venture_id, "approved"):
        return _text("All items from the approved plan are already started or done.", _open("Open work", f"/v/{venture_id}/tasks"))
    from app.ventures.journey_overrides import NOTE, read as journey_choices
    if journey_choices(venture_id)["skipped"]:
        from app.chat.recovery import _auto_card
        return question_reply("No approved work items are saved. Review a free starter checklist, or choose any other step. " + NOTE,
                     [_auto_card(venture_id, "plan_template"), _open("Choose another step", f"/v/{venture_id}")])
    return question_reply("There are no approved work items yet. Prepare a plan, or choose another step.",
                 [button("Prepare plan generation", "plan.generate"), _open("Choose another step", f"/v/{venture_id}")])


_START_NAMED = re.compile(r"^\s*(?:(?:please|ok|now|can you|could you|let'?s|go ahead and)[,\s]+)*"
                          r"(?:start|run|do|begin|kick off|work on)\s+(?:the\s+)?(?:task\s+)?[\"“']?(.{4,80}?)[\"”']?[.!]*$", re.I)


def named_task_card(venture_id: str, text: str, actor: str | None = None) -> dict | None:
    """'start <task title>' for an open approved-plan item; None when nothing matches."""
    m = _START_NAMED.match(text or "")
    if not m:
        return None
    want = m.group(1).lower().strip()
    task = next((t for t in open_tasks(venture_id) if want in t["title"].lower() or t["title"].lower() in want), None)
    return task_card(venture_id, task, actor) if task else None
