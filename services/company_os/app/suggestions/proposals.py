"""What Supymem could take over for a venture right now.

One swap point: when God mode exposes `app.autopilot.actions.propose_next_actions` this module uses it;
until then it derives the same shape from the venture's next step. A proposal is {key, title, why, plan_text, est_credits, action_kind, payload,
requires_mac}. Nothing here runs anything.
"""

from __future__ import annotations

import logging
import json
import math
from typing import Any

from sqlalchemy import func, select

from app.autopilot import guard
from app.credits.units import MICRO_PER_CREDIT

log = logging.getLogger("app.suggestions")

# Never offered as a one-tap takeover: publishing and anything that reaches real people.
BLOCKED_KINDS = frozenset({"site.publish", "social_post", "outreach.send"})
CAPABILITIES = {
    "browser.task.prepare": {"outcome": "Prepare a read-only competitor browser task for review", "executor": "device_orders.prepare_browser_task", "completes_fully": True},
    "idea.answer": {"outcome": "Answer one idea question as an AI assumption", "executor": "lifecycle.answer_clarification", "completes_fully": True},
    "idea.advance": {"outcome": "Run idea validation or confirm the completed validation", "executor": "lifecycle.advance_idea", "completes_fully": True},
    "research.start": {"outcome": "Start a research job for the confirmed idea", "executor": "chat.execute_action", "completes_fully": True},
    "pagebuilder.build": {"outcome": "Build a private landing page preview", "executor": "pagebuilder.api._start", "completes_fully": True},
    "outreach.draft": {"outcome": "Draft 3 outreach emails for review", "executor": "journey.draft_for_review", "completes_fully": True},
    "waitlist.enable": {"outcome": "Enable and test the waitlist", "executor": "site_actions._enable", "completes_fully": True},
    "recovery.plan_template": {"outcome": "Save a starter execution plan for review", "executor": "recovery.create_plan", "completes_fully": True},
}
EXECUTABLE_KINDS = frozenset(k for k, v in CAPABILITIES.items() if v["completes_fully"])
# What the card shows when the step has no quote of its own; a later quote above this stops for the founder.
EST_CREDITS = {"research.start": 20, "gtm.diagnosis": 10, "plan.generate": 5, "outreach.draft": 3,
               "build.prepare": 10, "work.start": 10, "build.app.create": 10, "mac.task.prepare": 2}


def _est(kind: str, payload: dict[str, Any]) -> int:
    micro = guard.cost_micro(kind, payload)
    return round(micro / MICRO_PER_CREDIT) if micro else EST_CREDITS.get(kind, 0)


def _shape(p: dict[str, Any], trusted: bool = False) -> dict[str, Any] | None:
    """`trusted`: God mode's planner already vetted the kind (its floors apply again when it runs)."""
    kind = str(p.get("action_kind") or "")
    from app.core.local_capabilities import surface_visible

    if not surface_visible("actions", kind):
        return None
    # A card is only ever an action the executor will actually run: on the allow-list, never publishing or reaching people.
    # (`trusted` once let any non-human God mode kind through; a kind the guard refuses made a card that failed when tapped.)
    if kind in BLOCKED_KINDS or not p.get("title") or kind not in EXECUTABLE_KINDS:
        return None
    payload = dict(p.get("payload") or {})
    if kind == "browser.task.prepare":
        from app.research.deep import safe_source_url
        url = safe_source_url(str(payload.get("url") or ""))
        name = str(payload.get("name") or "").strip()[:120]
        if not url or not url.startswith("https://") or not name:
            return None
        payload = {"url": url, "name": name}
    if kind == "outreach.draft":
        payload = {"run": True, "review_only": True}
    if kind == "pagebuilder.build":
        if (type(payload.get("ceiling_micro")) is not int or payload["ceiling_micro"] < 0
                or not isinstance(payload.get("quote_fingerprint"), str) or len(payload["quote_fingerprint"]) != 64):
            return None
        payload = {key: payload[key] for key in ("ceiling_micro", "quote_fingerprint")}
    outcome = CAPABILITIES[kind]["outcome"]
    if kind == "browser.task.prepare":
        outcome = f"Review a browser check of {payload['name']}'s pricing and features"
    if kind == "idea.advance":
        outcome = ("Confirm the completed idea validation" if payload.get("idea_phase") == "validating:True"
                   else "Start an idea validation run")
    return {"key": str(p.get("key") or kind), "title": outcome, "why": str(p.get("why") or "")[:300],
            "plan_text": outcome + ". Nothing is published or sent; any later step needs its own approval.",
            "est_credits": (math.ceil(payload["ceiling_micro"] / MICRO_PER_CREDIT) if kind == "pagebuilder.build"
                            else int(p.get("est_credits") or _est(kind, payload))),
            "action_kind": kind, "payload": payload,
            "requires_mac": bool(p.get("requires_mac") or kind in {"mac.task.prepare", "browser.task.prepare"})}


def _from_god(p: Any) -> dict[str, Any] | None:
    """God mode's ProposedAction (app/autopilot/actions.py) in this module's shape; human-only steps are not cards."""
    d = p.as_dict() if hasattr(p, "as_dict") else dict(p)
    if d.get("human_only"):
        return None
    credits = math.ceil(float(d.get("estimated_credits") or 0))
    return _shape({"key": d.get("id"), "title": d.get("title"), "why": d.get("why"), "plan_text": d.get("will_do"),
                   "est_credits": credits or EST_CREDITS.get(str(d.get("kind")), 0), "action_kind": d.get("kind"),
                   "payload": d.get("payload"), "requires_mac": d.get("requires_mac")}, trusted=True)


def god_actions() -> Any:
    """The God mode planner module when it is deployed, else None (the adapter below stands in)."""
    try:
        from app.autopilot import actions

        return actions if hasattr(actions, "propose_next_actions") and hasattr(actions, "execute_proposed_action") else None
    except ImportError:
        return None


def propose_next_actions(venture_id: str, limit: int = 5) -> list[dict[str, Any]]:
    from app.tenancy import bind, current_or_none
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage in {"archived", "purged"}:
            return []
        workspace_id = venture.workspace_id
    scope = current_or_none()
    if scope and scope.workspace_id not in {"none", "", workspace_id}:
        raise PermissionError("suggestion_scope_mismatch")
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=scope.actor if scope else "system"):
        items = _propose_scoped(venture_id, limit)
        from app.ventures.next_step import next_step
        from app.pagebuilder import api as pagebuilder
        from app.suggestions.service import _owner
        step = next_step(venture_id)
        choices = {choice.get("choice") for choice in step.get("alternatives", [])}
        items = [item for item in items if item["action_kind"] != "pagebuilder.build"]
        if step.get("key") == "site.build" and choices == {"build", "skip"}:
            if not any(item["action_kind"] == "outreach.draft" for item in items):
                items.append(_shape({"action_kind": "outreach.draft", "title": "Draft outreach"}))
            try:
                quote = pagebuilder.estimate(venture_id, _owner(venture_id))
                if quote["ready"]:
                    items.insert(0, _shape({"action_kind": "pagebuilder.build", "title": "Private preview",
                        "why": step.get("detail"), "payload": quote}))
            except Exception:  # noqa: BLE001 - unavailable admission is not an executable suggestion
                log.info("suggestions_pagebuilder_unavailable", exc_info=True)
        from app.autopilot.outreach import drafts
        from app.db.models import Job, PlannedTask
        from app.db.supymem_models import Suggestion
        with db()() as session:
            drafted = bool(drafts(venture_id) or session.scalar(select(PlannedTask.id).where(
                PlannedTask.venture_id == venture_id, PlannedTask.workspace_id == workspace_id,
                PlannedTask.title == "Draft the outreach emails for me to review",
                PlannedTask.status.in_(("done", "running"))).limit(1)) or session.scalar(select(Suggestion.id).where(
                Suggestion.venture_id == venture_id, Suggestion.action_kind == "outreach.draft",
                Suggestion.status.in_(("accepted", "executing", "running"))).limit(1)))
            from app.db.supymem_models import IdeaSession
            idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
                IdeaSession.workspace_id == workspace_id, IdeaSession.state != "abandoned")
                .order_by(IdeaSession.created_at.desc(), IdeaSession.id).limit(1))
            building = bool(session.scalar(select(Suggestion.id).where(
                Suggestion.venture_id == venture_id, Suggestion.action_kind == "pagebuilder.build",
                Suggestion.status.in_(("accepted", "executing", "running"))).limit(1)))
            if idea and any(item["action_kind"] == "pagebuilder.build" for item in items):
                page_jobs = session.scalars(select(Job).where(Job.venture_id == venture_id,
                    Job.workspace_id == workspace_id, Job.kind == pagebuilder.KIND)).all()
                building |= any(json.loads(job.payload_json).get("revision_id") == idea.confirmed_revision_id
                    and (job.status not in pagebuilder.FAILED_QUEUE | {"succeeded", "done"}
                         or pagebuilder._view(job)["state"] == "ready") for job in page_jobs)
        if idea and idea.confirmed_revision_id:
            browser = _browser_suggestion(venture_id, workspace_id, idea.confirmed_revision_id)
            if browser:
                items.insert(0, browser)
        for item in items:
            if item["action_kind"] == "research.start" and idea:
                item["payload"]["expected_revision_id"] = idea.confirmed_revision_id
        from app.autopilot.lifecycle import ASSUMED, MAX_ASSUMED_ANSWERS, _option_text
        from app.db.supymem_models import ClarifierTurn
        with db()() as session:
            for item in items:
                if item["action_kind"] != "idea.answer":
                    continue
                question = session.get(ClarifierTurn, item["payload"].get("question_id"))
                if (not question or not idea or question.session_id != idea.id or idea.state == "confirmed"
                        or question.kind != "question" or question.role == "founder"):
                    item["action_kind"] = "human"
                    continue
                options = json.loads(question.options or "[]") if question else []
                if isinstance(options, dict):
                    options = options.get("options") or []
                assumed = session.scalar(select(func.count(ClarifierTurn.id)).where(
                    ClarifierTurn.session_id == question.session_id, ClarifierTurn.role == "founder",
                    ClarifierTurn.text.startswith(ASSUMED))) if question else 0
                if not any(_option_text(option) for option in options) or assumed >= MAX_ASSUMED_ANSWERS:
                    item["action_kind"] = "human"
        items = [item for item in items if item["action_kind"] in EXECUTABLE_KINDS]
        return [p for p in items if not (p["action_kind"] == "outreach.draft" and drafted)
                and not (p["action_kind"] == "pagebuilder.build" and building)][:limit]


def _browser_suggestion(venture_id: str, workspace_id: str, revision_id: str) -> dict | None:
    import os
    from app.build import device_browser_ai, device_orders
    from app.db.supymem_models import ResearchDossier
    from app.research.service import get_dossier
    from app.state import db
    from app.suggestions.service import _owner

    if not device_orders.general_browser_execution_enabled(_owner(venture_id)):
        return None
    try:
        if os.getenv("DEVICE_BROWSER_EXECUTION_ENABLED") != "1":
            return None
        device_browser_ai.assert_configuration(json.loads(os.getenv("DEVICE_BROWSER_CUAI_PROFILE_JSON", "{}")))
        with db()() as session:
            dossier = session.scalar(select(ResearchDossier).where(
                ResearchDossier.venture_id == venture_id, ResearchDossier.workspace_id == workspace_id,
                ResearchDossier.revision_id == revision_id, ResearchDossier.state.in_(("complete", "partial")))
                .order_by(ResearchDossier.created_at.desc(), ResearchDossier.id).limit(1))
        if not dossier:
            return None
        report = get_dossier(dossier.id)["summary"].get("founder_report", {})
        for source in report.get("dimensions", {}).get("competition", {}).get("support", []):
            item = _shape({"key": f"browser.competitor:{source.get('source_id')}",
                "action_kind": "browser.task.prepare", "title": "Review competitor page",
                "why": "Check current pricing and features against your saved research. Acceptance only prepares a review; it never runs the browser.",
                "payload": {"url": source.get("url"), "name": source.get("name")}})
            if item:
                return item
    except (ValueError, KeyError):
        return None
    return None


def _propose_scoped(venture_id: str, limit: int) -> list[dict[str, Any]]:
    god = god_actions()
    if god is not None:
        try:
            return [s for s in (_from_god(p) for p in god.propose_next_actions(venture_id, limit)) if s][:limit]
        except Exception:  # noqa: BLE001 - fall back to the adapter rather than offer nothing
            log.exception("suggestions_god_proposals_failed")
    out: list[dict[str, Any]] = []
    try:
        from app.ventures.next_step import next_step

        step = next_step(venture_id)
        if step.get("chat_action"):
            out.append({"key": str(step["key"]), "title": step["title"], "why": step.get("detail"),
                        "plan_text": step.get("detail") or "I will carry out the proposed action with the approved credit limit.",
                        "action_kind": step["chat_action"], "payload": step.get("payload")})
    except Exception:  # noqa: BLE001
        log.exception("suggestions_next_step_failed")
    return [s for s in (_shape(p) for p in out) if s][:limit]
