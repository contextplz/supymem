"""God mode (beta): keep a venture moving without permission prompts, inside the guard's floors.

A tick reads the venture's next step (ventures/next_step.py) and runs it through the same
chat execute path the confirmation cards use. Cards the step raises are confirmed here, each one
checked by autopilot.guard first and written to the timeline.
"""

from __future__ import annotations

import json
import asyncio
import threading
from datetime import timedelta
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from typing import Any

from sqlalchemy import func, literal, select
from app.connections.composio_pacing import RateLimited
from sqlalchemy.orm import aliased

from app.autopilot import guard, lifecycle, receipts
from app.autopilot.authority import founder_scope
from app.authz.roles import role_allows
from app.credits.units import MICRO_PER_CREDIT
from app.db.models import EventLog, Venture, VentureDocument, Workspace
from app.db.supymem_models import AutopilotEvent, AutopilotSetting, BuildRun
from app.needs_you import hooks as needs_you_hooks
from app.state import db
from app.workspaces.service import role_of
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger("app.autopilot")
# Consequential (spending or irreversible) steps admitted in the current tick; None outside a tick.
_tick_used: ContextVar[list[int] | None] = ContextVar("autopilot_tick_used", default=None)
_forbidden: ContextVar[frozenset[str]] = ContextVar("suggestion_forbidden_kinds", default=frozenset())
_effects: ContextVar[dict | None] = ContextVar("suggestion_effects", default=None)
MAX_STEPS_PER_TICK = 4
MAX_FOLLOW_UPS = 4
# A step the journey keeps offering without progress (the GTM diagnosis never "completes") must not loop and spend.
MAX_REPEATS_PER_DAY = {"gtm.diagnosis": 1, "outreach.draft": 8, "outreach.targets": 3, "outreach.send": 8, "content.drafts": 1,
                       "idea.advance": 12, "idea.answer": 10, "pmf.iterate": 400}  # pmf.iterate is idle most ticks: it measures
# An uncertain outcome (held) is never replayed automatically for actions that spend, publish or reach people. Drafts, reads and
# idempotent steps may be tried again (still at most MAX_FAILED_ATTEMPTS_PER_DAY a day): one unconfirmed draft must not stop that step for good.
NO_REPLAY = frozenset({"outreach.send", "social_post", "site.publish", "build.start", "research.start", "plan.generate",
                       "plan.approve", "mac.approve", "experiment.start"})
# Chat hears about decisions God mode made for the founder, not about every five-minute tick. The timeline records them all.
MEANINGFUL = frozenset({"idea.advance", "plan.approve", "build.start", "build.accept", "site.publish", "mac.approve", "outreach.send",
                        "research.start", "plan.generate", "waitlist.enable"})
DEFAULT_MAX_REPEATS = 6
MAX_FAILED_ATTEMPTS_PER_DAY = 3
WARNING = "Beta: review recorded results. Pausing blocks new work; running work may finish."


def record(venture_id: str, action: str, what: str, why: str = "", credits_micro: int = 0,
           outcome: str = "done", *, dedupe: bool = False, event_id: str | None = None) -> bool:
    """Append a timeline row. dedupe skips a row already written today with the same action, text and outcome
    (not only the newest one: two stuck steps take turns, and used to repeat every tick)."""
    with db()() as s:
        if dedupe and s.scalar(select(AutopilotEvent.id).where(
                AutopilotEvent.venture_id == venture_id, AutopilotEvent.action == action, AutopilotEvent.what == what[:300],
                AutopilotEvent.outcome == outcome, AutopilotEvent.created_at >= guard.day_start()).limit(1)):
            return False
        s.add(AutopilotEvent(id=event_id or new_id("apv"), venture_id=venture_id, action=action, what=what[:300],
                             why=why[:400], credits_micro=credits_micro, outcome=outcome, created_at=utcnow()))
        s.commit()
    return True


def timeline(venture_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with db()() as s:
        rows = s.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == venture_id)
                         .order_by(AutopilotEvent.created_at.desc(), AutopilotEvent.id.desc()).limit(limit)).all()
    return [{"id": r.id, "action": r.action, "what": r.what, "why": r.why, "outcome": r.outcome,
             "credits": r.credits_micro / MICRO_PER_CREDIT, "at": r.created_at.isoformat(),
             "display_outcome": "recorded" if r.outcome == "done" else r.outcome,
             "completion_verified": r.outcome == "completed"} for r in rows]


def public_settings(venture_id: str) -> dict[str, Any]:
    s = guard.get_settings(venture_id)
    return {"god_mode": s["god_mode"], "daily_cap_credits": s["daily_cap_credits"],
            "spent_today_credits": s["spent_today_micro"] / MICRO_PER_CREDIT,
            "allow_send": s["allow_send"], "send_max_per_day": s["configured_send_max_per_day"], "allow_mac": s["allow_mac"],
            "continue_when_offline": s["continue_when_offline"], "lifetime_cap_credits": s["lifetime_cap_credits"],
            "lifetime_spent_credits": s["lifetime_spent_micro"] / MICRO_PER_CREDIT,
            "floors": list(guard.FLOORS), "warning": WARNING,
            "send_recommendation": "15 a day is recommended. You can confirm a higher daily limit."}


def update_settings(venture_id: str, *, god_mode: bool | None = None, daily_cap_credits: int | None = None,
                    allow_send: bool | None = None, send_max_per_day: int | None = None,
                    allow_mac: bool | None = None, continue_when_offline: bool | None = None,
                    confirmed_send_max: bool = False, actor_subject: str | None = None) -> dict[str, Any]:
    """Save safety settings; legacy daily-cap inputs are validated but no longer set limits."""
    if daily_cap_credits is not None and not 0 <= daily_cap_credits <= guard.MAX_CAP_CREDITS:
        raise ValueError("daily_cap_invalid")
    if allow_send is False:
        send_max_per_day = 0
    if send_max_per_day is not None and (type(send_max_per_day) is not int or not 0 <= send_max_per_day <= guard.MAX_SEND_MAX):
        raise ValueError("send_max_invalid")
    with db()() as s:
        ven = s.get(Venture, venture_id)
        if ven is None:
            raise KeyError("venture")
        row = s.get(AutopilotSetting, venture_id)
        if row is None:
            row = AutopilotSetting(venture_id=venture_id, god_mode=False, daily_cap_credits=guard.DEFAULT_CAP_CREDITS,
                                   allow_send=False, send_max_per_day=0, allow_mac=False, updated_at=utcnow())
            s.add(row)
        if send_max_per_day is not None and (confirmed_send_max is True or send_max_per_day > guard.DEFAULT_SEND_MAX):
            if not actor_subject or not role_allows(role_of(ven.workspace_id, actor_subject), "change_autonomy"):
                raise PermissionError("change_autonomy_required")
            if confirmed_send_max is True:
                s.add(EventLog(id=new_id("evt"), topic="autopilot.send_cap.confirmed", venture_id=venture_id,
                    workspace_id=ven.workspace_id, actor=actor_subject, actor_id=actor_subject, actor_kind="person",
                    surface="api", target_ref=venture_id, outcome="ok",
                    payload_json=json.dumps({"send_max_per_day": send_max_per_day}), ts=utcnow()))
        was = row.god_mode
        if god_mode is not None:
            row.god_mode = god_mode
        if allow_mac is not None:
            row.allow_mac = allow_mac
        if continue_when_offline is not None:
            row.continue_when_offline = continue_when_offline
        if allow_send is not None:
            row.allow_send = allow_send
        if send_max_per_day is not None:
            row.send_max_per_day = send_max_per_day
        if not row.allow_send:
            row.send_max_per_day = 0
        elif row.send_max_per_day <= 0 and send_max_per_day is None:
            row.send_max_per_day = guard.DEFAULT_SEND_MAX
        elif row.send_max_per_day <= 0:
            row.allow_send = False  # "Allow sending (max 0/day)" is the same as off
        row.updated_at = utcnow()
        now_on, workspace_id = row.god_mode, ven.workspace_id
        s.commit()
    if now_on != was:
        record(venture_id, "god_mode.on" if now_on else "god_mode.off",
               "God mode turned on" if now_on else "God mode turned off",
               "You chose this." if now_on else (
                   "You chose this. New autonomous steps are disabled. Work already running may finish; "
                   "review its recorded result and credit settlement."), outcome="info")
        try:
            from app.audit.writer import audit

            audit("autopilot.god_mode.changed", workspace_id=workspace_id, venture_id=venture_id, surface="api",
                  target_ref=venture_id, payload={"god_mode": now_on})
        except Exception:  # noqa: BLE001 - the audit trail never blocks the kill switch
            log.exception("autopilot_audit_failed")
    return public_settings(venture_id)


def _owner(venture_id: str) -> str | None:
    with db()() as s:
        ven = s.get(Venture, venture_id)
        ws = s.get(Workspace, ven.workspace_id) if ven and ven.workspace_id else None
        return ws.owner_id if ws else None


def _stopped(venture_id: str) -> str | None:
    """Why this venture must not move right now, else None. Read fresh before every step (kill switch)."""
    if not guard.get_settings(venture_id)["god_mode"]:
        return "god_mode_off"
    from app.ventures.lifecycle import is_paused

    return "paused" if is_paused(venture_id) else None


def _card_followups(reply: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The cards (or card-less buttons) in a chat reply, as (action, payload): what a founder would tap."""
    out = []
    for a in reply.get("actions") or []:
        if not isinstance(a, dict):
            continue
        card = a.get("confirmation")
        if card is not None and not isinstance(card, dict):
            continue
        name = (card or {}).get("action") or a.get("action")
        if not name or a.get("href"):
            continue
        payload = a.get("payload") or (card or {}).get("resulting_state") or {}
        if isinstance(payload, dict):
            out.append((str(name), dict(payload)))
    return out


@dataclass
class _ApprovedCardBudget:
    remaining: int
    parent: _ApprovedCardBudget | None = None

    def admit(self, cost: int) -> bool:
        if type(cost) is not int or cost < 0 or not self._permits(cost):
            return False
        self._consume(cost)
        return True

    def _permits(self, cost: int) -> bool:
        return cost <= self.remaining and (self.parent is None or self.parent._permits(cost))

    def _consume(self, cost: int) -> None:
        self.remaining -= cost
        if self.parent is not None:
            self.parent._consume(cost)


@dataclass(frozen=True)
class _CardBudgetScope:
    venture_id: str
    actor: str
    budget: _ApprovedCardBudget


_card_budget: ContextVar[_CardBudgetScope | None] = ContextVar("approved_suggestion_budget", default=None)


def _inherited_card_budget(venture_id: str, actor: str) -> _ApprovedCardBudget | None:
    scope = _card_budget.get()
    if scope is None:
        return None
    if (scope.venture_id, scope.actor) != (venture_id, actor):
        raise PermissionError("approved_suggestion_scope_mismatch")
    return scope.budget


async def _execute(venture_id: str, name: str, payload: dict[str, Any], why: str, owner: str | None,
                   done: list[str], human: bool = False, *,
                   approved_budget: _ApprovedCardBudget | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Guard, run and log one action. Returns (reply, None) or (None, stop reason).
    human: the founder approved this exact action on a card (Suggestions); the same floors apply."""
    with founder_scope(venture_id, owner, autonomous=not human) as scope:
        # Nested drivers share the same allowance and cannot cross its owner.
        approved_budget = _inherited_card_budget(venture_id, scope.actor) or approved_budget
        with _admission_fence(venture_id, scope.actor) as acquired:
            if not acquired:
                return None, "already_running"
            token = None
            if _card_budget.get() is None and approved_budget is not None:
                token = _card_budget.set(_CardBudgetScope(venture_id, scope.actor, approved_budget))
            try:
                return await _execute_scoped(venture_id, name, payload, why, scope.actor, done, human,
                                             approved_budget=approved_budget)
            finally:
                if token is not None:
                    _card_budget.reset(token)


async def _execute_scoped(venture_id: str, name: str, payload: dict[str, Any], why: str, owner: str | None,
                          done: list[str], human: bool, *,
                          approved_budget: _ApprovedCardBudget | None = None) -> tuple[dict[str, Any] | None, str | None]:
    from app.chat.execute import execute_action
    from app.core.local_mode import is_local

    if name in _forbidden.get():
        return None, "needs_you"
    if "_suggestion_state" in payload:
        from app.suggestions.service import _state_binding

        if payload.pop("_suggestion_state") != _state_binding(venture_id):
            message = "Your saved venture inputs or work changed since this review. Review a fresh suggestion before I continue."
            effects = _effects.get()
            if effects is not None:
                effects["message"] = message
                effects["links"].append(f"/v/{venture_id}/suggestions")
            return {"kind": "needs_you", "body_md": message,
                    "actions": [{"href": f"/v/{venture_id}/suggestions"}]}, "needs_you"

    # Identity is contextual and persisted; a proposal cannot supply it.
    payload = {k: v for k, v in payload.items() if k != "_actor_subject"}
    if human and name == "outreach.send":
        payload = {**payload, "approved_by_founder": True}  # Go ahead on a card that showed this send is the approval
    verdict = guard.check(venture_id, name, payload, human_approved=human)
    if not verdict.ok:
        if verdict.reason == "send_limit_reached":  # the founder already allowed sending: today's max is simply used up
            record(venture_id, name, "Today's send limit is reached", "The rest go out tomorrow, or request God mode access in Settings to raise the max.",
                   outcome="stopped", dedupe=True)
        else:
            record(venture_id, name, f"Left for you: {name}", f"Not allowed on autopilot ({verdict.reason}).",
                   outcome="blocked", dedupe=True)
            needs_you_hooks.from_godmode(venture_id, name, verdict.reason)
        return None, verdict.reason
    local_build = is_local() and name == "build.prepare"
    local_task_build = False
    if is_local() and name == "work.start":
        from app.planning.execute import _is_local_build_task
        from app.planning.store import get_task

        local_task_build = _is_local_build_task(get_task(venture_id, str(payload.get("task_id") or "")))
    if (local_build or local_task_build) and not human:
        return {"kind": "needs_you", "action_started": False,
                "body_md": "Ask me in your venture chat to build or change the specific item you want; no new build has started.",
                "actions": [{"label": "Open your chat", "href": f"/v/{venture_id}/chat"}]}, "needs_you"
    if _unresolved_admission(venture_id, name):
        return None, "held"
    if not human and _repeated(venture_id, name):
        return None, "repeat_limit"
    if name == "mac.approve":
        mac_verdict = _mac_order_verdict(venture_id, payload, human)
        if not mac_verdict.ok:
            reason = mac_verdict.reason
            return None, (reason if reason in {"mac_review_required", lifecycle.MAC_NATIVE_APPROVAL_REQUIRED}
                          else "mac_scope_not_read_only")
    used = _tick_used.get()
    if used is not None and not human and (verdict.cost_micro > 0 or name in NO_REPLAY):
        if used[0] >= guard.MAX_CONSEQUENTIAL_PER_TICK:
            return None, "tick_ceiling"
        used[0] += 1
    if approved_budget is not None and not approved_budget.admit(verdict.cost_micro):
        record(venture_id, name, "Additional work needs a new approval",
               "The aggregate action chain exceeds the credit maximum approved on the suggestion.",
               outcome="blocked")
        return None, "approved_credit_ceiling"
    # A conservative admission reservation also bounds failures/uncertain calls.
    # It is not a provider charge, and is never double-counted on the result row.
    admission_id = new_id("apv")
    terminal_id = _terminal_id(admission_id)
    record(venture_id, name, f"Attempting {name}", "Guarded action admission.",
           verdict.cost_micro, outcome="attempt", event_id=admission_id)
    evidence = None
    try:
        with receipts.collect(venture_id) as evidence:
            if local_build:
                from app.admin.authn import CallerContext
                from app.build import device_orders
                from app.planning.execute import _request_local_coding
                from fastapi import HTTPException

                instruction = payload.get("hint") or payload.get("instruction")
                if not isinstance(instruction, str) or not instruction.strip():
                    reply = {"kind": "needs_you", "action_started": False,
                        "body_md": "Tell me in your venture chat exactly what you want changed on your page, then ask me to build it.", "actions": []}
                else:
                    caller = CallerContext(sub=owner, email="", role="user", path="", method="POST", exp=0, nonce="")
                    try:
                        order = _request_local_coding(venture_id, instruction, caller=caller)
                    except HTTPException as exc:
                        reply = {"kind": "needs_you", "action_started": False,
                            "body_md": (exc.detail.get("message") if isinstance(exc.detail, dict) else None) or
                                "Open Settings > Keys and connections > Coding tools, check your tool connection, then retry your request.",
                            "actions": ([{"label": "Buy more", "href": f"/v/{venture_id}/settings#settings-credits"}]
                                if isinstance(exc.detail, dict) and exc.detail.get("code") == "insufficient_credits" else
                                [{"label": "Connect your coding tool", "href": "/settings/local"}])}
                    else:
                        with db()() as session:
                            document = session.get(VentureDocument, order["id"])
                            if (not document or document.venture_id != venture_id or document.kind != device_orders.KIND
                                    or json.loads(document.provenance_json or "{}").get("state") not in {"approved", "claimed", "waiting_for_device"}):
                                raise RuntimeError("device_order_admission_unverified")
                            receipts.saved_document(document.id, document.kind, document.body_md, provenance=document.provenance_json)
                        reply = {"kind": "text", "action_started": True,
                            "body_md": "Your requested page work is queued with your coding tool; open Build activity to watch or stop it and review its checked result.",
                            "actions": [{"label": "Watch your build", "href": order["review_path"]}]}
            elif name in lifecycle.DRIVERS:
                reply = await lifecycle.run_driver(venture_id, name, payload, owner, human)
            else:
                reply = await execute_action(venture_id, name, payload=payload, actor_subject=owner)
            if (not isinstance(reply, dict) or not isinstance(reply.get("kind", "text"), str)
                    or (reply.get("actions") is not None and not isinstance(reply["actions"], (list, tuple)))):
                raise RuntimeError("action_reply_invalid")
        outcome = evidence.outcome()
        if (local_build or local_task_build) and outcome == "completed":
            outcome = "queued"
        effects = _effects.get()
        if effects is not None:
            effects["message"] = str(reply.get("body_md") or "")
            effects["links"].extend(a["href"] for a in reply.get("actions") or []
                                    if isinstance(a, dict) and isinstance(a.get("href"), str) and a["href"].startswith("/"))
    except Exception as exc:  # noqa: BLE001 - an exception cannot prove absence of effects
        await _failed(venture_id, name, exc, owner, human, terminal_id=terminal_id)
        return None, "held"
    finally:
        effects = _effects.get()
        if effects is not None and evidence is not None:
            effects["jobs"].update(evidence.jobs)
            effects["documents"].extend(identifier for identifier, _, _ in evidence.documents)
            effects["records"].extend({"model": model.__name__, "id": identifier, "expected": expected}
                                      for model, identifier, expected in evidence.persisted)
    has_evidence = bool(evidence.jobs or evidence.documents or evidence.persisted or evidence.failure)
    if has_evidence and (reply.get("kind") in {"wait", "refusal"}
                         or (reply.get("kind") == "needs_you" and outcome == "held")):
        record(venture_id, name, f"{name}: held", "Control reply conflicts with collected effect evidence.",
               outcome="held", event_id=terminal_id)
        _tell(venture_id, f"The outcome of {name} requires review. "
              "Open saved venture status before retrying.")
        return reply, "held"
    if reply.get("kind") == "wait":
        _forget_admission(admission_id)  # nothing happened: no timeline noise, no attempt counted against today's limit
        return reply, "waiting"
    if reply.get("kind") in {"needs_you", "refusal"}:
        record(venture_id, name, f"{name} needs you", "Founder review is required.", outcome="blocked", event_id=terminal_id)
        needs_you_hooks.from_godmode(venture_id, name, "needs_you", card=False)
        return reply, "needs_you"
    if outcome == "held" and not has_evidence and _card_followups(reply):
        outcome = "prepared"
    record(venture_id, name, f"{name}: {outcome}", why, outcome=outcome, event_id=terminal_id)
    if outcome == "completed":
        done.append(name)
        return reply, None
    if outcome == "queued":
        return reply, "queued"
    if outcome == "failed":
        return reply, "failed"
    # Preparation cards are guarded independently and remain usable. Without a
    # followup, hold rather than inventing completion from a text reply.
    if outcome == "prepared":
        return reply, None
    if name in NO_REPLAY:
        _tell_once(venture_id, f"god-held:{name}:{guard.day_start().date()}",
                   f"The outcome of {name} could not be independently confirmed. "
                   f"[Open saved venture status](/v/{venture_id}/now) to review it before retrying.")
    return reply, "held"


# The recovery step each autopilot action maps to (chat/recovery.py STEPS).
RECOVERY_STEP = {"plan.generate": "plan.compile", "research.start": "research.deep", "build.prepare": "build",
                 "build.start": "build", "site.publish": "publish", "outreach.draft": "content.draft",
                 "gtm.diagnosis": "gtm.diagnosis", "job.retry": "idea.validate"}


async def _failed(venture_id: str, name: str, exc: Exception, owner: str | None, human: bool = False,
                  *, terminal_id: str | None = None) -> None:
    """Retain a recoverable durable hold; unknown effects are never replayed automatically."""
    with founder_scope(venture_id, owner, autonomous=not human) as scope:
        await _failed_scoped(venture_id, name, exc, scope.actor, human, terminal_id=terminal_id)


async def _failed_scoped(venture_id: str, name: str, exc: Exception, owner: str, human: bool,
                         *, terminal_id: str | None = None) -> None:
    from app.autopilot.diagnostics import save_failure
    from app.chat import recovery

    save_failure(venture_id, name, exc)
    if not record(venture_id, name, f"{name}: held", "Action outcome requires saved-status review.",
                  outcome="held", dedupe=terminal_id is None, event_id=terminal_id):
        return
    # An arbitrary exception is not a failed-before-effect receipt. Never
    # automatically dispatch another provider action on that assumption.
    try:
        recovery.post_failure(
            venture_id, step=RECOVERY_STEP.get(name, "chat.action"),
            key=f"god-hold:{name}:{guard.day_start().date()}", kind="autopilot_unconfirmed",
            code="action_outcome_unconfirmed",
            body_md=f"The outcome of {name} could not be confirmed. "
                    f"[Open saved venture status](/v/{venture_id}/now) before retrying or choosing another method.")
    except Exception:  # noqa: BLE001 - retain the durable hold if projection fails
        log.warning("autopilot_recovery_failed")


def _mac_order_verdict(venture_id: str, payload: dict[str, Any], human: bool) -> guard.Verdict:
    body = next((b for b in lifecycle.pending_mac_orders(venture_id) if b.get("id") == payload.get("order_id")), None)
    reviewed = payload.get("review_digest")
    blocker = lifecycle.mac_approval_blocker(body) if body else None
    if body and (not isinstance(reviewed, str) or not reviewed or reviewed != body.get("review_digest")):
        verdict = guard.Verdict(False, reason="mac_review_required")
    elif blocker is not None:
        verdict = guard.Verdict(False, reason=blocker["code"])
    else:
        verdict = guard.check_mac_manifest(venture_id, (body or {}).get("manifest") or {}, human_approved=human)
    if not verdict.ok:
        detail = (blocker["body_md"] if verdict.reason == lifecycle.MAC_NATIVE_APPROVAL_REQUIRED
                  else f"Not safe to auto-approve ({verdict.reason}).")
        record(venture_id, "mac.approve", "Left Mac task for you", detail,
               outcome="blocked", dedupe=True)
    return verdict


def _terminal_id(admission_id: str) -> str:
    return "apv_result_" + admission_id


def _unresolved_admission(venture_id: str, name: str) -> bool:
    """A durable attempt needs its exact paired terminal receipt, across restarts.

    Append-only correlation uses source IDs, not chronological ordering. An old,
    foreign, differently scoped, charged or malformed result cannot close it.
    """
    terminal = aliased(AutopilotEvent)
    terminal_exists = select(terminal.id).where(
        terminal.id == literal("apv_result_") + AutopilotEvent.id,
        terminal.venture_id == AutopilotEvent.venture_id,
        terminal.action == AutopilotEvent.action,
        terminal.credits_micro == 0,
        terminal.outcome.in_(["prepared", "queued", "completed", "failed", "blocked"]),
    ).correlate(AutopilotEvent).exists()
    with db()() as session:
        return session.scalar(select(AutopilotEvent.id).where(
            AutopilotEvent.venture_id == venture_id, AutopilotEvent.action == name,
            AutopilotEvent.outcome == "attempt", ~terminal_exists).limit(1)) is not None


def _repeated(venture_id: str, name: str) -> bool:
    if _unresolved_admission(venture_id, name):
        return True
    with db()() as s:
        latest = s.scalars(select(AutopilotEvent).where(
            AutopilotEvent.venture_id == venture_id, AutopilotEvent.action == name,
            AutopilotEvent.outcome.in_(["held", "completed", "queued", "failed", "done"]))
            .order_by(AutopilotEvent.created_at.desc(), AutopilotEvent.id.desc()).limit(1)).first()
        if latest and latest.outcome == "held" and name in NO_REPLAY:
            return True  # UTC rollover cannot silently replay an uncertain effect.
        base = (AutopilotEvent.venture_id == venture_id, AutopilotEvent.action == name,
                AutopilotEvent.created_at >= guard.day_start())
        n = s.scalar(select(func.count(AutopilotEvent.id)).where(
            *base, AutopilotEvent.outcome.in_(["done", "attempt"]))) or 0
        failures = s.scalar(select(func.count(AutopilotEvent.id)).where(
            *base, AutopilotEvent.outcome.in_(["failed", "held"]))) or 0
    return failures >= MAX_FAILED_ATTEMPTS_PER_DAY or n >= MAX_REPEATS_PER_DAY.get(name, DEFAULT_MAX_REPEATS)


async def _run_step(venture_id: str, step: dict[str, Any], owner: str | None, done: list[str],
                    human: bool = False, *, never: frozenset[str] = frozenset(),
                    approved_ceiling_micro: int | None = None) -> str | None:
    """Run a next step, then keep confirming the cards it raises (quote, then start). Returns a stop reason."""
    if approved_ceiling_micro is not None and (type(approved_ceiling_micro) is not int or approved_ceiling_micro < 0):
        return "approved_credit_ceiling"
    with founder_scope(venture_id, owner, autonomous=not human) as scope:
        never = never | _forbidden.get()
        fence_token = _forbidden.set(never)
        budget = _inherited_card_budget(venture_id, scope.actor)
        token = None
        if approved_ceiling_micro is not None:
            # An explicit child approval may narrow the inherited maximum,
            # never replace or expand it. Every admission debits both.
            budget = _ApprovedCardBudget(approved_ceiling_micro, parent=budget)
            token = _card_budget.set(_CardBudgetScope(venture_id, scope.actor, budget))
        try:
            return await _run_step_with_budget(venture_id, step, scope.actor, done, human, never=never, budget=budget)
        finally:
            _forbidden.reset(fence_token)
            if token is not None:
                _card_budget.reset(token)


async def _run_step_with_budget(venture_id: str, step: dict[str, Any], owner: str | None, done: list[str],
                                human: bool, *, never: frozenset[str], budget: _ApprovedCardBudget | None) -> str | None:
    name, payload = str(step["chat_action"]), dict(step.get("payload") or {})
    why = f"Next step: {step['title']}"
    if not human and _repeated(venture_id, name):
        record(venture_id, f"wait:{step.get('key')}", f"Waiting: {step['title']}",
               "Today’s guarded attempt limit was reached without confirmed progress; review saved status before retrying.",
               outcome="info", dedupe=True)
        return "repeat_limit"
    seen = {(name, json.dumps(payload, sort_keys=True, default=str))}
    for _ in range(MAX_FOLLOW_UPS + 1):
        reply, stop = await _execute(venture_id, name, payload, why, owner, done, human, approved_budget=budget)
        if not human and (stopped := _stopped(venture_id)):
            return stopped
        if stop:
            return stop
        if _effects.get() is not None:
            return None
        nxt = next(((n, p) for n, p in _card_followups(reply or {}) if n in guard.AUTO_ACTIONS and n not in never
                    and (n, json.dumps(p, sort_keys=True, default=str)) not in seen), None)
        if nxt is None:
            return None
        name, payload = nxt
        seen.add((name, json.dumps(payload, sort_keys=True, default=str)))
    return None


def _accept_passed_builds(venture_id: str, owner: str | None, done: list[str]) -> None:
    """Surface finished QA; structural checks cannot authorize content acceptance.

    A source-owned independently qualified content-review receipt is not yet
    available for this lifecycle. Never impersonate founder review on QA alone.
    """
    with db()() as s:
        runs = [(r.id, json.loads(r.spec or "{}")) for r in s.scalars(select(BuildRun).where(
            BuildRun.venture_id == venture_id, BuildRun.runtime_version == 1, BuildRun.state == "succeeded",
            BuildRun.closed_at.is_not(None))).all()]
    for _run_id, spec in runs:
        review = (spec.get("lifecycle") or {}).get("review") or {}
        if spec.get("qa_state") != "passed" or review.get("status") != "pending" or not owner:
            continue
        if _stopped(venture_id):
            return
        record(venture_id, "build.review", "Build ready for content review",
               "Independent QA passed. Compare the private result with the approved milestone before accepting it.",
               outcome="prepared", dedupe=True)


async def _approve_safe_mac_orders(venture_id: str, owner: str | None, done: list[str]) -> None:
    """Approve pending Mac reviews inside the floors (read-only; more with Allow Mac actions)."""
    for mv in lifecycle.mac_moves(venture_id):
        if _stopped(venture_id):
            return
        if mv.human:
            needs_human(venture_id, mv)
            continue
        _, stop = await _execute(venture_id, mv.action, mv.payload, "Reviewed Mac task, inside the floors.", owner, done)
        if stop == "failed":
            continue


def _announce(venture_id: str, done: list[str]) -> None:
    """Consolidate verified progress in one chat card per venture per day; the timeline keeps every step."""
    if not any(name in MEANINGFUL for name in done):
        return
    day = guard.day_start()
    with db()() as session:
        rows = session.scalars(select(AutopilotEvent.what).where(
            AutopilotEvent.venture_id == venture_id, AutopilotEvent.action.in_(MEANINGFUL),
            AutopilotEvent.outcome == "completed", AutopilotEvent.created_at >= day)
            .order_by(AutopilotEvent.created_at, AutopilotEvent.id)).all()
    if not rows:
        return
    from app.chat.milestones import bump_milestone, post_milestone

    key = f"god-update:{day.date()}"
    body = f"Autopilot update: {'; '.join(dict.fromkeys(rows))}."
    if not post_milestone(venture_id, key=key, kind="autopilot_update", body_md=body):
        try:
            bump_milestone(venture_id, key=key, body_md=body)
        except Exception:
            log.exception("autopilot_chat_failed")


def _forget_admission(admission_id: str) -> None:
    with db()() as s:
        row = s.get(AutopilotEvent, admission_id)
        if row is not None and row.outcome == "attempt":
            s.delete(row)
            s.commit()


def _tell_once(venture_id: str, key: str, text: str) -> None:
    """A chat note that is said once per key (the same reason is not repeated every five minutes)."""
    from app.chat.milestones import post_milestone

    post_milestone(venture_id, key=key, kind="autopilot_note", body_md=text)


def _tell(venture_id: str, text: str) -> None:
    import hashlib
    from app.chat.milestones import post_milestone

    try:
        post_milestone(venture_id, key=f"god-review:{guard.day_start().date()}:{hashlib.sha256(text.encode()).hexdigest()}",
            kind="autopilot_note", body_md=text, actions=[{"label": "Review saved status", "href": f"/v/{venture_id}/now"}])
    except Exception:  # noqa: BLE001 - a chat hiccup never undoes work
        log.exception("autopilot_chat_failed")


@contextmanager
def _tick_fence(venture_id: str):
    """One active pass per venture without retaining a database connection."""
    from app.autopilot.send_admission import try_send_admission
    with try_send_admission("autopilot-tick:" + venture_id) as acquired:
        yield acquired


@dataclass
class _AdmissionLease:
    venture_id: str
    actor: str
    thread: int
    task: object
    active: bool = True


_ADMISSION_LEASE: ContextVar[_AdmissionLease | None] = ContextVar("autopilot_admission_lease", default=None)


@contextmanager
def _admission_fence(venture_id: str, actor: str):
    """Only the exact validated actor and owning task may reuse a live lease.

    ContextVar copies in child tasks/threads are not ownership. The underlying
    token fence also excludes other processes and reviewed-card callers.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    thread = threading.get_ident()
    lease = _ADMISSION_LEASE.get()
    if (lease is not None and lease.active and lease.venture_id == venture_id
            and lease.actor == actor and lease.thread == thread and lease.task is task):
        yield True
        return
    with _tick_fence(venture_id) as acquired:
        if not acquired:
            yield False
            return
        owned = _AdmissionLease(venture_id, actor, thread, task)
        token = _ADMISSION_LEASE.set(owned)
        try:
            yield True
        finally:
            owned.active = False
            _ADMISSION_LEASE.reset(token)


async def tick(venture_id: str) -> dict[str, Any]:
    """Do not admit or account a concurrent pass for the same venture."""
    with founder_scope(venture_id, autonomous=True) as scope:
        with _admission_fence(venture_id, scope.actor) as acquired:
            if not acquired:
                return {"status": "already_running", "done": []}
            token = _tick_used.set([0])
            try:
                return await _tick(venture_id)
            finally:
                _tick_used.reset(token)


async def pmf_iterate(venture_id: str, owner: str | None, human: bool = False) -> dict[str, Any]:
    """One guarded, serialized pass of the PMF loop."""
    with founder_scope(venture_id, owner, autonomous=not human) as scope:
        with _admission_fence(venture_id, scope.actor) as acquired:
            if not acquired:
                return {"kind": "wait", "body_md": "An owned pass is already running.", "actions": []}
            return await _pmf_iterate_scoped(venture_id, scope.actor, human)


async def _pmf_iterate_scoped(venture_id: str, owner: str, human: bool) -> dict[str, Any]:
    from app.autopilot import pmf
    from app.db.models import Venture

    async def run_action(name: str, payload: dict[str, Any]) -> bool:
        if name in _forbidden.get():
            return False
        if name == "build.prepare":
            before = len(done)
            stop = await _run_step(venture_id, {"key": "pmf.page", "title": "Landing page experiment", "chat_action": name,
                                                "payload": payload}, owner, done, human, never=_forbidden.get())
            # "queued": the quoted build was admitted. Treating that as a failure re-quoted and re-started a build every tick.
            return stop == "queued" or (stop is None and len(done) > before)
        before = len(done)
        _, stop = await _execute(venture_id, name, payload, "PMF experiment", owner, done, human)
        return stop == "queued" or (stop is None and len(done) > before)  # queued = admitted; do not run it again next tick

    done: list[str] = []
    with db()() as s:
        idea = (s.get(Venture, venture_id).raw_idea or "")
    from app.ventures.state_brief import _website_section

    with db()() as s:
        ws = s.get(Venture, venture_id).workspace_id
    live = bool(_website_section(ws, venture_id)["live"])
    changed = await pmf.advance(venture_id, run_action,
                               allow_send=guard.get_settings(venture_id)["allow_send"] and "outreach.send" not in _forbidden.get(),
                               idea=idea, live=live)
    from app.chat.milestones import post_milestone

    for it in changed:
        with db()() as session:
            journal = session.get(VentureDocument, it["doc_id"])
            if not journal or journal.venture_id != venture_id or journal.kind != pmf.KIND:
                continue
            receipts.saved_document(journal.id, journal.kind, journal.body_md)
        if it["state"] == "running":
            record(venture_id, "pmf.start", f"Started experiment #{it['n']}: {it['variable']}", pmf.describe(it), outcome="queued")
            continue
        record(venture_id, f"pmf.{it['decision']}", f"Experiment #{it['n']} ({it['variable']}): {it['decision']}", pmf.describe(it), outcome="completed")
        post_milestone(venture_id, key=f"pmf:{it['doc_id']}:{it['decision']}", kind="pmf_decision",
                       body_md=f"**PMF journal.** {pmf.describe(it)}")
    waiting = [i for i in pmf.journal(venture_id) if i["state"] == "running" and i.get("waiting_send")]
    if waiting:
        needs_human(venture_id, lifecycle.Move("pmf.send", "Review and send the outreach drafts",
                    "An experiment is waiting on real replies. I never send for you unless you turn on Allow sending.",
                    "", None, human_href=f"/v/{venture_id}/now"))
    return {"kind": "text", "body_md": f"PMF loop: {len(changed)} change(s). " + "; ".join(pmf.describe(i) for i in changed)[:600],
            "actions": [], "citations": []} if changed else {"kind": "wait", "body_md": "No experiment is due.", "actions": []}


def clear_stale_asks(venture_id: str, current_key: str) -> None:
    """Close the card for something that no longer needs the founder: the journey moved past the step, a legacy cap or
    consent that stopped an action has been lifted, or the drafts were sent. A card that outlives its blocker is a lie."""
    from app.needs_you import service as ny

    for r in ny.list_open(venture_id):
        ref = str(r.get("blocking_ref") or "")
        if not ref.startswith("godmode:"):
            continue
        if ref.startswith("godmode:human:"):
            key = ref[len("godmode:human:"):]
            if key == "pmf.send":
                from app.autopilot import pmf

                if any(i["state"] == "running" and i.get("waiting_send") for i in pmf.journal(venture_id)):
                    continue
            elif key.startswith("mac.") and r.get("reason_code") == lifecycle.MAC_NATIVE_APPROVAL_REQUIRED:
                if not lifecycle.native_mac_review_retired(venture_id, key[len("mac."):]):
                    continue
            elif key == current_key:
                continue
        else:
            name = ref[len("godmode:"):]
            if name.startswith("bigquote:") or name == "cap":
                pass
            elif name in guard.AUTO_ACTIONS or name in guard.SEND_ACTIONS:
                if not guard.check(venture_id, name, {}).ok:
                    continue
            else:
                continue
        ny.resolve(r["id"], venture_id=venture_id, actor="system")


def needs_human(venture_id: str, mv: lifecycle.Move) -> None:
    """Ask the founder for the one thing only a person can do. One open card per task (the needs-you service dedupes
    it, and reminds); the timeline shows it once a day. Other work goes on."""
    from app.needs_you.policy import Blocker, escalate

    if mv.key.startswith("mac."):
        current = lifecycle.native_mac_review_move(venture_id, mv.key[len("mac."):])
        if current is None or not mv.human or (mv.payload, mv.human_href) != (current.payload, current.human_href):
            return
        from app.needs_you import service as ny
        ny.request_human(venture_id, reason_code=lifecycle.MAC_NATIVE_APPROVAL_REQUIRED, title=current.title,
            what_we_need=current.why, how_to_resume=current.will_do, blocking_ref=f"godmode:human:{current.key}")
        record(venture_id, f"human:{mv.key}", f"Needs you: {current.title}", current.why, outcome="needs_you", dedupe=True)
        return
    escalate(venture_id, Blocker("real_world_action", f"godmode:human:{mv.key}", other_work_available=True),
             title=mv.title, what_we_need=mv.why or mv.will_do,
             how_to_resume=f"Do it ({mv.human_href or 'Now page'}), then tap Continue. I am carrying on with everything else.")
    record(venture_id, f"human:{mv.key}", f"Needs you: {mv.title}", mv.why or mv.will_do, outcome="needs_you", dedupe=True)


async def _tick(venture_id: str) -> dict[str, Any]:
    """One autopilot pass for one venture. Safe to call when God mode is off (it does nothing).

    Walk the journey as far as it will go. When the primary step needs a person (or is still running),
    ask once and run the parallel branches instead of waiting."""
    if (reason := _stopped(venture_id)):
        return {"status": reason, "done": []}
    from app.ventures.next_step import next_step

    owner, done = _owner(venture_id), []
    with founder_scope(venture_id, owner, autonomous=True):
        _accept_passed_builds(venture_id, owner, done)
    await _approve_safe_mac_orders(venture_id, owner, done)
    stop, blocked = None, False
    for _ in range(MAX_STEPS_PER_TICK):
        if (stop := _stopped(venture_id)):
            break
        step = next_step(venture_id)
        mv = lifecycle._step_move(step, venture_id)
        clear_stale_asks(venture_id, str(step.get("key")))
        if mv.human:
            needs_human(venture_id, mv)
            blocked = True
            break
        if mv.wait:
            record(venture_id, f"wait:{step.get('key')}", f"Waiting: {step.get('title')}", str(step.get("detail") or ""),
                   outcome="info", dedupe=True)
            blocked = True
            break
        run = {"key": step.get("key"), "title": mv.title, "chat_action": mv.action, "payload": mv.payload}
        if mv.action == "site.publish":
            from app.ventures.state_brief import _website_section
            from app.tenancy import current

            if not _website_section(current().workspace_id, venture_id).get("waitlist_enabled"):
                run = {"key": "waitlist.enable", "title": "Enable and test the waitlist",
                       "chat_action": "waitlist.enable", "payload": {}}
        if (stop := await _run_step(venture_id, run, owner, done)):
            blocked = stop in {"waiting", "needs_you", "repeat_limit", "held", "queued"}
            break
    if not _stopped(venture_id):  # human-blocked or waiting: everything else goes on
        for br in lifecycle.branches(venture_id):
            await _run_step(venture_id, {"key": br.key, "title": br.title, "chat_action": br.action,
                                         "payload": br.payload}, owner, done)
        stop = stop or ("waiting" if blocked else None)
    if not _stopped(venture_id):
        from app.autopilot import outreach

        with founder_scope(venture_id, owner, autonomous=True):
            await outreach.housekeeping(venture_id)  # follow-ups, reply tracking, the batch-approval card
    _announce(venture_id, done)
    return {"status": stop or "ran", "done": done}


def status(venture_id: str) -> dict[str, Any]:
    """Live "Supymem is working on: ..." line for the cockpit and chat."""
    s = guard.get_settings(venture_id)
    if not s["god_mode"]:
        return {"state": "off", "working_on": "", "waiting_on_you": [], "last_at": None}
    from app.ventures.lifecycle import is_paused

    if is_paused(venture_id):
        return {"state": "paused", "working_on": "Paused. Nothing new starts until you resume.", "waiting_on_you": [], "last_at": None}
    if guard.mac_offline(venture_id, _owner(venture_id)):
        return {"state": "mac_offline", "working_on": guard.OFFLINE_STATUS, "waiting_on_you": [], "last_at": None}
    events = timeline(venture_id, 20)
    last = next((e for e in events if e["outcome"] == "completed"), None)
    try:
        mv = lifecycle.primary_move(venture_id)
    except Exception:  # noqa: BLE001
        mv = None
    waiting = [{"title": m.title, "why": m.why, "href": m.human_href} for m in ([mv] if mv else []) if m.human]
    try:
        from app.ventures.run_status import snapshot

        with db()() as session:
            venture = session.get(Venture, venture_id)
            workspace_id = venture.workspace_id if venture else None
        if not workspace_id:
            raise ValueError("venture_scope_unavailable")
        observed = snapshot(venture_id, workspace_id)
        state = str(observed.get("state") or "unconfirmed")
        line = str(observed.get("activity") or "Current activity could not be confirmed.")
        if state == "idle" and waiting:
            state, line = "waiting_on_you", f"Waiting on you: {waiting[0]['title']}."
        elif state == "idle" and mv:
            line = f"Next: {mv.title}. No work is in progress."
    except Exception:  # noqa: BLE001
        state, line = "unconfirmed", "Current activity could not be confirmed."
    return {"state": state, "working_on": line, "waiting_on_you": waiting, "last_at": last["at"] if last else None,
            "last_done": last["what"] if last else None, "limit_reached": False}


def daily_summary(venture_id: str) -> bool:
    """Once a day (UTC evening) tell the founder what God mode did. Returns True when it posted."""
    now = utcnow()
    if now.hour < 17:
        return False
    since = now - timedelta(hours=24)
    rows = [e for e in timeline(venture_id, 200) if e["at"] >= since.isoformat()]
    from app.autopilot import brief as _brief

    if not _brief.recent(rows):  # nothing worth saying: stay quiet rather than post an empty summary
        return False
    from app.autopilot import brief

    body = brief.daily_note(venture_id, rows)
    from app.chat.milestones import post_milestone

    return post_milestone(venture_id, key=f"god-daily:{now.date()}", kind="autopilot_summary", body_md=body)


async def tick_all(founder_id=None) -> int:
    from app.db.models import Workspace
    owned = select(Venture.id).join(Workspace, Venture.workspace_id == Workspace.id).where(
        Workspace.owner_id == founder_id, Workspace.archived_at.is_(None), Venture.archived_at.is_(None))
    with db()() as s:
        query = select(AutopilotSetting).where(AutopilotSetting.god_mode.is_(True))
        if founder_id:
            query = query.where(AutopilotSetting.venture_id.in_(owned))
        ids = [r.venture_id for r in s.scalars(query).all()]
    from app.autopilot import outreach

    for vid in ids:
        try:
            if guard.mac_offline(vid, _owner(vid)):
                continue  # the founder's Mac is silent: wait for it unless they chose to keep working
            await tick(vid)
            with founder_scope(vid, autonomous=True):
                daily_summary(vid)
        except RateLimited:
            raise
        except Exception:  # noqa: BLE001 - one venture never blocks the rest
            log.exception("autopilot_tick_failed", extra={"venture_id": vid})
    # Replies to outreach the founder approved by hand still come back with God mode off.
    with db()() as s:
        query = select(VentureDocument.venture_id).where(VentureDocument.kind == outreach.KIND).distinct()
        if founder_id:
            query = query.where(VentureDocument.venture_id.in_(owned))
        others = [v for v in s.scalars(query).all() if v not in ids]
    for vid in others[:50]:
        try:
            with founder_scope(vid, autonomous=True):
                await outreach.poll_replies(vid)
        except Exception:  # noqa: BLE001
            log.exception("outreach_poll_failed", extra={"venture_id": vid})
    return len(ids)
