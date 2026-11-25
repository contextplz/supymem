"""Suggestions: generate cards, list them, and carry out the founder's decision on each.

"Sure, go ahead" is the founder's explicit confirmation: the action runs through the same chat execute
path the confirmation cards use, inside the credit estimate the card showed. Navigation-only work,
publishing and sends are never offered (proposals.CAPABILITIES).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.credits.units import MICRO_PER_CREDIT
from app.db.models import Venture, Workspace
from app.db.supymem_models import Suggestion
from app.state import db
from app.suggestions import proposals
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger("app.suggestions")
MAX_NEW = 5  # fresh cards waiting per venture
DISMISS_COOLDOWN = timedelta(days=7)
DONE_COOLDOWN = timedelta(days=1)
OPEN_STATES = ("new", "saved", "accepted", "executing", "running", "needs_you")
FEEDBACK = frozenset({"not_relevant", "not_now", "too_expensive", "other"})


def _emit(event: str, row: Suggestion, person_id: str | None = None, **props: Any) -> None:
    try:
        from app.analytics.events import emit

        with db()() as s:
            ven = s.get(Venture, row.venture_id)
        emit(f"feature.suggestions.{event}", person_id=person_id or row.person_id,
             workspace_id=ven.workspace_id if ven else None, venture_id=row.venture_id,
             props={"feature": "suggestions", "action_kind": row.action_kind, **props})
    except Exception:  # noqa: BLE001 - analytics never blocks a decision
        log.warning("suggestions_emit_failed", exc_info=True)


def _result_href(row: Suggestion) -> str:
    if row.action_kind == "browser.task.prepare":
        try:
            result = json.loads(row.result_ref or "{}")
        except (ValueError, TypeError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        href = result.get("href", "")
        prefix = f"/v/{row.venture_id}/device-work-orders/"
        return href if isinstance(href, str) and re.fullmatch(re.escape(prefix) + r"dwo_[A-Za-z0-9_-]+", href) else f"/v/{row.venture_id}/settings#settings-computer-use"
    if row.action_kind == "outreach.draft":
        return f"/gtm?venture={row.venture_id}&step=review"
    page = {"idea.answer": "idea", "idea.advance": "idea", "research.start": "research",
            "waitlist.enable": "site", "pagebuilder.build": "site", "recovery.plan_template": "plan"}
    return f"/v/{row.venture_id}/{page.get(row.action_kind, '')}".rstrip("/")


def _result_summary(row: Suggestion, result: dict) -> str:
    message = str(result.get("message") or "")
    if row.status == "done":
        if row.action_kind == "outreach.draft":
            if message.startswith("No people to write to yet"):
                return "No people to write to yet. Find customers first."
            count = re.search(r"\b(\d+) drafts? ready for review", message)
            count = int(count[1]) if count else len(set(re.findall(r"(?im)^\s*Draft\s+(\d+)", message)))
            return f"{count} draft{'s' if count != 1 else ''} ready for review" if count else "Drafts ready for review"
        return {"research.start": "Research ready for review", "idea.answer": "Idea answer saved",
                "idea.advance": "Idea validation ready for review", "waitlist.enable": "Waitlist enabled and tested",
                "pagebuilder.build": "Private landing page preview ready",
                "browser.task.prepare": "Browser task prepared for review; nothing has run",
                "recovery.plan_template": "Plan template ready for review"}.get(row.action_kind, "Done")
    return " ".join(message.replace("```", "").split())[:200]


def public(r: Suggestion, venture_name: str = "") -> dict[str, Any]:
    try:
        result = json.loads(r.result_ref or "{}")
    except (ValueError, TypeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    return {"id": r.id, "venture_id": r.venture_id, "venture_name": venture_name, "title": r.title, "why": r.why,
            "plan_text": r.plan_text, "est_credits": r.est_credits, "action_kind": r.action_kind,
            "requires_mac": r.requires_mac, "status": "accepted" if r.status in {"running", "needs_you", "noop"} else r.status,
            "result_state": r.status if r.status in {"running", "done", "needs_you", "noop"} else None,
            "created_at": r.created_at.isoformat(),
            "decided_at": r.decided_at.isoformat() if r.decided_at else None, "result_ref": r.result_ref,
            "result_href": _result_href(r), "result_message": _result_summary(r, result)}


def _dedupe_key(p: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([p["action_kind"], p["payload"]], sort_keys=True, default=str).encode()).hexdigest()[:24]


def _owner(venture_id: str) -> str | None:
    with db()() as s:
        ven = s.get(Venture, venture_id)
        ws = s.get(Workspace, ven.workspace_id) if ven and ven.workspace_id else None
        return ws.owner_id if ws else None


# ------------------------------------------------------------------ generate

def generate(venture_id: str) -> list[str]:
    """Add up to MAX_NEW fresh, non-duplicate cards for one venture. Returns the new ids."""
    from app.ventures.lifecycle import is_paused

    if is_paused(venture_id):
        return []
    _expire_stale({venture_id})
    now = utcnow()
    with db()() as s:
        ven = s.get(Venture, venture_id)
        if ven is None or ven.archived_at is not None or ven.stage == "purged":
            return []
        waiting = s.scalar(select(func.count(Suggestion.id)).where(
            Suggestion.venture_id == venture_id, Suggestion.status == "new")) or 0
        if waiting >= MAX_NEW:
            return []
        rows = s.scalars(select(Suggestion).where(Suggestion.venture_id == venture_id)).all()
        blocked = set()
        for r in rows:
            at = (r.decided_at or r.created_at).replace(tzinfo=None)
            age = now.replace(tzinfo=None) - at
            if r.status in OPEN_STATES or (r.status == "dismissed" and age < DISMISS_COOLDOWN) \
                    or (r.status in {"done", "failed"} and age < DONE_COOLDOWN):
                blocked.add(r.dedupe_key)
    owner, ids, titles = _owner(venture_id), [], []
    with db()() as s:
        for candidate in proposals.propose_next_actions(venture_id, MAX_NEW):
            p = proposals._shape(candidate)
            if p is None:
                continue
            p = {**p, "payload": {**p["payload"], "_suggestion_state": _state_binding(venture_id)}}
            key = _dedupe_key(p)
            if key in blocked or len(ids) >= MAX_NEW - waiting:
                continue
            blocked.add(key)
            row = Suggestion(id=new_id("sug"), venture_id=venture_id, person_id=owner, title=p["title"], why=p["why"],
                             plan_text=p["plan_text"], est_credits=p["est_credits"], action_kind=p["action_kind"],
                             payload_json=json.dumps(p["payload"], sort_keys=True, default=str), dedupe_key=key,
                             requires_mac=p["requires_mac"], status="new", created_at=now)
            s.add(row)
            ids.append(row.id)
            titles.append(row.title)
        s.commit()
    if ids:
        try:
            from app import desktop_notices

            n = len(ids)
            desktop_notices.enqueue(title=f"Supymem can take {n} thing{'s' if n != 1 else ''} off your plate",
                                    body="; ".join(titles)[:300], venture_id=venture_id, kind="suggestions",
                                    target_path=f"/v/{venture_id}/suggestions")
        except Exception:  # noqa: BLE001 - the cards are saved; a missed notice is not a failure
            log.exception("suggestions_notice_failed")
    return ids


def generate_all() -> int:
    from app.autopilot.authority import founder_scope

    with db()() as s:
        ids = list(s.scalars(select(Venture.id).where(Venture.archived_at.is_(None), Venture.stage.not_in(("archived", "purged")))))
    made = 0
    for vid in ids:
        try:
            with founder_scope(vid, autonomous=True):
                made += len(generate(vid))
        except Exception:  # noqa: BLE001 - one venture never blocks the rest
            log.exception("suggestions_generate_failed", extra={"venture_id": vid})
    return made


# ------------------------------------------------------------------ read

def _state_binding(venture_id: str) -> dict[str, Any]:
    from app.db.models import Job, PlannedTask, SiteVersion
    from app.db.supymem_models import BuildRun, ClarifierTurn, ExecutionPlan, ExistingAsset, IdeaSession, PlanTask, ResearchDossier, ValidationRun, VentureWaitlist
    from app.tenancy import bind, current_or_none
    from app.ventures.next_step import next_step
    from app.ventures.idea_page import JOB_KIND as IDEA_PAGE_REFRESH

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage in {"archived", "purged"}:
            return {"unavailable": True}
        scope = current_or_none()
        with bind(workspace_id=venture.workspace_id, venture_id=venture_id, actor=scope.actor if scope else "system"):
            step = next_step(venture_id)
        result = {"idea": venture.raw_idea, "stage": venture.stage, "step": step.get("key"),
                  "step_payload": step.get("payload")}
        # ponytail: venture-wide snapshot expires unrelated cards too, narrow per-kind bindings if this creates churn.
        for model, fields in ((IdeaSession, ("state", "current_revision_id", "confirmed_revision_id")),
                              (ExecutionPlan, ("state", "revision_id", "structured")),
                              (PlanTask, ("state", "build_run_id")), (ResearchDossier, ("state", "revision_id")),
                              (ValidationRun, ("state", "revision_id")), (Job, ("status",)),
                              (ClarifierTurn, ("kind", "role", "text", "options", "chosen", "revision_id")), (PlannedTask, ("status",)),
                              (BuildRun, ("state", "plan_task_id", "spec")), (SiteVersion, ("version", "copy_hash", "published")),
                              (ExistingAsset, ("detail", "exists", "usable"))):
            result[model.__tablename__] = [[row.id, *[getattr(row, field) for field in fields]]
                for row in session.scalars(select(model).where(model.venture_id == venture_id,
                    model.workspace_id == venture.workspace_id).order_by(model.id))
                if model is not Job or row.kind != IDEA_PAGE_REFRESH]
        waitlist = session.get(VentureWaitlist, venture_id)
        result["waitlist"] = [waitlist.enabled, waitlist.verified_entry_id] if waitlist else None
        return result


def _is_current(row: Suggestion, state: dict, candidates: list[dict]) -> bool:
    try:
        payload = json.loads(row.payload_json)
        binding = payload.pop("_suggestion_state", None)
        return (row.action_kind in proposals.EXECUTABLE_KINDS and not state.get("unavailable")
                and binding == state and any(p["action_kind"] == row.action_kind and p["payload"] == payload
                                             for p in candidates))
    except (ValueError, TypeError, AttributeError):
        return False


def _expire_stale(venture_ids: set[str]) -> None:
    for vid in venture_ids:
        try:
            state = _state_binding(vid)
            candidates = [shaped for candidate in proposals.propose_next_actions(vid, MAX_NEW)
                          if (shaped := proposals._shape(candidate)) is not None]
        except Exception:  # noqa: BLE001
            state, candidates = {"unavailable": True}, []
        with db()() as s:
            for row in s.scalars(select(Suggestion).where(Suggestion.venture_id == vid,
                                                         Suggestion.status.in_(("new", "saved")))):
                if not _is_current(row, state, candidates):
                    row.status, row.decided_at, row.result_ref = "expired", utcnow(), "expired:state_changed"
            s.commit()


def _reconcile(venture_ids: set[str]) -> None:
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaClarificationRun, ResearchDossier
    from app.research.execution import identifier as dossier_id
    from app.db import models, supymem_models

    active = {"pending", "scheduled", "retry_wait", "running", "leased"}
    with db()() as session:
        for row in session.scalars(select(Suggestion).where(Suggestion.venture_id.in_(venture_ids),
                                                            Suggestion.status.in_(("running", "accepted", "executing", "needs_you")))):
            try:
                result = json.loads(row.result_ref or "{}")
            except (ValueError, TypeError):
                result = {}
            jobs = result.get("jobs", []) if isinstance(result, dict) else []
            if not jobs:
                if row.status == "executing" and row.decided_at and (
                        utcnow().replace(tzinfo=None) - row.decided_at.replace(tzinfo=None)) < timedelta(minutes=15):
                    continue
                if row.status in {"accepted", "executing", "running"}:
                    row.status = "needs_you"
                    row.result_ref = json.dumps({"message": "Review the saved outcome before retrying; no live job identity was recorded.",
                                                "href": f"/v/{row.venture_id}/now"})
                continue
            venture = session.get(Venture, row.venture_id)
            owned_jobs = [session.get(Job, identifier) for identifier in jobs]
            if any(not job or (job.venture_id, job.workspace_id) != (row.venture_id, venture.workspace_id)
                   for job in owned_jobs):
                status, message = "needs_you", "The saved job is unavailable. Review the outcome before retrying."
            elif any(job.status in active for job in owned_jobs):
                continue
            elif any(job.status != "succeeded" for job in owned_jobs):
                status, message = "needs_you", "The job stopped without completing. Review its failure or cancellation before retrying."
            else:
                valid = True
                for job in owned_jobs:
                    if job.kind in {"research.deep", "idea.validate"}:
                        dossier = session.get(ResearchDossier, dossier_id(job.id))
                        valid = valid and bool(dossier and (dossier.venture_id, dossier.workspace_id) == (
                            row.venture_id, venture.workspace_id) and dossier.state in {"complete", "partial"})
                        if dossier:
                            result["dossier_id"] = dossier.id
                    elif job.kind == "idea.clarify_turn":
                        clarification = session.scalar(select(IdeaClarificationRun).where(
                            IdeaClarificationRun.job_id == job.id, IdeaClarificationRun.venture_id == row.venture_id,
                            IdeaClarificationRun.workspace_id == venture.workspace_id,
                            IdeaClarificationRun.state == "completed"))
                        valid = valid and clarification is not None
                        if clarification:
                            result["clarification_run_id"] = clarification.id
                    elif job.kind == "pagebuilder.build":
                        from app.pagebuilder.api import _view

                        try:
                            view = _view(job)
                            valid = valid and view["state"] == "ready" and bool(view.get("preview_url"))
                        except (ValueError, TypeError, KeyError):
                            valid = False
                for record in result.get("records", []):
                    model = getattr(supymem_models, record["model"], None) or getattr(models, record["model"], None)
                    effect = session.get(model, record["id"]) if model else None
                    valid = valid and bool(effect and effect.venture_id == row.venture_id
                                           and effect.workspace_id == venture.workspace_id)
                for identifier in result.get("documents", []):
                    document = session.get(VentureDocument, identifier)
                    valid = valid and bool(document and document.venture_id == row.venture_id)
                status, message = ("done", "Done, the saved job completed. Review its result.") if valid else (
                    "needs_you", "The job ended but its saved result is unavailable. Review before retrying.")
            row.status = status
            row.result_ref = json.dumps({**result, "message": message, "outcome": status}, default=str)
        session.commit()


def list_for(workspace_ids: set[str], statuses: tuple[str, ...] = ("new", "saved"), limit: int = 50,
             *, venture_id: str | None = None, offset: int = 0) -> dict[str, Any]:
    from app.core.local_capabilities import UNAVAILABLE, capability

    hidden = [action for name, item in UNAVAILABLE.items() if capability(name)["state"] == "not_in_this_version"
              for action in item["surfaces"].get("actions", ())]
    with db()() as s:
        vids = set(s.scalars(select(Venture.id).where(Venture.workspace_id.in_(workspace_ids))))
    _expire_stale({venture_id} & vids if venture_id else vids)
    _reconcile({venture_id} & vids if venture_id else vids)
    with db()() as s:
        scope = [Venture.workspace_id.in_(workspace_ids), Venture.archived_at.is_(None),
                 Venture.stage.not_in(("archived", "purged"))]
        if venture_id is not None:
            scope.append(Suggestion.venture_id == venture_id)
        if hidden:
            scope.append(Suggestion.action_kind.not_in(hidden))
        q = (select(Suggestion, Venture.name).join(Venture, Venture.id == Suggestion.venture_id)
             .where(*scope, Suggestion.status.in_(statuses))
             .order_by(Suggestion.created_at.desc(), Suggestion.id).offset(offset).limit(limit + 1))
        items = [public(r, name) for r, name in s.execute(q).all()]
        counts = dict(s.execute(select(Suggestion.status, func.count(Suggestion.id)).join(
            Venture, Venture.id == Suggestion.venture_id).where(
            *scope, Suggestion.status.in_(("new", "saved")))
            .group_by(Suggestion.status)).all())
    return {"items": items[:limit], "new": counts.get("new", 0), "saved": counts.get("saved", 0),
            "next_offset": offset + limit if len(items) > limit else None}


def get(sid: str) -> Suggestion | None:
    with db()() as s:
        return s.get(Suggestion, sid)


def mark_shown(ids: list[str]) -> None:
    for sid in ids[:10]:
        if row := get(sid):
            _emit("shown", row)


# ------------------------------------------------------------------ decide

def _set(sid: str, expect: tuple[str, ...], status: str, result_ref: str | None = None) -> bool:
    with db()() as s:
        done = s.execute(update(Suggestion).where(Suggestion.id == sid, Suggestion.status.in_(expect)).values(
            status=status, decided_at=utcnow(), **({"result_ref": result_ref} if result_ref else {})))
        s.commit()
        return bool(done.rowcount)


def save(sid: str) -> bool:
    ok = _set(sid, ("new",), "saved")
    if ok and (row := get(sid)):
        _emit("saved", row)
    return ok


def dismiss(sid: str, feedback: str = "") -> bool:
    note = feedback if feedback in FEEDBACK else ""
    ok = _set(sid, ("new", "saved"), "dismissed", f"dismissed:{note}" if note else None)
    if ok and (row := get(sid)):
        _emit("dismissed", row, feedback=note or "none")
    return ok


def _tell(venture_id: str, text: str, actions: list | None = None) -> None:
    from app.chat.store import append_message
    from app.org.characters import stamp_speaker
    from app.org.manager import manager_speaker

    try:
        append_message(venture_id, role="assistant", body_md=stamp_speaker(text, manager_speaker(venture_id)),
                       actions=actions or None)
    except Exception:  # noqa: BLE001 - a chat hiccup never undoes work
        log.exception("suggestions_chat_failed")


def _fail(row: Suggestion, code: str, body: str) -> None:
    from app.analytics.error_events import log_error_event
    from app.chat import recovery

    _set(row.id, ("executing",), "needs_you", json.dumps({
        "message": body + " Review the saved outcome before retrying; it could not be verified.",
        "href": f"/v/{row.venture_id}/now", "code": code[:60], "outcome": "needs_you"}))
    _emit("failed", row, code=code[:60])
    log_error_event("suggestions", feature="suggestions", code=code, venture_id=row.venture_id, person_id=row.person_id)
    recovery.post_failure(row.venture_id, step=row.action_kind, key=f"suggestion:{row.id}", body_md=body,
                          kind="suggestion_failed", code=code)


def _dispatch_payload(row: Suggestion) -> dict[str, Any]:
    payload = dict(json.loads(row.payload_json or "{}"))
    if row.action_kind == "outreach.draft":
        # A Suggestions draft card never approves future delivery. Keep its
        # outputs held even when the venture already allows automatic sends.
        # This source-owned decision cannot be weakened by the saved payload.
        payload["review_only"] = True
    return payload


async def _accept_with_god(god: Any, row: Suggestion, actor: str | None) -> dict[str, Any]:
    try:
        res = await god.execute_proposed_action(row.venture_id, {"kind": row.action_kind, "title": row.title,
                                                                 "payload": _dispatch_payload(row)},
                                                actor or row.person_id, approved_ceiling_micro=row.est_credits * MICRO_PER_CREDIT)
    except Exception as exc:  # noqa: BLE001
        log.exception("suggestion_failed", extra={"suggestion_id": row.id})
        _fail(row, f"execute_failed:{exc.__class__.__name__}", f"I could not finish \"{row.title}\".")
        return public(get(row.id))
    effects = res.get("effects") or {}
    reason = res.get("reason")
    completed = (res.get("completion_verified") is True and row.action_kind in res.get("done", [])
                 and reason in {None, ""})
    if completed:
        status, message = "done", "Done"
    elif effects.get("jobs") and reason == "queued":
        status, message = "running", "Running"
    elif reason == "noop":
        status, message = "noop", "Already done, nothing to do"
    else:
        status, message = "needs_you", "Needs you"
        if reason == "approved_credit_ceiling":
            effects["message"] = "Approve the exact current credit quote before any additional work."
        elif not effects.get("message"):
            effects["message"] = "Review the current saved outcome before retrying; completion could not be verified."
    href = _result_href(row)
    result = {**effects, "href": href, "message": effects.get("message") or message, "outcome": status}
    row.status = status
    result["message"] = _result_summary(row, result)
    _set(row.id, ("executing",), status, json.dumps(result, sort_keys=True, default=str))
    _tell(row.venture_id, f"{message}: {row.title}. {result['message']} [Open result or progress]({href})")
    _emit("executed", row, outcome=status)
    return public(get(row.id))


async def accept(sid: str, actor: str | None, *, caller=None) -> dict[str, Any]:
    """Run the card now. Raises KeyError if missing, ValueError('already_decided') if not open."""
    row = get(sid)
    if row is None:
        raise KeyError(sid)
    if row.action_kind == "browser.task.prepare":
        from app.build.device_orders import _scope
        if caller is None:
            raise PermissionError("authenticated_device_required")
        _scope(row.venture_id, caller)
    _expire_stale({row.venture_id})
    row = get(sid)
    if row.status == "expired":
        message = "Your saved venture inputs or work changed since this review. Review a fresh suggestion before I continue."
        _set(sid, ("expired",), "noop", json.dumps({"message": message,
                                                   "href": f"/v/{row.venture_id}/suggestions"}))
        row = get(sid)
        _tell(row.venture_id, message, [{"label": "Review fresh suggestions", "href": f"/v/{row.venture_id}/suggestions"}])
        return public(row)
    if not _set(sid, ("new", "saved"), "executing"):
        raise ValueError("already_decided")
    row = get(sid)
    _emit("accepted", row)
    if row.action_kind == "browser.task.prepare":
        from app.build import device_orders
        payload = json.loads(row.payload_json or "{}")
        try:
            goal = f"Visit {payload['url']} and read only: summarize the published pricing and features."
            prepared = device_orders.prepare_browser_task(row.venture_id, caller, goal)
            if not device_orders.general_browser_read_only(prepared["manifest"]):
                raise ValueError("device_browser_ai_request_invalid")
            from app.chat.store import append_message
            append_message(row.venture_id, role="user", body_md=goal,
                           idempotency_key=f"mac-review:{prepared['id']}:user")
        except Exception:
            _fail(row, "browser_review_unconfirmed", "I could not confirm this browser review. Open Browser tasks in Settings and check saved Mac tasks before trying again.")
            return public(get(row.id))
        _set(row.id, ("executing",), "done", json.dumps({"message": "Browser task prepared for review; nothing has run.",
            "href": prepared["review_path"], "outcome": "done"}))
        _tell(row.venture_id, "Your read-only browser task is ready for review. Nothing runs until you approve and press Start in the Mac app.",
            [{"label": "Review browser task", "href": prepared["review_path"], "style": "primary"}])
        return public(get(row.id))
    from app.autopilot.outreach import held_review_drafts

    with held_review_drafts(row.venture_id):
        return await _execute_accepted(row, actor)


async def _execute_accepted(row: Suggestion, actor: str | None) -> dict[str, Any]:
    from app.autopilot import actions

    _tell(row.venture_id, f"On it: {row.title}. I will use at most {row.est_credits} credits.")
    return await _accept_with_god(proposals.god_actions() or actions, row, actor)
