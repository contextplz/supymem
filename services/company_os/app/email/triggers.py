"""Durable lifecycle events and the hourly, idempotent email schedule.

Product progress waits two hours so nearby milestones become one email. The
scheduler reads saved product state: an active founder or a worker restart can
never silently discard an event. All delivery policy stays in email.service.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.credits.policy import person_wallet_enabled
from app.db.models import (ChatConversation, ChatMessage, CreditAccount, FeedbackEvent, GtmTouch, Job,
                           Opportunity, Person, Venture, VentureDocument, Workspace)
from app.db.supymem_models import (BetaCode, BetaCodeRedemption, BuildRun, ClarifierTurn, Deployment, DesktopDevice,
                                  EmailMessage, EmailPreference, IdeaSession,
                                  NeedsYouRequest, ProblemReport, ResearchDossier, VentureWaitlistEntry)
from app.email.service import _key, last_active, send_email, template_enabled, utcnow
from app.state import db

log = logging.getLogger("app.email.triggers")
NUDGE_DAYS = (2, 5, 14, 30)
PROGRESS_WINDOW = timedelta(hours=2)
EVENT_LOOKBACK = timedelta(days=7)
_WHAT = {"plan_ready": "plan", "research_ready": "research", "site_live": "landing", "build_accepted": "build"}
_SCREEN = {"plan": "plan", "research": "research", "landing": "site", "build": "builds", "waitlist": "analytics"}
DIGEST_KINDS = (*_WHAT, "mac_task_finished", "waitlist_on")


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _loads(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _owner_and_name(venture_id: str) -> tuple[str | None, str]:
    with db()() as s:
        v = s.get(Venture, venture_id)
        ws = s.get(Workspace, v.workspace_id) if v and v.workspace_id else None
        if not v or v.archived_at or v.stage == "purged" or not ws or ws.archived_at:
            return None, ""
        return ws.owner_id, v.name


def _first_line(text: str) -> str:
    return (text or "").strip().split("\n", 1)[0].replace("**", "")[:300]


def on_milestone(venture_id: str, kind: str, key: str, body_md: str, *, human_reason: str | None = None) -> None:
    """Progress is reconciled from the committed chat row; human decisions queue immediately."""
    try:
        from app.needs_you.policy import HUMAN_ONLY
        if kind != "needs_you" or human_reason not in HUMAN_ONLY:
            return  # previews, internal failures and unaccepted builds are not value moments
        owner, name = _owner_and_name(venture_id)
        if owner:
            with db()() as s:
                source = _needs_source(s, s.get(Venture, venture_id), owner, key, require_card=True)
                detail = _first_line(s.get(ChatMessage, source["_needs_card_id"]).body_md) if source else ""
            if not source:
                return
            send_email(owner, "needs_you", {"reason": human_reason, "detail": detail,
                "venture_name": name, "path": f"/v/{venture_id}/now", **source}, ref=f"{venture_id}:{kind}:{key}")
    except Exception:  # email must never fail the journey or disclose personal exception content
        log.warning("email_milestone_trigger_failed")


def on_needs_you(venture_id: str | None, title: str, body: str, ref: str) -> None:
    try:
        if not venture_id:
            return
        owner, name = _owner_and_name(venture_id)
        low = f"{title} {body}".lower()
        reason = "captcha" if "captcha" in low else "approval" if "approv" in low else "blocked"
        if owner:
            with db()() as s:
                source = _needs_source(s, s.get(Venture, venture_id), owner, ref)
            if not source:
                return
            send_email(owner, "needs_you", {"reason": reason, "detail": _first_line(body),
                "venture_name": name, "path": f"/v/{venture_id}/now", **source}, ref=f"{venture_id}:{ref}")
    except Exception:
        log.warning("email_needs_you_trigger_failed")


def _admission_code(s, person_id: str) -> str | None:
    """Read the existing admission only; download eligibility does not consume an invite seat."""
    admission = s.get(BetaCodeRedemption, person_id)
    code = s.get(BetaCode, admission.code) if admission else None
    if code and code.active and len(code.code) == 8 and code.code.isascii() and code.code.isalnum() and code.code == code.code.upper():
        return code.code
    return None


def _welcome_context(s, person_id: str) -> dict:
    has_device = s.scalar(select(DesktopDevice.id).where(DesktopDevice.person_id == person_id,
        DesktopDevice.revoked_at.is_(None)).limit(1)) is not None
    code = _admission_code(s, person_id) if not has_device else None
    return {"path": "/new", "web_signup": bool(code), **({"admission_code": code} if code else {})}


def on_welcome(person_id: str) -> None:
    """Admission and the scheduler use the same key, including across restarts."""
    try:
        with db()() as s:
            ctx = _welcome_context(s, person_id)
        send_email(person_id, "welcome", ctx, ref="")
    except Exception:
        log.warning("email_welcome_trigger_failed")


def on_first_signup(venture_id: str) -> None:
    """Compatibility hook; the committed waitlist count is reconciled by the schedule."""
    on_waitlist_signup(venture_id)


def on_waitlist_signup(venture_id: str) -> None:
    """No recipient data crosses this hook. Saved, non-test entries are the only signal."""
    # Progress intentionally waits for the hourly scheduler's two-hour batch.
    # No transient in-memory timer or public signup request is allowed to send mail.
    return None


def on_credit_balance(account_id: str) -> None:
    """After settlement, with an hourly fallback covering pooled/reconciled spend too."""
    try:
        with db()() as s:
            account = s.get(CreditAccount, account_id)
            if person_wallet_enabled(s):
                if account and account.scope == "person" and account.owner_person_id and s.get(Person, account.owner_person_id):
                    _credits(account, account.owner_person_id, "Your account", send_email)
            elif account and account.scope == "venture" and account.venture_id:
                owner, name = _owner_and_name(account.venture_id)
                if owner:
                    _credits(account, owner, name, send_email)
    except Exception:
        log.warning("email_credit_trigger_failed")


def _pseudo(kind: str, email: str) -> str:
    return f"{kind}:{hashlib.sha256(email.encode()).hexdigest()[:20]}"


def _admin(template: str, ctx: dict, ref: str) -> None:
    # Internal lifecycle alerts have exactly one authorized recipient.
    from app.email.service import internal_recipient
    address = internal_recipient()
    if not address:
        return
    send_email(_pseudo("admin", address), template, ctx, ref=ref, to_email=address)


def on_god_mode_requested(venture_id: str, note: str = "") -> None:
    try:
        owner, name = _owner_and_name(venture_id)
        if not owner:
            return
        ctx = {"venture_name": name, "path": f"/v/{venture_id}/now", "detail": note[:400]}
        send_email(owner, "god_mode_requested", ctx, ref=f"god:{venture_id}")
        _admin("god_mode_admin", {"venture_name": name, "path": "/admin/god-mode"}, f"god:{venture_id}")
    except Exception:
        log.warning("email_god_mode_trigger_failed")


def send_invite(email: str, code: str, inviter: str = "The Supymem team") -> str | None:
    email = email.strip().lower()
    return send_email(_pseudo("invite", email), "beta_invite", {"code": code, "inviter": inviter}, ref=code, to_email=email)


def _visible(person_id: str):
    conversations = select(ChatConversation.id).where(
        (ChatConversation.visibility == "legacy_shared") | (ChatConversation.creator_person_id == person_id))
    return ChatMessage.conversation_id.is_(None) | ChatMessage.conversation_id.in_(conversations)


def _activity(s, person_id: str, since: datetime, until: datetime) -> list[str]:
    ventures = _owned_ventures(s, person_id)
    names = {v.id: v.name for v in ventures}
    if not names:
        return []
    rows = s.scalars(select(ChatMessage).where(
        ChatMessage.venture_id.in_(names), ChatMessage.role == "assistant", ChatMessage.kind.in_(DIGEST_KINDS),
        ChatMessage.created_at > since, ChatMessage.created_at <= until,
        _visible(person_id)).order_by(ChatMessage.created_at.desc()).limit(8)).all()
    items = [f"{names[r.venture_id]}: {_first_line(r.body_md)}" for r in rows]
    for venture in ventures:
        pending = 0
        for document in s.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture.id,
                VentureDocument.kind == "outreach_draft", VentureDocument.created_at > since,
                VentureDocument.created_at <= until)):
            draft = _loads(document.provenance_json)
            pending += int(draft.get("state") == "draft" and draft.get("channel") == "email"
                and draft.get("requires_batch_approval") is True and bool(draft.get("batch_id")))
        if pending:
            items.append(f"{venture.name}: {pending} outreach drafts await your approval; nothing sent automatically")
        touches = s.scalars(select(GtmTouch).where(GtmTouch.venture_id == venture.id,
            GtmTouch.workspace_id == venture.workspace_id, GtmTouch.sent_at > since,
            GtmTouch.sent_at <= until)).all()
        for direction, label in (("out", "approved outreach messages accepted by Gmail"), ("in", "replies received")):
            receipts = {t.message_id for t in touches if _real_touch(t, venture, direction)}
            if receipts:
                items.append(f"{venture.name}: {len(receipts)} {label}")
    from app.tasks.digest import digest_items
    items.extend(digest_items(s, person_id, since, until))
    return items


def _real_touch(touch, venture, direction) -> bool:
    return bool(touch and touch.venture_id == venture.id and touch.workspace_id == venture.workspace_id
        and not touch.simulated and touch.message_id and touch.sent_at and touch.direction == direction
        and touch.channel == "email" and (direction != "in" or touch.classification != "automated")
        and (touch.send_mode == "founder_gmail" if direction == "out" else touch.provider_thread_id))


def _weekly_learning(s, person_id: str, since: datetime, until: datetime) -> dict | None:
    from app.chat.next_moves import experiment_learning, experiments_autopilot_enabled
    from app.credits.units import format_credits
    from app.connections.revenue import founder_connection, snapshot, summary_lines

    if not experiments_autopilot_enabled():
        return None
    changes, ended, budgets, surprises = [], [], [], []
    decision = None
    spend, cost_count, result_count = 0, 0, 0
    revenue_count = 0
    revenue_workspaces, revenue_accounts = set(), set()
    labels = {"supported": "worked", "refuted": "didn't work", "inconclusive": "too early to judge"}
    for venture in _owned_ventures(s, person_id):
        revenue = None
        if venture.workspace_id not in revenue_workspaces:
            revenue_workspaces.add(venture.workspace_id)
            account = founder_connection(venture.workspace_id)
            account_key = (account["provider_user_id"], account["connected_account_id"]) if account else None
            if account_key and account_key not in revenue_accounts:
                revenue = snapshot(venture.id, until=until)
            lines = summary_lines(revenue)
            if lines:
                revenue_accounts.add(account_key)
                revenue_count += 1
                changes.extend(lines)
                if revenue.get("strength") == "cash":
                    decision = {"next_step": "Open Keys and connections and review your connected Stripe account. Check which tests led to your paying customers before you change direction.",
                                "path": "/connections"}
        learning = experiment_learning(venture.id)
        if not learning:
            continue
        results = {row["id"]: row for row in learning["results"]}
        recent = [row for row in results.values() if since < _naive(row["ended_at"]) <= until]
        if not recent:
            continue
        result_count += len(recent)
        for row in recent:
            cost = row.get("result", {}).get("actual_micro")
            if type(cost) is int and cost >= 0:
                spend += cost
                cost_count += 1
            if row["outcome"] == "refuted":
                ended.append(f"{venture.name}: {row['hypothesis']}")
        linked = set()
        for belief in learning["beliefs"]:
            history = sorted((item for item in belief.get("evidence", []) if item["experiment_id"] in results),
                key=lambda item: _naive(results[item["experiment_id"]]["ended_at"]))
            previous = None
            for item in history:
                linked.add(item["experiment_id"])
                row = results[item["experiment_id"]]
                outcome = row["outcome"]
                if since < _naive(row["ended_at"]) <= until and outcome != previous:
                    change = f"{venture.name}: {belief['statement']}: {labels[outcome]}"
                    changes.append(change)
                    if previous in {"supported", "refuted"} and outcome in {"supported", "refuted"}:
                        surprises.append(change)
                previous = outcome
        changes.extend(f"{venture.name}: {row['hypothesis']}: {labels[row['outcome']]}"
                       for row in recent if row["id"] not in linked)
        plan = learning["plan"]
        ceiling, reserved = plan.get("ceiling_micro"), plan.get("reserved_micro")
        if plan.get("approved_at") and type(ceiling) is int and type(reserved) is int and min(ceiling, reserved) >= 0:
            budget = f"{format_credits(max(0, ceiling - reserved))} credits not committed to tests"
            expired = plan.get("deadline") and _naive(datetime.fromisoformat(plan["deadline"])) <= until
            if plan.get("state") not in {"active", "budget_exhausted"} or expired:
                budget += "; new tests need your approval"
            budgets.append(f"{venture.name}: {budget}")
        else:
            budgets.append(f"{venture.name}: you have no recorded approved test budget")
        recommendation = learning["decision"]
        if recommendation and decision is None:
            decision = {"next_step": f"{recommendation['title']}. Why: {recommendation['why']}",
                        "path": f"/v/{venture.id}/market-map", "_venture_id": venture.id}
        elif decision is None:
            decision = {"next_step": "Keep testing your current direction. Why: this week's results are from an earlier direction.",
                        "path": f"/v/{venture.id}/market-map", "_venture_id": venture.id}
    if not result_count and not revenue_count:
        return None
    return {**(decision or {}), "changes": changes, "ended_tests": ended, "budgets": budgets,
        "surprise": surprises[-1] if surprises else "No recorded result reversed an earlier result this week.",
        "recorded_spend": (f"{format_credits(spend)} credits" if cost_count else "No test costs were recorded")
            + ("; some test costs are not recorded" if 0 < cost_count < result_count else "")}


def _signup_count(s, ventures, since, until) -> int:
    if not ventures:
        return 0
    return s.scalar(select(func.count()).select_from(VentureWaitlistEntry).join(Venture,
        Venture.id == VentureWaitlistEntry.venture_id).where(Venture.id.in_([v.id for v in ventures]),
        Venture.workspace_id == VentureWaitlistEntry.workspace_id, VentureWaitlistEntry.is_test.is_(False),
        VentureWaitlistEntry.created_at > since, VentureWaitlistEntry.created_at <= until)) or 0


def _suggestions(s, person_id: str, since: datetime, until: datetime) -> list[tuple[str, str]]:
    owned = select(Workspace.id).where(Workspace.owner_id == person_id, Workspace.archived_at.is_(None))
    rows = s.execute(select(Opportunity.id, Opportunity.title).where(
        Opportunity.workspace_id.in_(owned), Opportunity.status == "proposed",
        Opportunity.created_at > since, Opportunity.created_at <= until
        ).order_by(Opportunity.created_at.desc()).limit(5)).all()
    return [(r[0], r[1]) for r in rows]


def _has(s, person_id: str, template: str, ref: str | None = None) -> bool:
    query = select(EmailMessage.id).where(EmailMessage.person_id == person_id, EmailMessage.template == template)
    if ref is not None:
        query = query.where(EmailMessage.idempotency_key == _key(person_id, template, ref))
    return s.scalar(query.limit(1)) is not None


def _credits(account, person_id, name, go):
    granted, spent = account.granted_micro, account.spent_micro
    if account.status != "active" or granted <= 0:
        return
    percent = next((n for n in (100, 95, 80) if spent * 100 >= granted * n), None)
    if percent:
        wallet = account.scope == "person"
        if wallet and account.owner_person_id != person_id:
            return
        go(person_id, "credits_exhausted" if percent == 100 else "credits_low",
            {"venture_name": name, "percent": percent, "_account_id": account.id,
             "path": "/account" if wallet else f"/v/{account.venture_id}/settings"},
            ref=f"{account.id}:{granted}:{percent}")


def _next_step(venture, person_id: str) -> dict:
    """Read current guidance for the intended recipient of an owned-venture email."""
    from fastapi import HTTPException
    from app.authz.workspace import assert_venture_access, subject_person_id
    from app.tenancy import Scope, bind, current_or_none
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.ventures.next_step import next_step

    fallback = {"venture_name": "Your venture", "next_step": "Review your venture",
                "detail": "Open the venture's saved work to see the next available action.",
                "cta_label": "Open saved work", "path": "/"}
    existing = current_or_none()
    scheduler = Scope("none", "", "system")
    if (not isinstance(person_id, str) or not person_id or subject_person_id(person_id) != person_id or
            person_id.casefold() in {"system", "anonymous", "local", "founder", LOCAL_PERSON_ID.casefold()} or
            existing is not None and existing != scheduler and (
                existing.workspace_id != venture.workspace_id or
                existing.venture_id not in {"", venture.id} or
                subject_person_id(existing.actor) != person_id)):
        return fallback
    with db()() as s:
        # Match the stage worker's custody order and keep the recipient stable through the read.
        current = s.scalar(select(Venture).where(Venture.id == venture.id).with_for_update())
        workspace = s.scalar(select(Workspace).where(Workspace.id == venture.workspace_id).with_for_update())
        if (workspace is None or workspace.archived_at is not None or workspace.owner_id != person_id or
                current is None or current.workspace_id != workspace.id or current.archived_at is not None or
                current.stage in {"archived", "purged"} or s.get(Person, person_id) is None):
            return fallback
        try:
            assert_venture_access(current.id, person_id)
        except HTTPException:
            return fallback
        with bind(workspace_id=workspace.id, venture_id=current.id, actor=person_id):
            step = next_step(current.id)
        return {"venture_name": current.name, "next_step": step["title"], "detail": step["detail"],
                "cta_label": step["cta_label"], "path": step["href"]}


def _owned_ventures(s, person_id):
    return s.scalars(select(Venture).join(Workspace, Workspace.id == Venture.workspace_id).where(
        Workspace.owner_id == person_id, Workspace.archived_at.is_(None),
        Venture.archived_at.is_(None), Venture.stage != "purged")
        .order_by(Venture.created_at, Venture.id)).all()


def _context_venture(ctx):
    if ctx.get("_venture_id"):
        return ctx["_venture_id"]
    path = ctx.get("path", "")
    return path.split("/", 3)[2].split("?", 1)[0].split("#", 1)[0] if isinstance(path, str) and path.startswith("/v/") else None


def _source_card(s, venture, owner, key, kind, card_id=None):
    if not venture or not key:
        return None
    query = select(ChatMessage).where(ChatMessage.venture_id == venture.id,
        (ChatMessage.workspace_id == venture.workspace_id) | ChatMessage.workspace_id.is_(None),
        ChatMessage.role == "assistant", ChatMessage.kind == kind, _visible(owner))
    if card_id:
        query = query.where(ChatMessage.id == card_id)
    return next((card for card in s.scalars(query.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()))
        if card.idempotency_key and _source_matches(s, venture, card, key)), None)


def _request_open(request, venture, now):
    return bool(request and venture and request.venture_id == venture.id
        and request.workspace_id == venture.workspace_id and request.status == "open"
        and (request.deadline is None or _naive(request.deadline) > now))


def _needs_source(s, venture, owner, key, *, require_card=False):
    request_key = key.startswith("needs_you:") or ":remind:" in key
    request_id = key.split(":", 2)[1] if key.startswith("needs_you:") else key.split(":", 1)[0]
    request = s.get(NeedsYouRequest, request_id)
    if request_key or request:
        if not _request_open(request, venture, utcnow()):
            return None
        card = _source_card(s, venture, owner, key, "needs_you") if require_card else None
        if require_card and not card:
            return None
        return {"_needs_request_id": request.id,
            **({"_needs_card_id": card.id, "_needs_key": key} if card else {})}
    card = _source_card(s, venture, owner, key, "needs_you")
    if not card:
        return None
    requests = [a.get("payload", {}).get("request_id") for a in _actions(card)
        if a.get("action") == "needs_you.resolve" and isinstance(a.get("payload"), dict)]
    if requests and (len(requests) != 1 or not _request_open(s.get(NeedsYouRequest, requests[0]), venture, utcnow())):
        return None
    return {"_needs_card_id": card.id, "_needs_key": key,
        **({"_needs_request_id": requests[0]} if requests else {})}


def _decision_current(s, venture, owner, ctx):
    from app.chat.failed_jobs import FAILED, KINDS
    model = {"job": Job, "build": BuildRun, "deployment": Deployment}.get(ctx.get("_failure_type"))
    if not venture or not model or not ctx.get("_failure_id") or not ctx.get("_failure_card_id"):
        return False
    row = s.get(model, ctx["_failure_id"])
    if not row or (row.venture_id, row.workspace_id) != (venture.id, venture.workspace_id):
        return False
    query = select(model.id).where(model.venture_id == venture.id, model.workspace_id == venture.workspace_id)
    ordering = Deployment.version if model is Deployment else model.created_at
    if model is Job:
        if row.kind not in KINDS or row.status not in FAILED or row.kind != ctx.get("_failure_kind") or row.attempts != ctx.get("_failure_attempts"):
            return False
        query = query.where(Job.kind == row.kind)
        card_kind = f"{KINDS[row.kind][2]}_failed"
        key, path = f"{card_kind}:{row.id}", f"/v/{venture.id}/{KINDS[row.kind][2]}"
    elif row.state != "failed" or (model is BuildRun and (row.runtime_version != 1 or not row.closed_at)):
        return False
    elif model is BuildRun:
        from app.chat.milestone_hooks import _build_href
        from app.planning.publication_evidence import landing_build_result
        landing = landing_build_result(s, row, _loads(row.spec))
        if landing and landing["available"]:
            return False
        card_kind, key, path = "build_failed", f"build_failed:{row.id}", _build_href(row)
    else:
        card_kind, key, path = "site_publish_failed", f"deploy_failed:{row.id}", f"/v/{venture.id}/site"
    if (ctx.get("_failure_card_kind"), ctx.get("_failure_key"), ctx.get("path")) != (card_kind, key, path):
        return False
    if s.scalar(query.order_by(ordering.desc(), model.id.desc()).limit(1)) != row.id:
        return False
    card = _source_card(s, venture, owner, ctx.get("_failure_key"), ctx.get("_failure_card_kind"), ctx["_failure_card_id"])
    if not card:
        return False
    actions = _actions(card)
    if model is Job:
        return any(a.get("action") == "job.retry" and isinstance(a.get("payload"), dict) and a["payload"].get("kind") == row.kind for a in actions)
    from app.chat.navigation_map import hidden_href
    # Chat-first saves a hidden-page link as its in-chat action, so the card's button is that action, not the href.
    swap = hidden_href(ctx.get("path"))
    return any(a.get("href") == ctx.get("path") or (swap is not None and not a.get("href") and a.get("action") == swap["action"])
               for a in actions)


def refresh_context(s, person_id: str, template: str, ctx: dict) -> dict | None:
    """Recheck queued eligibility; frozen retries preserve eligible original copy."""
    try:
        return _refresh_context(s, person_id, template, dict(ctx))
    except Exception:
        log.warning("email_context_refresh_failed")
        return None


def _refresh_context(s, person_id, template, ctx):
    if template in {"venture_export_prepared", "venture_archived"}:
        from app.email.venture_receipts import refresh_context

        return refresh_context(s, person_id, template, ctx)
    now, frozen = utcnow(), "_email_delivery_payload" in ctx
    if ctx.get("_eligible_until") and now >= _naive(datetime.fromisoformat(ctx["_eligible_until"])):
        return None
    ventures = _owned_ventures(s, person_id)
    owned = {v.id: v for v in ventures}
    if template in {"digest_daily", "weekly_insights", "weekly_learning", "suggestions_waiting"}:
        if template == "weekly_learning":
            from app.chat.next_moves import experiments_autopilot_enabled

            if not experiments_autopilot_enabled():
                return None
        workspaces = set(s.scalars(select(Workspace.id).where(Workspace.owner_id == person_id,
            Workspace.archived_at.is_(None))))
        if frozen:
            return ctx if (ctx.get("_workspace_ids") and set(ctx["_workspace_ids"]) <= workspaces
                and ctx.get("_venture_ids") is not None and set(ctx["_venture_ids"]) <= set(owned)) else None
        days = 1 if template == "digest_daily" else 7
        since = _naive(datetime.fromisoformat(ctx["_activity_since"])) if ctx.get("_activity_since") else now - timedelta(days=days)
        if template == "weekly_learning":
            until = min(now, _naive(datetime.fromisoformat(ctx["_activity_until"])))
            learning = _weekly_learning(s, person_id, since, until)
            if not learning:
                return None
            return {**ctx, **learning, "items": _activity(s, person_id, since, until),
                    "_workspace_ids": sorted(workspaces), "_venture_ids": sorted(owned)}
        if template == "suggestions_waiting":
            suggestions = _suggestions(s, person_id, since, now)
            if not suggestions:
                return None
            return {**ctx, "items": [title for _, title in suggestions], "count": len(suggestions),
                    "_workspace_ids": sorted(workspaces), "_venture_ids": sorted(owned)}
        items = _activity(s, person_id, since, now)
        signups = _signup_count(s, ventures, since, now)
        if not items and not signups:
            return None
        if template == "digest_daily" and signups:
            items.append(f"{signups} waitlist signups: unverified interest")
        return {**ctx, **_next_step(ventures[-1], person_id), "items": items, "signup_count": signups,
                "_workspace_ids": sorted(workspaces), "_venture_ids": sorted(owned), "_venture_id": ventures[-1].id}
    venture_id = _context_venture(ctx)
    venture = owned.get(venture_id) if venture_id else None
    if venture_id and not venture:
        return None
    if venture and not frozen:
        ctx["venture_name"] = venture.name
    if template == "needs_you":
        if not venture or not (ctx.get("_needs_request_id") or ctx.get("_needs_card_id")):
            return None  # Legacy notices without exact saved evidence cannot establish a pending request.
        if ctx.get("_needs_request_id") and not _request_open(s.get(NeedsYouRequest, ctx["_needs_request_id"]), venture, now):
            return None
        if ctx.get("_needs_card_id") and not _source_card(s, venture, person_id, ctx.get("_needs_key"), "needs_you", ctx["_needs_card_id"]):
            return None
    if template == "action_failed" and not _decision_current(s, venture, person_id, ctx):
        return None
    if ctx.get("_activity_baseline"):
        active = last_active(s, person_id)
        if active and _naive(active) > _naive(datetime.fromisoformat(ctx["_activity_baseline"])):
            return None
    if template == "finish_setup" and ventures:
        return None
    if template == "get_mac_app":
        if not ctx.get("admission_code") or s.scalar(select(DesktopDevice.id).where(DesktopDevice.person_id == person_id,
                DesktopDevice.revoked_at.is_(None)).limit(1)) or _admission_code(s, person_id) != ctx.get("admission_code"):
            return None
    if template == "welcome":
        if frozen and ctx.get("admission_code") and _admission_code(s, person_id) != ctx["admission_code"]:
            return None
        if not frozen:
            ctx.pop("admission_code", None)
            ctx.update(_welcome_context(s, person_id))
    if ctx.get("_idea_session"):
        idea = s.get(IdeaSession, ctx["_idea_session"])
        if not idea or idea.state in {"confirmed", "validating", "abandoned"} or not timedelta(days=1) <= now - _naive(idea.updated_at) < EVENT_LOOKBACK:
            return None
        turn = s.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1))
        if not turn or turn.role != "agent" or turn.kind != "question":
            return None
        if not frozen:
            ctx["detail"] = _first_line(turn.text)
    if ctx.get("_research_job"):
        job = s.get(Job, ctx["_research_job"])
        if not job or job.status not in {"running", "leased"}:
            return None
    if template in {"credits_low", "credits_exhausted"}:
        if template == "credits_low":
            return None
        if person_wallet_enabled(s):
            credit = s.get(CreditAccount, ctx.get("_account_id")) if ctx.get("_account_id") else None
            if not credit or credit.scope != "person" or credit.owner_person_id != person_id:
                return None
            if not frozen:
                ctx.update(path="/account", venture_name="Your account")
            elif ctx.get("path") != "/account":
                return None
        else:
            if not venture:
                return None
            credit = s.scalar(select(CreditAccount).where(CreditAccount.venture_id == venture.id,
                CreditAccount.workspace_id == venture.workspace_id, CreditAccount.scope == "venture"))
            if ctx.get("_account_id") and (not credit or credit.id != ctx["_account_id"]):
                return None
        if not credit or credit.status != "active" or credit.granted_micro <= 0 or credit.spent_micro * 100 < credit.granted_micro * int(ctx.get("percent") or 100):
            return None
    if template == "outreach_drafts_ready":
        if not venture or not ctx.get("_batch_id"):
            return None
        ids = []
        for document in s.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture.id,
                VentureDocument.kind == "outreach_draft")):
            draft = _loads(document.provenance_json)
            if draft.get("batch_id") == ctx["_batch_id"] and draft.get("state") == "draft" and draft.get("channel") == "email" and draft.get("requires_batch_approval") is True:
                ids.append(document.id)
        if not ids or (frozen and (not ctx.get("_draft_ids") or not set(ctx["_draft_ids"]) <= set(ids))):
            return None
        if not frozen:
            ctx.update(count=len(ids), _draft_ids=sorted(ids))
    if template in {"outreach_sent", "replies_received"}:
        if not venture or not ctx.get("source_ids"):
            return None
        direction = "out" if template == "outreach_sent" else "in"
        for source in ctx["source_ids"]:
            touch = s.get(GtmTouch, source.removeprefix("touch:"))
            if not _real_touch(touch, venture, direction):
                return None
    if not frozen and venture and template in {"nudge_d2", "nudge_d5", "nudge_d14", "nudge_d30"}:
        ctx.update(_next_step(venture, person_id))
    return ctx


def _actions(row) -> list[dict]:
    try:
        parsed = json.loads(row.actions_json or "[]") if row else []
        return [a for a in parsed if isinstance(a, dict)] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _review(spec: dict) -> dict:
    lifecycle = spec.get("lifecycle")
    review = lifecycle.get("review") if isinstance(lifecycle, dict) else None
    return review if isinstance(review, dict) else {}


def _source_matches(s, venture, row, event_key: str) -> bool:
    if not row.idempotency_key:
        return True  # legacy chat rows predate the source-key contract
    identity = [venture.id, "assistant", event_key]
    conversation = s.get(ChatConversation, row.conversation_id) if row.conversation_id else None
    if conversation and conversation.visibility == "private":
        identity.append(conversation.id)
    expected = "chat:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return row.idempotency_key == expected


def _task_path(row, venture_id: str) -> str:
    try:
        for action in _actions(row):
            path = action.get("href", "")
            if isinstance(path, str) and ".." not in path and (path.startswith(f"/v/{venture_id}/device-work-orders/") or path.startswith(f"/v/{venture_id}/builds")):
                return path
    except (TypeError, ValueError, AttributeError):
        pass
    return f"/v/{venture_id}/tasks"


def _progress(s, venture, owner, now, go) -> None:
    """Replay recent, committed progress visible to the recipient; batch one cluster."""
    since = now - EVENT_LOOKBACK
    previous = s.scalars(select(EmailMessage).where(EmailMessage.person_id == owner,
        EmailMessage.template.in_(("milestone", "progress_summary", "task_finished", "task_failed")),
        EmailMessage.created_at > since)).all()
    done = {source for row in previous for source in _loads(row.context_json).get("source_ids", [])}
    records = []
    for row in s.scalars(select(ChatMessage).where(
            ChatMessage.venture_id == venture.id, ChatMessage.role == "assistant", ChatMessage.kind.in_(_WHAT),
            ChatMessage.created_at > since, ChatMessage.created_at <= now,
            _visible(owner)).order_by(ChatMessage.created_at, ChatMessage.id)):
        source = f"chat:{row.id}"
        if source in done:
            continue
        what = _WHAT[row.kind]
        # Do not replay a pre-upgrade event already covered by the old direct hook.
        if any(not _loads(p.context_json).get("source_ids") and _loads(p.context_json).get("what") == what
               and str(_loads(p.context_json).get("path", "")).startswith(f"/v/{venture.id}/")
               and _naive(p.created_at) >= _naive(row.created_at) for p in previous):
            continue
        ctx = {"what": what, "venture_name": venture.name, "detail": _first_line(row.body_md),
               "path": f"/v/{venture.id}/{_SCREEN[what]}"}
        if what == "research":
            dossier = s.scalar(select(ResearchDossier).where(ResearchDossier.venture_id == venture.id,
                ResearchDossier.workspace_id == venture.workspace_id, ResearchDossier.kind == "deep")
                .order_by(ResearchDossier.created_at.desc()).limit(1))
            if not dossier or dossier.state not in {"complete", "partial"} or not _source_matches(s, venture, row, f"research_done:{dossier.id}"):
                continue
            from app.research.service import get_dossier
            verdict = get_dossier(dossier.id)["summary"]["verdict"]
            ctx["items"] = (verdict["why"] or verdict["why_not"])[:3]
            ctx["verdict"] = verdict["title"]
        elif what == "build":
            run = s.scalar(select(BuildRun).where(BuildRun.venture_id == venture.id,
                BuildRun.workspace_id == venture.workspace_id, BuildRun.runtime_version == 1)
                .order_by(BuildRun.created_at.desc(), BuildRun.id).limit(1))
            spec = _loads(run.spec) if run else {}
            if not (run and run.state == "succeeded" and run.closed_at and spec.get("qa_state") == "passed"
                    and _review(spec).get("status") == "accepted"
                    and _source_matches(s, venture, row, f"build_accepted:{run.id}")):
                continue
            from app.chat.milestone_hooks import _build_href
            ctx["path"] = _build_href(run)
        elif what == "landing":
            from app.planning.publication_evidence import public_site_evidence
            evidence = public_site_evidence(s, workspace_id=venture.workspace_id, venture_id=venture.id)
            if not evidence or (evidence["source"] == "deployment" and not _source_matches(s, venture, row, f"deploy_live:{evidence['id']}")):
                continue
            ctx["url"] = evidence["url"]
        records.append((source, _naive(row.created_at), "milestone", ctx))
    entries = s.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture.id,
        VentureWaitlistEntry.workspace_id == venture.workspace_id, VentureWaitlistEntry.is_test.is_(False))
        .order_by(VentureWaitlistEntry.created_at, VentureWaitlistEntry.id).limit(100)).all()
    for threshold in (1, 10, 50, 100):
        source = f"waitlist:{venture.id}:{threshold}"
        if len(entries) < threshold or source in done:
            continue
        at = _naive(entries[threshold - 1].created_at)
        if since < at <= now:
            records.append((source, at, "milestone", {"what": "waitlist", "count": threshold,
                "venture_name": venture.name, "path": f"/v/{venture.id}/analytics"}))
    # Mac task cards are durable completion receipts; private conversations stay private.
    for row in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture.id,
            ChatMessage.kind.in_(("mac_task_finished", "mac_task_stopped", "build_ready")), ChatMessage.role == "assistant",
            _visible(owner), ChatMessage.created_at > since,
            ChatMessage.created_at <= now).order_by(ChatMessage.created_at, ChatMessage.id)):
        source = f"chat:{row.id}"
        if row.kind == "build_ready":
            pending = s.scalar(select(BuildRun).where(BuildRun.venture_id == venture.id,
                BuildRun.workspace_id == venture.workspace_id, BuildRun.runtime_version == 1)
                .order_by(BuildRun.created_at.desc(), BuildRun.id).limit(1))
            spec = _loads(pending.spec) if pending else {}
            if not (pending and pending.state == "succeeded" and pending.closed_at
                    and spec.get("qa_state") == "passed" and _review(spec).get("status") == "pending"
                    and _source_matches(s, venture, row, f"build_ready:{pending.id}")):
                continue
        if source not in done:
            records.append((source, _naive(row.created_at), "task_failed" if row.kind == "mac_task_stopped" else "task_finished",
                {"venture_name": venture.name, "detail": _first_line(row.body_md), "path": _task_path(row, venture.id)}))
    if not records:
        return
    records.sort(key=lambda r: (r[1], r[0]))
    cluster = [r for r in records if r[1] <= records[0][1] + PROGRESS_WINDOW]
    if cluster[-1][1] + PROGRESS_WINDOW > now:
        return
    if len(cluster) > 1 and not template_enabled("progress_summary", owner):
        enabled = [r for r in cluster if template_enabled(r[2], owner)]
        if not enabled:
            return
        cluster = [enabled[-1]]  # rollout keeps legacy mail; only this conveyed event is acknowledged
    sources = [r[0] for r in cluster]
    if len(cluster) == 1:
        template, ctx = cluster[0][2], cluster[0][3]
    else:
        template = "progress_summary"
        ctx = {"venture_name": venture.name, "items": [r[3].get("detail") or
            (f"{r[3]['count']} waitlist signups: unverified interest" if "count" in r[3]
             else "Your saved work has an update") for r in cluster],
            "path": cluster[-1][3]["path"], "cta_label": "View progress"}
    go(owner, template, {**ctx, "source_ids": sources},
       ref="progress:" + hashlib.sha256(json.dumps(sources).encode()).hexdigest()[:24])


def _decisions(s, venture, owner, now, go) -> None:
    """Only the current terminal failure with a saved founder decision gets a receipt."""
    from app.chat.failed_jobs import FAILED, KINDS, latest_failed
    since = now - EVENT_LOOKBACK
    for kind, (_, _, screen) in KINDS.items():
        failed = latest_failed(venture.id, venture.workspace_id, kind)
        job = s.get(Job, failed["job_id"]) if failed else None
        if not job or job.status not in FAILED or not since < _naive(job.created_at) <= now:
            continue
        key = f"{screen}_failed:{job.id}"
        card = _source_card(s, venture, owner, key, f"{screen}_failed")
        if card and _naive(job.created_at) <= _naive(card.created_at) <= now:
            ctx = {"venture_name": venture.name, "detail": _first_line(card.body_md),
                "path": f"/v/{venture.id}/{screen}", "cta_label": "Review and retry",
                "_failure_type": "job", "_failure_id": job.id, "_failure_kind": kind, "_failure_attempts": job.attempts,
                "_failure_card_id": card.id, "_failure_card_kind": card.kind, "_failure_key": key}
            if _decision_current(s, venture, owner, ctx):
                go(owner, "action_failed", ctx, f"{venture.id}:job:{job.id}")
    for model, state, card_kind, screen, ordering in (
            (BuildRun, "failed", "build_failed", "builds", BuildRun.created_at),
            (Deployment, "failed", "site_publish_failed", "site", Deployment.version)):
        latest = s.scalar(select(model).where(model.venture_id == venture.id,
            model.workspace_id == venture.workspace_id).order_by(ordering.desc(), model.id.desc()).limit(1))
        if not latest or latest.state != state:
            continue
        if model is BuildRun and (latest.runtime_version != 1 or not latest.closed_at):
            continue
        key = f"build_failed:{latest.id}" if model is BuildRun else f"deploy_failed:{latest.id}"
        card = _source_card(s, venture, owner, key, card_kind)
        if card and since < _naive(card.created_at) <= now:
            path = f"/v/{venture.id}/{screen}"
            if model is BuildRun:
                from app.chat.milestone_hooks import _build_href
                path = _build_href(latest)
            ctx = {"venture_name": venture.name, "detail": _first_line(card.body_md),
                "path": path, "cta_label": "Review and retry", "_failure_type": "build" if model is BuildRun else "deployment",
                "_failure_id": latest.id, "_failure_card_id": card.id, "_failure_card_kind": card.kind,
                "_failure_key": key}
            if _decision_current(s, venture, owner, ctx):
                go(owner, "action_failed", ctx, f"{venture.id}:{card_kind}:{latest.id}")


def _outreach(s, venture, owner, now, go) -> None:
    """Count only attributed Gmail receipts and saved review-only draft batches."""
    since = now - EVENT_LOOKBACK
    journey_receipts = set()
    for doc in s.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture.id,
            VentureDocument.kind == "outreach_draft")):
        draft = _loads(doc.provenance_json)
        if draft.get("requires_journey_review") is True and draft.get("state") == "sent" and draft.get("channel") == "email":
            try:
                journey_receipts.add((draft.get("contact_id"), _naive(datetime.fromisoformat(draft["sent_at"]))))
            except (KeyError, TypeError, ValueError):
                continue
    for direction, template in (("out", "outreach_sent"), ("in", "replies_received")):
        previous = s.scalars(select(EmailMessage).where(EmailMessage.person_id == owner,
            EmailMessage.template == template, EmailMessage.created_at > since)).all()
        done = {source for row in previous for source in _loads(row.context_json).get("source_ids", [])}
        sent_receipts = {touch.message_id for source in done
            if (touch := s.get(GtmTouch, source.removeprefix("touch:"))) and _real_touch(touch, venture, direction)}
        rows = s.scalars(select(GtmTouch).where(GtmTouch.venture_id == venture.id,
            GtmTouch.workspace_id == venture.workspace_id, GtmTouch.direction == direction,
            GtmTouch.channel == "email", GtmTouch.simulated == 0, GtmTouch.message_id.is_not(None),
            (GtmTouch.provider_thread_id.is_not(None) if direction == "in" else True), GtmTouch.sent_at > since,
            GtmTouch.sent_at <= now).order_by(GtmTouch.sent_at, GtmTouch.id)).all()
        batches = {}
        for row in rows:
            if f"touch:{row.id}" in done:
                continue
            if not _real_touch(row, venture, direction):
                continue
            if row.message_id in sent_receipts:
                continue
            # One completed hourly batch, no external recipient or message content.
            batch = _naive(row.sent_at).strftime("%Y%m%d%H")
            if _naive(row.sent_at).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1) > now:
                continue
            sent_receipts.add(row.message_id)
            batches.setdefault(batch, []).append(row.id)
        for batch, ids in batches.items():
            sources = [f"touch:{ident}" for ident in ids]
            digest = hashlib.sha256(json.dumps(sources).encode()).hexdigest()[:24]
            journey = direction == "out" and any(row.id in ids and (row.contact_id, _naive(row.sent_at)) in journey_receipts for row in rows)
            go(owner, template, {"venture_name": venture.name, "_venture_id": venture.id, "count": len(ids), "source_ids": sources,
                "_gtm_journey": journey, "path": f"/gtm?venture={venture.id}" if journey else
                f"/v/{venture.id}" + ("?chat=1" if direction == "in" else "")}, ref=f"{venture.id}:{batch}:{digest}")
    batches, journey_batches = {}, set()
    for doc in s.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture.id,
            VentureDocument.kind == "outreach_draft", VentureDocument.created_at > since,
            VentureDocument.created_at <= now - PROGRESS_WINDOW)):
        draft = _loads(doc.provenance_json)
        if draft.get("state") == "draft" and draft.get("channel") == "email" and draft.get("requires_batch_approval") is True and draft.get("batch_id"):
            batches.setdefault(draft["batch_id"], []).append(doc.id)
            if draft.get("requires_journey_review") is True:
                journey_batches.add(draft["batch_id"])
    for batch, ids in batches.items():
        go(owner, "outreach_drafts_ready", {"venture_name": venture.name, "_venture_id": venture.id, "count": len(ids),
            "_gtm_journey": batch in journey_batches,
            "path": f"/gtm?venture={venture.id}" if batch in journey_batches else f"/v/{venture.id}?chat=1", "_batch_id": batch, "_draft_ids": sorted(ids)}, ref=f"{venture.id}:{batch}")


def _internal_schedule(s, now) -> None:
    yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    signups = s.scalar(select(func.count()).select_from(BetaCodeRedemption).where(
        BetaCodeRedemption.created_at >= yesterday, BetaCodeRedemption.created_at < yesterday + timedelta(days=1))) or 0
    if signups:
        _admin("signup_summary", {"count": signups, "path": "/admin/users"}, yesterday.strftime("%Y%m%d"))
    for request in s.scalars(select(FeedbackEvent).where(FeedbackEvent.topic == "credit_request",
            FeedbackEvent.source == "founder", FeedbackEvent.created_at > now - EVENT_LOOKBACK,
            FeedbackEvent.created_at <= now)):
        _admin("credit_request_admin", {"path": "/admin/credits"}, request.id)


def _recover_queued() -> int:
    """Restore one missing send intent, without restarting an exhausted retry budget."""
    from app.core.jobs import ACTIVE_JOB_STATUSES, enqueue, publish_wake
    from app.email.service import MAX_ATTEMPTS, _lock
    recovered = 0
    with db()() as s:
        _lock(s, "global_send")
        rows = s.scalars(select(EmailMessage).where(EmailMessage.status == "queued").with_for_update()).all()
        if not rows:
            return 0
        queued_ids = {row.id for row in rows}
        jobs = {}
        for job in s.scalars(select(Job).where(Job.kind == "email.send")):
            message_id = _loads(job.payload_json).get("message_id")
            if isinstance(message_id, str) and message_id in queued_ids:
                jobs.setdefault(message_id, []).append(job)
        for row in rows:
            related = jobs.get(row.id, [])
            key = f"email.send:{row.id}:recover"
            if any(job.status in ACTIVE_JOB_STATUSES or job.dedupe_key == key for job in related):
                continue
            try:
                context = json.loads(row.context_json)
            except (TypeError, ValueError):
                context = None
            context = context if isinstance(context, dict) else None
            attempts = max(sum(max(0, (job.attempts or 0) - 1) if job.status in {"succeeded", "done"}
                else (job.attempts or 0) if job.status in {"failed", "failed_dead", "poison", "cancelled"} else 0 for job in related),
                1 if context and context.get("_email_first_attempt_at") else 0)
            if attempts >= MAX_ATTEMPTS:
                continue
            if attempts and context is not None and not context.get("_email_first_attempt_at") and context.get("_email_admission_version") != 1:
                context["_email_first_attempt_at"] = row.created_at.isoformat()
                row.context_json = json.dumps(context, sort_keys=True)
            job_id = enqueue(s, "email.send", {"message_id": row.id}, dedupe_key=key,
                max_attempts=MAX_ATTEMPTS, priority=10)
            s.flush()
            s.get(Job, job_id).attempts = attempts
            recovered += 1
        s.commit()
    if recovered:
        publish_wake()
    return recovered


def run_schedule(now: datetime | None = None) -> dict[str, int]:
    """Hourly. Source IDs, the existing outbox constraint and stable refs survive restarts."""
    now = _naive(now) if now else utcnow()
    try:
        digest_hour = max(0, min(23, int(os.environ.get("EMAIL_DIGEST_HOUR_UTC", "15"))))
    except ValueError:
        digest_hour = 15
    hour_ok = now.hour >= digest_hour
    counts = {"queued": 0}
    try:
        _recover_queued()
    except Exception:
        log.warning("email_orphan_recovery_failed")
    from app.email.venture_receipts import reconcile

    reconcile()

    def go(person_id, template, ctx, ref):
        ctx = dict(ctx)
        venture_id = _context_venture(ctx)
        if venture_id:
            ctx["_venture_id"] = venture_id
        if template in {"digest_daily", "weekly_insights", "weekly_learning", "suggestions_waiting"}:
            with db()() as check:
                ctx["_workspace_ids"] = sorted(check.scalars(select(Workspace.id).where(
                    Workspace.owner_id == person_id, Workspace.archived_at.is_(None))))
                ctx["_venture_ids"] = sorted(v.id for v in _owned_ventures(check, person_id))
            ctx["_activity_since"] = (now - timedelta(days=1 if template == "digest_daily" else 7)).isoformat()
            if template == "weekly_learning":
                ctx["_activity_until"] = now.isoformat()
        with db()() as check:
            if _has(check, person_id, template, ref):
                return
        message_id = send_email(person_id, template, ctx, ref=ref)
        if message_id:
            with db()() as check:
                row = check.get(EmailMessage, message_id)
                if row and row.status == "queued":
                    counts["queued"] += 1

    with db()() as s:
        # No fixed limit that permanently starves founders after the first 5,000.
        people = s.execute(select(Person.id, Person.created_at).where(Person.email.is_not(None))
            .order_by(Person.id)).all()
    for person_id, created in people:
        try:
            created = _naive(created)
            with db()() as s:
                active = last_active(s, person_id)
                age = now - created
                if timedelta(0) <= age < timedelta(days=3):
                    go(person_id, "welcome", _welcome_context(s, person_id), "")
                ventures = _owned_ventures(s, person_id)
                person_phase = person_wallet_enabled(s)
                if person_phase:
                    wallet = s.scalar(select(CreditAccount).where(CreditAccount.scope == "person",
                        CreditAccount.owner_person_id == person_id))
                    if wallet:
                        _credits(wallet, person_id, "Your account", lambda p, t, c, *, ref: go(p, t, c, ref))
                for report in s.scalars(select(ProblemReport).where(ProblemReport.person_id == person_id,
                        ProblemReport.created_at <= now,
                        (ProblemReport.created_at > now - EVENT_LOOKBACK) | (ProblemReport.status == "resolved"))):
                    path = f"/v/{report.venture_id}/now" if report.venture_id in {v.id for v in ventures} else "/"
                    if _naive(report.created_at) > now - EVENT_LOOKBACK:
                        go(person_id, "problem_received", {"path": path}, report.id)
                    if report.status == "resolved" and _has(s, person_id, "problem_received", report.id):
                        go(person_id, "problem_resolved", {"path": path}, report.id)
                # Relevant product progress precedes generic reminders and digests.
                for venture in ventures:
                    if not person_phase:
                        account = s.scalar(select(CreditAccount).where(CreditAccount.venture_id == venture.id,
                            CreditAccount.workspace_id == venture.workspace_id, CreditAccount.scope == "venture"))
                        if account:
                            _credits(account, person_id, venture.name, lambda p, t, c, *, ref: go(p, t, c, ref))
                    _decisions(s, venture, person_id, now, go)
                    _progress(s, venture, person_id, now, go)
                    _outreach(s, venture, person_id, now, go)
                    idea = s.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture.id,
                        IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
                        .order_by(IdeaSession.created_at.desc()).limit(1))
                    if idea and idea.state == "confirmed" and idea.confirmed_at and now - _naive(idea.confirmed_at) < timedelta(days=1):
                        running = s.scalar(select(Job.id).where(Job.venture_id == venture.id,
                            Job.workspace_id == venture.workspace_id, Job.kind == "research.deep",
                            Job.status.in_(("running", "leased"))).limit(1))
                        finished = s.scalar(select(ResearchDossier.id).where(ResearchDossier.venture_id == venture.id,
                            ResearchDossier.revision_id == idea.confirmed_revision_id,
                            ResearchDossier.state.in_(("complete", "partial"))).limit(1))
                        if running and not finished:
                            go(person_id, "research_started", {"venture_name": venture.name,
                                "path": f"/v/{venture.id}/research", "_research_job": running}, idea.id)
                    if idea and idea.state not in {"confirmed", "validating"} and timedelta(days=1) <= now - _naive(idea.updated_at) < EVENT_LOOKBACK:
                        turn = s.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                            .order_by(ClarifierTurn.turn.desc()).limit(1))
                        if turn and turn.role == "agent" and turn.kind == "question":
                            go(person_id, "idea_stalled", {"venture_name": venture.name, "detail": _first_line(turn.text),
                                "path": f"/v/{venture.id}/idea", "_idea_session": idea.id}, idea.id)
                if not ventures and timedelta(hours=1) <= age < timedelta(days=3):
                    attempt = 2 if age >= timedelta(days=1) else 1
                    go(person_id, "finish_setup", {"path": "/new", "attempt": attempt, "_eligible_until": (created + timedelta(days=3)).isoformat()}, f"setup:{attempt}")
                if timedelta(days=1) <= age < EVENT_LOOKBACK and not s.scalar(select(DesktopDevice.id).where(
                        DesktopDevice.person_id == person_id, DesktopDevice.revoked_at.is_(None)).limit(1)):
                    code = _admission_code(s, person_id)
                    if code:
                        go(person_id, "get_mac_app", {"admission_code": code, "_eligible_until": (created + EVENT_LOOKBACK).isoformat()}, "desktop")
                if ventures and timedelta(days=3) <= now - _naive(ventures[0].created_at) < timedelta(days=10):
                    go(person_id, "beta_feedback", {"venture_name": ventures[0].name,
                        "path": f"/v/{ventures[0].id}", "_eligible_until": (_naive(ventures[0].created_at) + timedelta(days=10)).isoformat()}, ventures[0].id)
                if hour_ok:
                    # Daily is opt-in: absence of a preference row is not consent.
                    pref = s.get(EmailPreference, person_id)
                    digest_step = _next_step(ventures[-1], person_id) if ventures else {"path": "/new"}
                    if now.weekday() == 0:
                        week = now.strftime("%G-W%V")
                        if not _has(s, person_id, "weekly_insights", week) and not _has(s, person_id, "weekly_learning", week):
                            items = _activity(s, person_id, now - EVENT_LOOKBACK, now)
                            signups = _signup_count(s, ventures, now - EVENT_LOOKBACK, now)
                            learning = _weekly_learning(s, person_id, now - EVENT_LOOKBACK, now)
                            if learning and template_enabled("weekly_learning", person_id):
                                go(person_id, "weekly_learning", {**digest_step, **learning, "items": items}, week)
                            elif items or signups:
                                go(person_id, "weekly_insights", {**digest_step, "items": items, "signup_count": signups}, week)
                    elif pref and pref.digests:
                        items = _activity(s, person_id, now - timedelta(days=1), now)
                        signups = _signup_count(s, ventures, now - timedelta(days=1), now)
                        if signups:
                            items.append(f"{signups} waitlist signups: unverified interest")
                        if items:
                            go(person_id, "digest_daily", {**digest_step, "items": items}, now.strftime("%Y%m%d"))
                    opps = _suggestions(s, person_id, now - EVENT_LOOKBACK, now)
                    if opps:
                        go(person_id, "suggestions_waiting", {"items": [t for _, t in opps], "count": len(opps), "path": "/"}, opps[0][0])
                baseline = max(_naive(x) for x in (active, created) if x is not None)
                idle = (now - baseline).days
                due = max((d for d in NUDGE_DAYS if idle >= d), default=None)
                if due and idle < due + (7 if due in (14, 30) else (3 if due == 2 else 9)):
                    ctx = _next_step(ventures[-1], person_id) if ventures else {"path": "/new", "next_step": "Tell Charlie your idea", "cta_label": "Start your first venture"}
                    until = baseline + timedelta(days=due + (7 if due in (14, 30) else (3 if due == 2 else 9)))
                    go(person_id, f"nudge_d{due}", {**ctx, "_activity_baseline": baseline.isoformat(),
                        "_eligible_until": until.isoformat()}, baseline.isoformat())
        except Exception:  # one person's failure must not stop the rest; no exception personal data
            log.warning("email_schedule_person_failed")
    try:
        from app.email.service import run_health_check
        run_health_check()
    except Exception:
        log.warning("email_health_check_failed")
    if hour_ok:
        try:
            with db()() as s:
                _internal_schedule(s, now)
        except Exception:
            log.warning("email_internal_schedule_failed")
    return counts
