"""God mode's lifecycle brain: what the next moves for a venture are, and the drivers for the steps
the journey leaves to the founder (answering clarifications, accepting validation, ...).

next_step() names ONE step. A "move" is that step turned into something Supymem can do: a chat action
(research.start, plan.approve, ...), a driver below, or "human" (only a person can do it: a real
interview, a CAPTCHA, a payment). Human moves never stop the venture; branches() lists work that
can run alongside them. The tick and the Suggestions cards both read this, so they never disagree.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from app.credits.units import MICRO_PER_CREDIT
from app.state import db
from core.clock import utcnow

ASSUMED = "[AI-assumed by Supymem God mode] "
CLARIFY_SILENCE = timedelta(minutes=5)  # give a founder who is present a chance to answer first
MAX_ASSUMED_ANSWERS = 6  # then stop asking questions and let the clarifier propose from what it has
HOSTING_ACTION = "hosting.host"  # hook: the "host my product" chat action; see _hosting_move

# Moves that cannot be undone from the app (everything else is a draft, a read or can be re-run).
IRREVERSIBLE = frozenset({"outreach.send", "site.publish", "plan.approve", "mac.approve", "build.start", "idea.advance"})
MAC_KINDS = frozenset({"mac.approve"})
MAC_NATIVE_APPROVAL_REQUIRED = "mac_native_approval_required"
MAC_NATIVE_APPROVAL_MESSAGE = "Open the task review in the updated Supymem app on your Mac."


@dataclass(frozen=True)
class Move:
    key: str
    title: str
    why: str
    will_do: str
    action: str | None = None  # chat action or driver name; None means only a human can do it
    payload: dict[str, Any] = field(default_factory=dict)
    human_href: str = ""  # where the founder goes when action is None
    wait: bool = False  # nothing to do yet (work running, founder may still answer)

    @property
    def human(self) -> bool:
        return self.action is None and not self.wait


def move_id(key: str, action: str | None, payload: dict[str, Any]) -> str:
    raw = json.dumps([key, action, payload], sort_keys=True, default=str)
    return "mv_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ resolving a journey step

def _step_move(step: dict[str, Any], venture_id: str | None = None) -> Move:
    key, title, detail = str(step.get("key")), str(step.get("title") or ""), str(step.get("detail") or "")
    href = str(step.get("href") or "")
    action, payload = step.get("chat_action"), dict(step.get("payload") or {})
    if action == "resume":  # a pause is the founder's call
        return Move(key, title, detail, "Nothing until you resume.", None, human_href=href)
    if key == "gtm.diagnose":  # the diagnosis is the founder's own description of customers; there is nothing for me to run
        return Move(key, title, detail, "Only you can describe your real customers. I carry on with everything else.", None, human_href=href)
    if key.startswith("mac.") and not action and venture_id:
        return native_mac_review_move(venture_id, key[len("mac."):]) or Move(key, title, detail, "", wait=True)
    if action:
        if action == "idea.advance" and venture_id:
            payload = _idea_advance_payload(venture_id)
        return Move(key, title, detail, detail or "I will carry out the proposed action with the approved credit limit.", action, payload)
    if key == "idea.clarify":
        question_id = _open_question_id(venture_id) if venture_id else None
        if venture_id and not question_id:
            return Move(key, title, detail, "Waiting for the next clarification question to be prepared.", wait=True)
        return Move(key, "Answer the idea questions for you", "You have not answered the open question. "
                    "I can answer it from your idea and mark the answer as an assumption you can correct.",
                    "Picks the most likely answer from your idea, marks it AI-assumed, and carries on.",
                    "idea.answer", {"question_id": question_id} if venture_id else {})
    if key == "idea.review_validation":
        return Move(key, "Validate and confirm the idea", "The clarification is finished.",
                    "Runs validation, accepts it, and confirms the idea so research can start.",
                    "idea.advance", _idea_advance_payload(venture_id) if venture_id else {})
    if key == "build.review":
        return Move(key, title, detail, "Only you can accept a build. I carry on with everything else meanwhile.", None, human_href=href)
    if key == "build.first":
        return Move(key, title, detail, "Prepares and quotes the first build, then starts it inside your cap.",
                    "build.prepare", {})
    if key == "gtm.outreach" and venture_id:
        from app.autopilot import outreach

        if outreach.drafts(venture_id):  # drafts exist or went out: the outreach branches and the reply watcher carry it from here
            return Move(key, title, "Outreach is under way: drafts are written and I am watching for replies.",
                        "Nothing to start; replies and follow-ups are handled as they arrive.", wait=True)
    if key == "gtm.outreach":
        return Move(key, "Draft first outreach", detail, "Finds people to contact and writes a batch of personal drafts. Nothing is sent.",
                    "outreach.draft", {"batch": True})
    if key in {"build.finish_product", "build.verify_product"}:
        return _hosting_move(key, title, detail, href)
    if key in {"site.audit", "experiment.plan"}:
        return Move(key, title, detail, "Review the saved evidence and choose what to do next.", None, human_href=href)
    if key in {"experiment.run", "experiment.review"}:
        what = ("Run this test with real people and tell me what happened"
                if key == "experiment.run" else "Pick the next direction")
        return Move(key, title, detail, what + ". Only you can do this.", None, human_href=href)
    if key in {"work.running", "idea.validating", "research.running"}:
        return Move(key, title, detail, "Already in progress.", wait=True)
    if key == "venture.paused":
        return Move(key, title, detail, "Paused by you.", None, human_href=href)
    return Move(key, title, detail, "Nothing I can do yet.", wait=True)


def _open_question_id(venture_id: str) -> str:
    """Each clarifying question is its own move (and its own Suggestions card), not 'the' answer-the-idea move."""
    from app.db.supymem_models import ClarifierTurn

    idea = _idea_session(venture_id)
    if idea is None:
        return ""
    with db()() as s:
        last = s.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                         .order_by(ClarifierTurn.turn.desc()).limit(1)).first()
    return last.id if last is not None and last.role != "founder" else ""


def _idea_advance_payload(venture_id: str) -> dict[str, Any]:
    """Validation admission and later confirmation are separate source states."""
    from app.idea.service import advance_binding

    idea = _idea_session(venture_id)
    if idea is None:
        return {}
    with db()() as session:
        return advance_binding(session, idea)


def _hosting_move(key: str, title: str, detail: str, href: str) -> Move:
    # ponytail: hook only. Once the "host my product" action is registered AND guard lets it spend,
    # return Move(key, ..., HOSTING_ACTION) here. Until then hosting stays a founder decision.
    return Move(key, title, detail, "Hosting and live account tests need you for now.", None, human_href=href)


def primary_move(venture_id: str) -> Move:
    from app.ventures.next_step import next_step

    return _step_move(next_step(venture_id), venture_id)


# ------------------------------------------------------------------ work that runs alongside

def branches(venture_id: str) -> list[Move]:
    """Useful moves that do not wait on the primary step: once the plan is approved, drafts and diagnosis."""
    try:
        from app.ventures.next_step import EXECUTABLE_PLAN_STATES, _facts

        f = _facts(venture_id)
    except Exception:  # noqa: BLE001 - branches are a bonus; never break the tick
        return []
    if not f.get("idea_confirmed") or f.get("plan_state") not in EXECUTABLE_PLAN_STATES or f.get("personal"):
        return []
    from app.ventures.state_brief import _website_section

    launched = _website_section(f["workspace_id"], venture_id)["live"]
    pmf = [Move("branch.pmf", "Run the next product-market-fit experiment",
                "Your landing page is live. Cheap experiments on positioning, audience, channel and offer find users fastest.",
                "Starts up to three small experiments in parallel, measures them on real sign-ups and replies, "
                "and keeps, pivots or drops each one.", "pmf.iterate", {})] if launched else []
    from app.autopilot import outreach

    # No "diagnose the go-to-market" branch: gtm.diagnosis only reads back a diagnosis the founder already saved, so running
    # it on a timer produced nothing. The PMF loop and outreach below are the go-to-market work Supymem can do alone.
    return pmf + outreach.moves(venture_id)


def pending_mac_orders(venture_id: str) -> list[dict[str, Any]]:
    from app.build import device_orders
    from app.db.models import VentureDocument

    with db()() as s:
        rows = []
        for row in s.scalars(select(VentureDocument).where(
            VentureDocument.venture_id == venture_id, VentureDocument.kind == device_orders.KIND)
            .order_by(VentureDocument.created_at.desc()).limit(10)):
            try:
                body = json.loads(row.provenance_json or "{}")
                if isinstance(body, dict) and body.get("id") == row.id:
                    rows.append(body)
            except (TypeError, ValueError):
                continue
    now = utcnow().isoformat()
    return [b for b in rows if b.get("state") == "pending" and not b.get("claimed_at")
            and b.get("expires_at", "") > now]


def native_mac_review_move(venture_id: str, order_id: str | None = None) -> Move | None:
    """Fresh owned review navigation; no approval, compatibility or execution proof."""
    from app.authz.workspace import subject_person_id
    from app.build import device_orders
    from app.credits._txn import naive_utc
    from app.db.models import VentureDocument
    from app.db.supymem_models import DesktopDevice
    from app.tasks.service import _active_custody
    from app.tenancy import current_or_none

    scope = current_or_none()
    if scope is None or scope.venture_id != venture_id:
        return None
    owner = subject_person_id(scope.actor)
    with db()() as session:
        if not _active_custody(session, scope.workspace_id, venture_id, owner):
            return None
        query = select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind == device_orders.KIND)
        if order_id is not None:
            query = query.where(VentureDocument.id == order_id)
        for row in session.scalars(query.order_by(VentureDocument.created_at.desc()).limit(10)):
            try:
                body = json.loads(row.provenance_json or "{}")
                if (not isinstance(body, dict) or "lease_contract" not in body
                        or (body.get("id"), body.get("venture_id"), body.get("workspace_id"), body.get("actor_id"))
                            != (row.id, venture_id, scope.workspace_id, owner)
                        or body.get("state") != "pending" or any(key in body for key in (
                            "claimed_at", "execution_id", "approved_at", "reservation_id", "invocations", "iterations",
                            "native_stop", "stop_reconciliation_required", "inference_reconciliation_required"))
                        or naive_utc(datetime.fromisoformat(body["expires_at"])) <= naive_utc()
                        or body.get("review_digest") != device_orders._digest(body)):
                    continue
                device = session.get(DesktopDevice, body.get("device_id"))
                if not device or device.person_id != owner or device.revoked_at:
                    continue
                if body["task_snapshot"] != device_orders._review_task_snapshot(session, scope.workspace_id, venture_id,
                        body["manifest"], actor=owner, device=device.id):
                    continue
                href = f"/v/{quote(venture_id, safe='')}/device-work-orders/{quote(row.id, safe='')}"
                return Move(f"mac.{row.id}", "Review on Mac", MAC_NATIVE_APPROVAL_MESSAGE, MAC_NATIVE_APPROVAL_MESSAGE,
                    payload={"order_id": row.id, "review_digest": body["review_digest"]}, human_href=href)
            except (AttributeError, KeyError, TypeError, ValueError, PermissionError):
                continue
    return None


def native_mac_review_retired(venture_id: str, order_id: str) -> bool:
    """Retire only from saved state; an unreadable or mismatched source stays unresolved."""
    from fastapi import HTTPException
    from app.authz.workspace import subject_person_id
    from app.autopilot.authority import founder_scope
    from app.build import device_orders
    from app.credits._txn import naive_utc
    from app.db.models import VentureDocument
    from app.db.supymem_models import DesktopDevice
    from app.tasks.device import _order_unknown
    from app.tasks.service import _active_custody
    try:
        with founder_scope(venture_id, autonomous=True) as scope, db()() as session:
            owner = subject_person_id(scope.actor)
            if not _active_custody(session, scope.workspace_id, venture_id, owner):
                return False
            row = session.get(VentureDocument, order_id)
            if row is None:
                return False
            body = json.loads(row.provenance_json or "{}")
            if (row.kind, row.venture_id, body.get("id"), body.get("venture_id"), body.get("workspace_id"), body.get("actor_id")) != (
                    device_orders.KIND, venture_id, order_id, venture_id, scope.workspace_id, owner):
                return False
            device = session.get(DesktopDevice, body.get("device_id"))
            if not device or device.person_id != owner or device.revoked_at:
                return False
            if body.get("review_digest") != device_orders._digest(body) or _order_unknown(body):
                return False
            if body.get("state") in {"approved", "claimed", "cancelled", "stopped", "expired"}:
                return True
            return body.get("state") == "pending" and naive_utc(datetime.fromisoformat(body["expires_at"])) <= naive_utc()
    except (AttributeError, KeyError, TypeError, ValueError, PermissionError, HTTPException):
        return False


def mac_moves(venture_id: str) -> list[Move]:
    out = []
    for body in pending_mac_orders(venture_id):
        order_id = body.get("id")
        if not isinstance(order_id, str) or not order_id or order_id != order_id.strip():
            continue
        if "lease_contract" in body:
            if (review := native_mac_review_move(venture_id, order_id)) is not None:
                out.append(review)
            continue
        manifest = body.get("manifest") or {}
        title = manifest.get("title") or "Mac task"
        out.append(Move(f"mac.{body['id']}", f"Approve Mac task: {title}"[:200], "Reviewed and waiting for your OK.",
                        "Approves the reviewed task; your Mac runs it when it is online.", "mac.approve",
                        {"order_id": body["id"], "review_digest": body.get("review_digest")}))
    return out


def all_moves(venture_id: str) -> list[Move]:
    """Primary move first, then Mac approvals and parallel branches. Used by the Suggestions cards."""
    return [primary_move(venture_id), *mac_moves(venture_id), *branches(venture_id)]


# ------------------------------------------------------------------ drivers

def _reply(body: str, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": [], "citations": []}


def _idea_session(venture_id: str):
    from app.db.supymem_models import IdeaSession

    with db()() as s:
        return s.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
                        IdeaSession.state != "abandoned").order_by(IdeaSession.updated_at.desc()).limit(1))


def _option_text(opt: Any) -> str:
    if isinstance(opt, dict):
        opt = opt.get("label") or opt.get("text") or opt.get("value") or ""
    return str(opt).strip()


def answer_clarification(venture_id: str, owner: str | None, *, immediate: bool = False,
                         question_id: str | None = None) -> dict[str, Any]:
    """Answer the open clarification question when the founder is silent. The answer is marked AI-assumed."""
    from app.db.supymem_models import ClarifierTurn
    from app.idea.service import get_session, post_message, retry_failed_clarification

    idea = _idea_session(venture_id)
    if idea is None:
        return _reply("There is no idea session yet.", "needs_you")
    with db()() as s:
        turns = s.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                          .order_by(ClarifierTurn.turn)).all()
    last = turns[-1] if turns else None
    if question_id is not None and (idea.state == "confirmed" or last is None or last.id != question_id
                                    or last.role == "founder" or last.kind != "question"):
        return _reply("The reviewed question changed or was already answered. Review the current Idea page.", "needs_you")
    if last is None or last.role == "founder" or last.kind != "question":
        info = get_session(idea.id)
        if info.get("clarification_retry_available"):
            retry_failed_clarification(idea.id, venture_id=venture_id, actor_subject=owner)
            return _reply("The last clarification step failed, so I retried it.")
        return _reply("Waiting for the next clarification question to be prepared.", "wait")
    if not immediate and utcnow().replace(tzinfo=None) - last.created_at.replace(tzinfo=None) < CLARIFY_SILENCE:
        return _reply("Giving you a few minutes to answer this question yourself.", "wait")
    raw_options = json.loads(last.options or "[]")
    if isinstance(raw_options, dict):
        raw_options = raw_options.get("options") or []
    options = [t for t in (_option_text(o) for o in raw_options) if t]
    assumed = sum(1 for t in turns if t.role == "founder" and t.text.startswith(ASSUMED))
    if options and assumed < MAX_ASSUMED_ANSWERS:
        text = ASSUMED + options[0]
        said = f"Answered \"{last.text[:120]}\" with \"{options[0][:120]}\" (AI-assumed)."
    else:
        text, said = "just propose", "Asked the clarifier to propose the MVP from what it already knows (AI-assumed)."
    posted = post_message(idea.id, text, actor_subject=owner, expected_question_id=last.id)
    from app.autopilot import receipts
    if posted.get("job_id"):
        receipts.queued_job(posted["job_id"])
    if posted.get("session"):
        with db()() as session:
            answer = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id,
                ClarifierTurn.turn > last.turn, ClarifierTurn.role == "founder").order_by(ClarifierTurn.turn).limit(1))
            if answer:
                receipts.persisted_record(ClarifierTurn, answer.id, {"text": answer.text})
    return _reply(said)


def advance_idea(venture_id: str, owner: str | None, *, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start validation after clarification, then accept it and confirm the idea (same calls as the Idea page)."""
    from app.chat.execute import _confirm_idea_for_research
    from app.idea.service import get_session
    from app.idea.validation import start_validation
    from app.ventures.next_step import _facts

    idea = _idea_session(venture_id)
    if idea is None:
        return _reply("There is no idea session yet.", "needs_you")
    if expected is not None and expected != _idea_advance_payload(venture_id):
        return _reply("The idea or validation phase changed. Review a fresh suggestion before advancing it.", "needs_you")
    f = _facts(venture_id)
    if f.get("personal"):
        return _reply("This is a personal project, so you confirm it yourself on the Idea page.", "needs_you")
    if idea.state == "validating" and f.get("validation_done"):
        blocked = _confirm_idea_for_research(venture_id, owner, **({"expected_advance_binding": expected} if expected is not None else {}))
        if blocked:
            return {**blocked, "kind": "needs_you"}
        from app.autopilot import receipts
        from app.db.supymem_models import IdeaSession

        receipts.persisted_record(IdeaSession, idea.id, {"state": "confirmed"})  # the saved confirmation is this step's receipt
        return _reply("Accepted the validation and confirmed the idea (AI-assumed). Research is next.")
    if idea.state in {"reviewing", "clarifying", "proposing", "recon", "open"}:
        try:
            bound = {"revision_id": expected["idea_revision_id"], "expected_advance_binding": expected} if expected is not None else {}
            validation = start_validation(idea.id, venture_id=venture_id, workspace_id=idea.workspace_id, **bound)
        except RuntimeError as error:
            if str(error) in {"idea_advance_scope_changed", "validation_revision_superseded"}:
                return _reply("The idea or validation phase changed. Review a fresh suggestion before advancing it.", "needs_you")
            raise
        if validation.get("job_id"):
            from app.autopilot import receipts
            receipts.queued_job(validation["job_id"])
        return _reply("Started validating the idea.")
    return _reply(f"Validation is {get_session(idea.id).get('state')}.", "wait")


async def run_driver(venture_id: str, name: str, payload: dict[str, Any], owner: str | None,
                     human: bool = False) -> dict[str, Any]:
    """Run a driver (or approve a Mac order) and return a chat-shaped reply."""
    from app.autopilot.authority import founder_scope

    with founder_scope(venture_id, owner, autonomous=not human) as scope:
        return await _run_driver_scoped(venture_id, name, payload, scope.actor, human)


async def _run_driver_scoped(venture_id: str, name: str, payload: dict[str, Any], owner: str,
                             human: bool) -> dict[str, Any]:
    import asyncio

    if name == "idea.answer":
        return await asyncio.to_thread(answer_clarification, venture_id, owner,
                                       immediate=bool(payload.get("immediate")) or human,
                                       question_id=payload.get("question_id"))
    if name == "idea.advance":
        bound = {"expected": payload} if "idea_phase" in payload else {}
        return await asyncio.to_thread(advance_idea, venture_id, owner, **bound)
    if name == "mac.approve":
        return await asyncio.to_thread(approve_mac_order, venture_id, payload)
    if name == "pmf.iterate":
        from app.autopilot import service

        return await service.pmf_iterate(venture_id, owner, human)
    raise ValueError(f"unknown driver {name}")


def mac_approval_blocker(body: dict[str, Any]) -> dict[str, Any] | None:
    # No server-recorded native negotiation proof exists for this producer.
    if "lease_contract" in body:
        return {**_reply(MAC_NATIVE_APPROVAL_MESSAGE, "needs_you"), "code": MAC_NATIVE_APPROVAL_REQUIRED}
    return None


def approve_mac_order(venture_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from app.db.models import VentureDocument
    from app.admin.authn import CallerContext
    from app.build import device_orders

    body = next((b for b in pending_mac_orders(venture_id) if b.get("id") == payload.get("order_id")), None)
    if body is None:
        return _reply("That Mac task is no longer waiting.", "needs_you")
    from app.tenancy import current_or_none
    from app.authz.workspace import subject_person_id
    scope = current_or_none()
    actor = subject_person_id(scope.actor) if scope else None
    if not scope or (scope.venture_id, scope.workspace_id, actor) != (
            venture_id, body.get("workspace_id"), body.get("actor_id")):
        raise PermissionError("autopilot_scope_mismatch")
    reviewed_digest = payload.get("review_digest")
    if not isinstance(reviewed_digest, str) or not reviewed_digest or reviewed_digest != body.get("review_digest"):
        return _reply("This Mac task needs a fresh review before approval. Open the current task card.", "needs_you")
    if (blocker := mac_approval_blocker(body)) is not None:
        return blocker
    caller = CallerContext(sub=body["actor_id"], email="", role="user", path="", method="POST", exp=0,
                           nonce="autopilot", device_id=body.get("device_id"))
    # The transition repeats this comparison under its own transaction. Never
    # substitute a fresh digest for the task the founder actually reviewed.
    approved = device_orders.transition(venture_id, caller, body["id"], reviewed_digest, "approve")
    # Certifies approval only, never claim, input, task execution or completion.
    from app.autopilot.receipts import saved_document
    with db()() as session:
        row = session.get(VentureDocument, body["id"])
        saved = json.loads(row.provenance_json or "{}") if row else {}
        if (row is None or row.venture_id != venture_id or row.kind != device_orders.KIND
                or (saved.get("workspace_id"), saved.get("actor_id"), saved.get("device_id")) !=
                   (scope.workspace_id, actor, body.get("device_id"))
                or saved.get("state") != "approved" or saved.get("claimed_at") or saved.get("execution_id")
                or saved.get("review_digest") != approved.get("review_digest")):
            raise RuntimeError("mac_approval_outcome_unconfirmed")
        saved_document(row.id, row.kind, row.body_md, provenance=row.provenance_json)
    return _reply("Approved the Mac task. Your Mac runs it when it is online.")


DRIVERS = frozenset({"idea.answer", "idea.advance", "mac.approve", "pmf.iterate"})


def estimate_credits(action: str | None, payload: dict[str, Any]) -> float:
    from app.autopilot import guard

    return guard.cost_micro(action or "", payload) / MICRO_PER_CREDIT
