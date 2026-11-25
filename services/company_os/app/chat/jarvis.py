"""Chat control for the golden path: caps, builds, batches, credits, status, navigation.

Every reply here reads or writes through the SAME service function the console uses
(autopilot.service.update_settings, builder.execution.review, autopilot.outreach, run_status.snapshot,
needs_you.service, payments_api.record_credit_request). Anything that spends, sends or is irreversible
returns a ConfirmationCard; nothing here acts silently. Typed, voice and Hinglish phrasings share one classifier.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from sqlalchemy import func, select

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard
from app.core.local_mode import is_local

_N = r"(\d{1,6})"
_DAY = r"(?:/\s*day|(?:a|per|each|every|ek|roz|rozana)\s+(?:day|din))"
_CAP_RE = re.compile(
    rf"^\s*(?:please\s+)?(?:set|change|make|update|put|lower|raise|limit|cap)\s+(?:my\s+|the\s+|our\s+)?(?:(?:daily|day|spend(?:ing)?)\s+)+(?:cap|limit|budget|ceiling)\s*(?:to|at|=|as)?\s*{_N}\b"
    rf"|^\s*(?:please\s+)?(?:limit|cap)\s+(?:my\s+|our\s+)?spend(?:ing)?\s+(?:to|at)\s*{_N}\b"
    rf"|^\s*(?:daily|day)\s+(?:cap|limit|budget)\s*(?:to|at|=)?\s*{_N}\b", re.I)
_SEND_CAP_RE = re.compile(
    rf"^\s*(?:please\s+)?(?:allow|let|permit|enable)\s+(?:you\s+to\s+|charlie\s+to\s+|him\s+to\s+)?send(?:ing)?\s+(?:up\s+to\s+|upto\s+|max(?:imum)?\s+|at\s+most\s+)?{_N}"
    rf"(?:\s+(?:emails?|messages?|mails?|outreach))?\s*{_DAY}?\s*[.!]*\s*$"
    rf"|^\s*(?:please\s+)?(?:set|change|make|update)\s+(?:my\s+|the\s+|our\s+)?(?:send|sending|email|outreach)\s+(?:cap|limit|max(?:imum)?)\s*(?:to|at|=)?\s*{_N}\b", re.I)
_BUILD_STATUS_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:show|check|give)(?:\s+me)?|what(?:'?s|\s+is)|how(?:'?s|\s+is)|whats|tell\s+me)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?:landing\s*page\s+|site\s+|website\s+|latest\s+|current\s+)?build(?:ing)?(?:\s+(?:status|progress|going|doing|ho\s+raha))?\s*[?.!]*\s*$"
    r"|^\s*build\s+(?:status|progress)\s*[?.!]*\s*$|^\s*(?:build|landing\s*page)\s+(?:ka\s+)?(?:status|progress)\b", re.I)
_BUILD_ACCEPT_RE = re.compile(
    r"^\s*(?:please\s+)?(?:accept|approve|sign\s*off(?:\s+on)?|looks\s+good,?\s+accept|ok(?:ay)?\s+accept)\s+"
    r"(?:the\s+|my\s+|this\s+|that\s+|latest\s+)*(?:landing\s*page\s+|site\s+|website\s+|milestone\s+)*(?:build|milestone|result|deliverable|site|landing\s*page)\s*[.!]*\s*$", re.I)
_BATCH_RE = re.compile(
    r"^\s*(?:please\s+)?(?:approve|okay|ok|ship|send|confirm|go\s+ahead\s+with)\s+(?:this|the|my|that|these|all)\s+(?:outreach\s+|email\s+|draft\s+)?(?:batch|drafts)\s*[.!]*\s*$", re.I)
_DRAFTS_RE = re.compile(
    r"^\s*(?:please\s+)?(?:show|list|open|see|review|dikhao)(?:\s+me)?\s+(?:my\s+|the\s+|our\s+)?(?:outreach\s+|email\s+|saved\s+)?drafts?\s*[.!?]*\s*$", re.I)
_DRAFT_OUTREACH_RE = re.compile(
    r"^\s*(?:please\s+)?(?:draft|write|prepare|create|make)\s+(?:me\s+)?(?:some\s+|an?\s+|the\s+)?(?:cold\s+)?outreach(?:\s+(?:emails?|messages?|mails?))?(?:\s+(?:to|for)\b.*)?\s*[.!]*\s*$", re.I)
_SPEND_RE = re.compile(
    r"\b(?:what|how\s+much)\b[^.?!]{0,30}\b(?:did|have|has)\s+(?:i|we|you)\b[^.?!]{0,15}\b(?:spen[dt]|used|burn(?:ed|t)?)\b"
    r"|\b(?:spen[dt]|spending|usage|credits?\s+used)\s+today\b|\btoday'?s?\s+(?:spend|spending|usage)\b"
    r"|\baaj\s+kitna\s+(?:kharch|spend|credit)", re.I)
_CREDIT_BAL_HI_RE = re.compile(r"\b(?:kitn[ae]|kitni)\s+credits?\b|\bcredits?\s+kitn[ae]\b|\bmera\s+credit\b|\bcredits?\s+(?:bache|bacha|balance)\b", re.I)
_CREDIT_REQ_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:request|ask\s+for|get|give\s+me|i\s+(?:need|want)|can\s+i\s+(?:get|have))\s+(?:some\s+|more\s+|extra\s+)?credits?|"
    r"(?:more\s+|aur\s+)?credits?\s+(?:chahiye|do|dedo|bhejo))\s*[.!?]*\s*$", re.I)
_NEW_CHAT_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:start|open|begin|create|make)\s+(?:a\s+)?)?(?:new|fresh|naya|nayi)\s+(?:chat|conversation|thread)\s*[.!]*\s*$", re.I)
_ACTIVITY_RE = re.compile(
    r"^\s*(?:so\s+|ok(?:ay)?\s+)?(?:what\s+(?:are|r)\s+(?:you|u)\s+(?:doing|working\s+on)(?:\s+(?:right\s+)?now)?|what(?:'?s|\s+is)\s+(?:happening|going\s+on)(?:\s+right\s+now)?|"
    r"what\s+are\s+you\s+up\s+to|kya\s+kar\s+rahe\s+ho(?:\s+abhi)?|abhi\s+kya\s+(?:chal|ho)\s+raha(?:\s+hai)?|status\s+kya\s+hai)\s*[?.!]*\s*$", re.I)
_BLOCKED_RE = re.compile(
    r"^\s*(?:so\s+|ok(?:ay)?\s+)?(?:what(?:'?s|\s+is)\s+(?:blocked|stuck|blocking(?:\s+(?:us|you|me))?)|what\s+do\s+you\s+need\s+(?:from\s+me)?|what\s+(?:is|are)\s+(?:waiting|pending)\s+(?:on|for)\s+me|"
    r"what\s+needs\s+(?:me|my\s+attention)|what\s+(?:should|do)\s+i\s+(?:do|approve)(?:\s+next)?|mujhse\s+kya\s+chahiye|kya\s+(?:atka|ruka)\s+hai)\s*[?.!]*\s*$", re.I)
_HI_GOD_RE = re.compile(r"^\s*(?:please\s+)?go[dr]\s*-?\s*mode\s+(on|off|chalu|band|bandh|start|stop)(?:\s+(?:kr|kar)(?:\s*do|\s*dena)?)?\s*[.!]*\s*$", re.I)
_HI_STOP_RE = re.compile(r"^\s*(?:please\s+)?(?:sab(?:\s+kuch)?\s+(?:rok|band)(?:\s*do|\s*dena)?|rok\s+do|everything\s+(?:rok|band)\s+(?:do|kr|kar))\s*[.!]*\s*$", re.I)
_HI_LANDING_RE = re.compile(r"^\s*(?:please\s+)?(?:meri\s+|mera\s+)?landing\s*page\s+(?:bana(?:o|\s*do|\s*dena)?|build\s+(?:kr|kar)(?:\s*do)?)\s*[.!]*\s*$", re.I)
_HI_RESEARCH_RE = re.compile(r"^\s*(?:please\s+)?(?:meri\s+)?research\s+(?:shuru|start)\s+(?:kar|kr)(?:o|\s*do)?\s*[.!]*\s*$", re.I)


def classify(raw: str) -> str | None:
    """Intent name for the golden-path phrases handled here, else None (the legacy classifier continues)."""
    if re.fullmatch(r"\s*(?:please\s+)?pressure[- ]test\s+(?:my|our|this|the)\s+idea[.!?]*\s*", raw, re.I):
        return "idea.pressure_test.prepare"
    if re.fullmatch(r"\s*(?:please\s+)?(?:show|review)(?:\s+me)?\s+(?:(?:my|our|the)\s+)?competitor\s+grid[.!?]*\s*", raw, re.I):
        return "research.competitors"
    if re.fullmatch(r"\s*(?:please\s+)?resume\s+(?:email\s+)?sending\s*[.!?]*\s*", raw, re.I):
        return "jarvis.resume_sending"
    if _CAP_RE.match(raw):
        return "jarvis.cap_daily"
    if _SEND_CAP_RE.match(raw):
        return "jarvis.cap_send"
    if _BUILD_ACCEPT_RE.match(raw):
        return "jarvis.build_accept"
    if _BUILD_STATUS_RE.match(raw):
        return "jarvis.build_status"
    if _BATCH_RE.match(raw):
        return "jarvis.batch_approve"
    if _DRAFTS_RE.match(raw):
        return "outreach.drafts"
    if _DRAFT_OUTREACH_RE.match(raw):
        return "outreach_draft"
    if _CREDIT_REQ_RE.match(raw):
        return "jarvis.credits_request"
    if _SPEND_RE.search(raw):
        return "jarvis.spend_today"
    if _CREDIT_BAL_HI_RE.search(raw):
        return "credits.balance"
    if _NEW_CHAT_RE.match(raw):
        return "jarvis.new_chat"
    if _BLOCKED_RE.match(raw):
        return "jarvis.blocked"
    if _ACTIVITY_RE.match(raw):
        return "jarvis.activity"
    if (god := _HI_GOD_RE.match(raw)):
        return "god_on" if god.group(1).lower() in {"on", "chalu", "start"} else "god_off"
    if _HI_STOP_RE.match(raw):
        return "pause"
    if _HI_LANDING_RE.match(raw):
        return "start_product_build"
    if _HI_RESEARCH_RE.match(raw):
        return "research"
    return None


def _first_int(text: str) -> int | None:
    m = _CAP_RE.match(text) or _SEND_CAP_RE.match(text)
    return int(next(g for g in m.groups() if g)) if m else None


def _reply(body: str, actions: list | None = None, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": []}


def _nav(label: str, href: str, style: str = "primary") -> dict[str, str]:
    return {"label": label, "href": href, "style": style}


def _card(name: str, *, interpretation: str, consequences: list[str], state: dict[str, Any], label: str) -> dict[str, Any]:
    return replace(ConfirmationCard.for_action(get_action(name), interpretation=interpretation,
                                               consequences=consequences, resulting_state=state), confirm_label=label).as_action()


def _workspace_id(venture_id: str) -> str | None:
    from app.db.models import Venture
    from app.state import db

    with db()() as s:
        v = s.get(Venture, venture_id)
        return v.workspace_id if v else None


# ------------------------------------------------------------------ caps

def daily_cap(venture_id: str, text: str) -> dict[str, Any]:
    return _reply('You can pause your requested work at any time. Say "pause work" when you want to stop it.')


def execute_daily_cap(venture_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return daily_cap(venture_id, "")


def send_cap(venture_id: str, text: str) -> dict[str, Any]:
    from app.autopilot.guard import MAX_SEND_MAX

    n = _first_int(text)
    if n is None or not 1 <= n <= MAX_SEND_MAX:
        return _reply(f"Tell me a send limit between 1 and {MAX_SEND_MAX} a day. Nothing changed.")
    from app.approvals.external import execution_enabled

    note = "" if execution_enabled("gtm_email_send") else " Sending is not switched on for this server yet, so nothing will leave until it is."
    from app.core.local_mode import is_local
    if is_local() and note:
        from app.gtm.local_mail import blocked_message
        note = " " + blocked_message("send_email")
    return _reply(f"15 a day is recommended. Allow real emails from your connected Gmail, up to {n} a day?{note}", kind="confirmation", actions=[
        _card("autopilot.send_cap", interpretation=f"Allow sending up to {n} emails a day.",
              consequences=[f"Autopilot may email real people from your own Gmail, at most {n} a day.",
                            "Each email carries an opt-out line; anyone who declines is never emailed again.",
                            "Say \"stop sending\" any time to switch it off."], state={"send_max": n}, label=f"Allow {n}/day")])


def execute_send_cap(venture_id: str, payload: dict[str, Any], actor_subject: str | None = None,
                     message_id: str | None = None) -> dict[str, Any]:
    from app.approvals.external import execution_enabled
    from app.authz.workspace import subject_person_id
    from app.autopilot import guard, service
    from app.chat import confirmations
    from app.chat.conversations import actor_id

    if message_id:
        confirmations.card(venture_id, message_id, "autopilot.send_cap", payload)

    try:
        maximum = payload.get("send_max")
        if type(maximum) is not int or not 1 <= maximum <= guard.MAX_SEND_MAX:
            raise ValueError("send_max_invalid")
        out = service.update_settings(venture_id, allow_send=True, send_max_per_day=maximum,
            confirmed_send_max=True, actor_subject=subject_person_id(actor_subject or actor_id()))
    except (TypeError, ValueError):
        return _reply("That send limit is not valid. Nothing changed.")
    note = "" if execution_enabled("gtm_email_send") else " Sending is not switched on for this server yet (an admin step), so nothing leaves until it is."
    from app.core.local_mode import is_local
    if is_local() and note:
        from app.gtm.local_mail import blocked_message
        note = " " + blocked_message("send_email")
    return _reply(f"Sending is allowed: up to {out['send_max_per_day']} emails a day.{note}",
                  [{"label": "Stop sending", "action": "autopilot.send_off", "style": "ghost"}])


# ------------------------------------------------------------------ builds

def _latest_build(venture_id: str, workspace_id: str | None):
    from app.db.supymem_models import BuildRun
    from app.state import db

    with db()() as s:
        run = s.scalar(select(BuildRun).where(BuildRun.venture_id == venture_id, BuildRun.workspace_id == workspace_id,
                                              BuildRun.runtime_version == 1).order_by(BuildRun.created_at.desc(), BuildRun.id.desc()).limit(1))
        if run is None:
            return None
        try:
            spec = json.loads(run.spec or "{}")
        except (TypeError, ValueError):
            spec = {}
        review = (spec.get("lifecycle") or {}).get("review") or {}
        return {"id": run.id, "app_id": run.app_id, "state": run.state, "closed": bool(run.closed_at), "artifact_id": run.artifact_id,
                "verified": bool(spec.get("execution_verified")), "qa": spec.get("qa_state"), "review": review,
                "phase": spec.get("phase"), "diagnostic": spec.get("lifecycle_version") == 4}


def review_readiness(b: dict[str, Any] | None) -> tuple[bool, str]:
    """(ready, exact reason). Mirrors the checks builder.execution.review enforces, so chat never offers a doomed accept."""
    if b is None:
        return False, "No build has run for this venture yet, so there is nothing to accept."
    if b["diagnostic"]:
        return False, "The latest run was a diagnostic, which cannot be accepted as the product."
    st = (b["review"] or {}).get("status")
    if st == "accepted":
        return False, "The latest build is already accepted."
    if st == "rejected":
        return False, "You already asked for changes on the latest build. A new build is needed before acceptance."
    if b["state"] != "succeeded" or not b["closed"]:
        return False, f"The latest build is {str(b['state']).replace('_', ' ')}, not finished, so it cannot be accepted yet."
    if not (b["verified"] or b["qa"] == "passed"):
        return False, "The latest build finished but its independent QA has not passed, so it is not ready for your review."
    if st != "pending" or not (b["review"] or {}).get("report_digest") or not b["artifact_id"]:
        return False, "The latest build has no saved QA report awaiting your review yet."
    return True, ""


def build_status(venture_id: str, ctx_venture: dict | None) -> dict[str, Any]:
    from app.ventures.run_status import snapshot

    ws = (ctx_venture or {}).get("workspace_id") or _workspace_id(venture_id)
    b = _latest_build(venture_id, ws)
    href = f"/v/{venture_id}/builds" + (f"?app={b['app_id']}&run={b['id']}" if b else "")
    if b is None:
        return _reply("No build has run for this venture yet. Say \"build my landing page\" to prepare one for your review.",
                      [_nav("Open builds", href)])
    ready, why = review_readiness(b)
    snap = snapshot(venture_id, ws)
    line = f"Latest build: {str(b['state']).replace('_', ' ')}"
    line += f", QA {b['qa']}" if b["qa"] else ""
    line += ". " + ("It is ready for your review: say \"accept build\" to accept it." if ready else why)
    return _reply(line + (f" Right now: {snap.get('activity')}" if snap.get("state") in {"working", "queued"} else ""), [_nav("Open builds", href)])


def build_accept(venture_id: str, ctx_venture: dict | None) -> dict[str, Any]:
    ws = (ctx_venture or {}).get("workspace_id") or _workspace_id(venture_id)
    b = _latest_build(venture_id, ws)
    ready, why = review_readiness(b)
    if not ready:
        return _reply(f"I cannot accept the build yet. {why} Nothing was accepted.",
                      [_nav("Open builds", f"/v/{venture_id}/builds" + (f"?app={b['app_id']}&run={b['id']}" if b else ""))])
    return _reply("Accept the latest build as this milestone's deliverable? It moves your venture to readiness review. It does not publish anything.",
                  kind="confirmation", actions=[
        _card("build.accept", interpretation="Accept the latest build after your review.",
              consequences=["The independent QA report is recorded as reviewed by you and the milestone is accepted.",
                            "Nothing is published or sent; hosting is a separate step you confirm."],
              state={"run_id": b["id"], "source_artifact_id": b["artifact_id"], "report_digest": b["review"]["report_digest"]},
              label="Accept build"),
        _nav("Open builds first", f"/v/{venture_id}/builds?app={b['app_id']}&run={b['id']}", "ghost")])


def execute_build_accept(venture_id: str, payload: dict[str, Any], actor: str | None) -> dict[str, Any]:
    from app.builder import execution
    from app.db.supymem_models import BuildRun
    from app.state import db

    run_id = str(payload.get("run_id") or "")
    with db()() as s:
        run = s.get(BuildRun, run_id)
        if run is None or run.venture_id != venture_id:
            return _reply("That build does not belong to this venture. Nothing was accepted.")
    try:
        execution.review(run_id, actor=actor, source_artifact_id=str(payload.get("source_artifact_id") or ""),
                         report_digest=str(payload.get("report_digest") or ""), decision="accept")
    except execution.BuildError as exc:
        why = {"build_review_not_ready": "its QA report or state changed since you asked, so it is no longer ready",
               "build_review_conflict": "it already has a different review decision"}.get(exc.code, f"the review was refused ({exc.code})")
        return _reply(f"The build was not accepted: {why}. Open Builds to check.", [_nav("Open builds", f"/v/{venture_id}/builds")])
    return _reply("Build accepted. Hosting and publishing are separate: say \"publish my landing page\" when you want it live.",
                  [_nav("Open builds", f"/v/{venture_id}/builds", "ghost")])


# ------------------------------------------------------------------ outreach batch

def batch_approve(venture_id: str) -> dict[str, Any]:
    from app.approvals.external import execution_enabled
    from app.autopilot import guard, outreach

    waiting = [d for d in outreach.drafts(venture_id, "draft") if d.get("channel") == "email"]
    if not waiting:
        return _reply("No outreach drafts are waiting for approval. Say \"draft outreach\" and I will prepare some for your review. Nothing was sent.")
    st = guard.get_settings(venture_id)
    batches = list(dict.fromkeys(d.get("batch_id") for d in waiting if d.get("batch_id")))
    cap = st["send_max_per_day"] if st["allow_send"] else outreach.DEFAULT_SEND_MAX
    left = max(0, cap - outreach.send_slots_used_today(venture_id))
    notes = [f"{len(waiting)} draft(s) waiting; send limit {cap} a day, {left} left today."]
    if not execution_enabled("gtm_email_send"):
        from app.core.local_mode import is_local
        if is_local():
            from app.gtm.local_mail import blocked_message
            return _reply(blocked_message("send_email"))
        notes.append("Sending is not switched on for this server yet, so approving records your decision but nothing leaves until it is.")
    if left == 0:
        notes.append("Today's send limit is used up, so nothing more can go out today.")
    cards = [outreach.send_card(sum(d.get("batch_id") == b for d in waiting), b) for b in batches] or [outreach.send_card(len(waiting), None)]
    return _reply(" ".join(notes) + " Review the drafts, then confirm the batch below.", cards + [{"label": "Review drafts", "action": "outreach.drafts", "style": "ghost"}], "confirmation")


# ------------------------------------------------------------------ credits

def spend_today(venture_id: str) -> dict[str, Any]:
    if is_local():
        import sqlite3
        from app.chat.navigation_map import billing_hint
        from app.chat.recovery import log_founder_exception
        from app.credits import local_meter
        from app.credits.errors import CreditsUnavailable

        try:
            usage = local_meter.snapshot()
            body = (f"Your recorded model spending this month is ${usage['month_cost_usd_micro'] / 1_000_000:.2f}, "
                    "including estimates for unconfirmed calls. ")
        except (OSError, sqlite3.Error, CreditsUnavailable) as error:
            log_founder_exception(error, venture_id, "spend.read")
            body = ("Your spending records could not be read. Check that your local data folder is writable "
                    "and has free space, then restart your app and ask to see your spending again. ")
        return _reply(body + billing_hint(),
                      [{**_nav("Open Keys and connections", "/settings/local#spend-title"), "kind": "recovery_link"}])
    return _reply("You can manage your account in Settings.",
                  [_nav("Open Settings", f"/v/{venture_id}/settings")])


def credits_request(venture_id: str) -> dict[str, Any]:
    if is_local():
        return spend_today(venture_id)
    return _reply("Your request is ready to send to the team. You can keep working in chat while it is reviewed.",
                  kind="confirmation", actions=[
        _card("credits.request", interpretation="Request more usage from the team.",
              consequences=["Your request is recorded for the team to review."],
              state={}, label="Request more usage")])


def execute_credits_request(venture_id: str, actor: str | None) -> dict[str, Any]:
    if is_local():
        return spend_today(venture_id)
    from app.api.payments_api import record_credit_request

    out = record_credit_request(venture_id, _workspace_id(venture_id) or "", actor)
    return _reply("The team already has your request. You can keep working in chat while it is reviewed." if out.get("status") == "already_requested"
                  else "Your request is saved for the team. You can review your saved work while the team reviews it.")


# ------------------------------------------------------------------ status

def activity(venture_id: str, ctx_venture: dict | None) -> dict[str, Any]:
    from app.chat.next_moves import compose
    return compose(venture_id)


def blocked(venture_id: str) -> dict[str, Any]:
    from app.chat.next_moves import compose
    return compose(venture_id)


def new_chat() -> dict[str, Any]:
    return _reply("Starting a fresh chat. Your venture context, tasks and approvals carry over.",
                  [{"label": "Start new chat", "name": "chat.new_conversation", "style": "primary"}], kind="navigation")


async def dispatch(venture_id: str, intent: str, text: str, ctx_venture: dict | None) -> dict[str, Any]:
    if intent == "jarvis.resume_sending":
        from app.gtm.relationships.abuse import bounce_counts
        from app.state import db
        workspace_id = _workspace_id(venture_id)
        if not workspace_id:
            return _reply("This venture has no workspace for sending yet. Say \"find me customers\" to start. Nothing was sent.")
        with db()() as session:
            counts = bounce_counts(session, workspace_id)
        from app.chat.customers import _card
        return _reply(f"{counts['bounced']} contacts bounced across {counts['sent']} confirmed sends in this window. "
            "Check the bounced addresses first, then confirm Resume sending below. Nothing is sent by resuming.",
            [_card("gtm.resume_sending", "I checked the bounced addresses; resume sending.",
                   ["Nothing is sent now. Each batch still needs your approval."], {"reviewed_bounced_addresses": True}, "Resume sending"),
             {"label": "Show customers and drafts", "action": "gtm.status", "style": "ghost"}], "confirmation")
    if intent == "jarvis.cap_daily":
        return daily_cap(venture_id, text)
    if intent == "jarvis.cap_send":
        return send_cap(venture_id, text)
    if intent == "jarvis.build_status":
        return build_status(venture_id, ctx_venture)
    if intent == "jarvis.build_accept":
        return build_accept(venture_id, ctx_venture)
    if intent == "jarvis.batch_approve":
        return batch_approve(venture_id)
    if intent == "jarvis.spend_today":
        return spend_today(venture_id)
    if intent == "jarvis.credits_request":
        return credits_request(venture_id)
    if intent == "jarvis.activity":
        return activity(venture_id, ctx_venture)
    if intent == "jarvis.blocked":
        return blocked(venture_id)
    return new_chat()


INTENTS = frozenset({"jarvis.cap_daily", "jarvis.cap_send", "jarvis.build_status", "jarvis.build_accept", "jarvis.batch_approve",
                     "jarvis.spend_today", "jarvis.credits_request", "jarvis.activity", "jarvis.blocked", "jarvis.new_chat", "jarvis.resume_sending"})
