"""Bounded scheduler pages on the existing durable job queue."""
from __future__ import annotations

import hashlib
import json
import math
import os
from contextvars import ContextVar
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.core.jobs import enqueue
from app.db.models import Venture, Workspace
from app.state import db
from core.clock import utcnow
from core.ids import new_id

FOUNDER_JOB = ContextVar("scheduled_founder_job", default=False)
KINDS = frozenset({"inbox.briefing.tick", "inbox.cos_brief.tick", "autopilot.tick", "gtm.journey.maintain"})


def batch_size():
    return max(1, min(100, int(os.getenv("SCHEDULED_FANOUT_BATCH_SIZE", "25"))))


def fanout(kind, payload, *, now=None):
    if kind not in KINDS:
        raise ValueError("unsupported_scheduled_kind")
    from app.tenancy import current_or_none
    scope = current_or_none()
    if scope and scope.workspace_id not in {"", "none"}:
        raise PermissionError("scheduled_fanout_requires_system_scope")
    moment = now or utcnow()
    window = payload.get("window") or moment.strftime("%Y%m%dT%H%M")
    cursor = payload.get("fanout_cursor", "")
    query = select(Workspace.owner_id, func.min(Workspace.id)).where(
        Workspace.archived_at.is_(None), Workspace.owner_id > cursor).group_by(
        Workspace.owner_id).order_by(Workspace.owner_id).limit(batch_size())
    with db()() as session:
        targets = session.execute(query).all()
        spread = max(0, min(300, int(os.getenv("SCHEDULED_FANOUT_SPREAD_SECONDS", "60"))))
        for founder_id, workspace_id in targets:
            key = f"{kind}:{window}:{founder_id}"
            delay = int(hashlib.sha256(founder_id.encode()).hexdigest()[:8], 16) % (spread + 1) if len(targets) > 1 or cursor else 0
            enqueue(session, kind, {"founder_id": founder_id, "window": window,
                "_actor_subject": founder_id}, workspace_id=workspace_id,
                run_after=moment + timedelta(seconds=delay), dedupe_key=key, effect_key=key, priority=10)
        if len(targets) == batch_size():
            key = f"{kind}:{window}:page:{targets[-1][0]}"
            enqueue(session, kind, {"window": window, "fanout_cursor": targets[-1][0]},
                run_after=moment, dedupe_key=key, effect_key=key, priority=10)
        session.commit()
    return len(targets)


def founder_target(founder_id):
    with db()() as session:
        row = session.execute(select(Venture.workspace_id, Venture.id).join(
            Workspace, Venture.workspace_id == Workspace.id).where(
            Workspace.owner_id == founder_id, Workspace.archived_at.is_(None),
            Venture.archived_at.is_(None)).order_by(Venture.created_at, Venture.id).limit(1)).first()
    return row


def founder_ventures(founder_id):
    return select(Venture.id).join(Workspace, Venture.workspace_id == Workspace.id).where(
        Workspace.owner_id == founder_id, Workspace.archived_at.is_(None), Venture.archived_at.is_(None))


def authorize_founder(payload):
    from app.tenancy import current
    from app.authz.workspace import subject_person_id
    scope = current()
    with db()() as session:
        owner = session.scalar(select(Workspace.owner_id).where(Workspace.id == scope.workspace_id,
            Workspace.archived_at.is_(None)))
    if not owner or owner != payload["founder_id"] or subject_person_id(scope.actor) != owner or payload.get("workspace_id") != scope.workspace_id:
        raise PermissionError("scheduled_founder_scope_mismatch")


def claim_founder(kind, payload):
    from app.inbox import store
    from app.core.ratelimit import RateLimitedJob
    store.ensure_schema(db().kw["bind"])
    with db()() as session:
        store.lock(session, payload["founder_id"])
        row = store.state(session, payload["founder_id"], payload["workspace_id"])
        values = json.loads(row.data_json)
        state = values.setdefault("scheduled_jobs", {}).setdefault(kind, {})
        moment = store.naive()
        until = store.naive(datetime.fromisoformat(state["lease_until"])) if state.get("lease_until") else moment
        if until > moment:
            wait = (until - moment).total_seconds()
            error = RateLimitedJob(min(5, math.ceil(wait)))
            error.args = ("scheduled_founder_busy",)
            raise error
        if payload.get("window") and state.get("checkpoint", "") >= payload["window"]:
            return None
        token = new_id("sfj")
        state.update(lease_id=token, lease_until=(moment + timedelta(minutes=15)).isoformat())
        store.save(row, values)
        session.commit()
        return payload["founder_id"], row.id, kind, token, payload.get("window")


def finish_founder(claim, *, succeeded):
    from app.inbox import store
    with db()() as session:
        store.lock(session, claim[0])
        row = session.get(store.InboxBriefingState, claim[1])
        values = json.loads(row.data_json)
        state = values["scheduled_jobs"][claim[2]]
        if state.get("lease_id") != claim[3]:
            return
        state.pop("lease_until", None)
        if succeeded and claim[4]:
            state["checkpoint"] = claim[4]
        store.save(row, values)
        session.commit()


def enqueue_mailboxes(venture_id=None, *, workspace_id=None, cursor="", window=None):
    from app.connections import mailboxes
    from app.core.ratelimit import gmail_account_key
    from app.db.models import GtmEmailAccount
    from app.gtm.supymem import pipeline
    from app.gtm.supymem.workers import _read_accounts
    from app.inbox import store
    store.ensure_schema(db().kw["bind"])
    moment = utcnow()
    window = window or moment.strftime("%Y%m%dT%H") + str(moment.minute // 10)
    with db()() as session:
        if venture_id:
            venture = session.get(Venture, venture_id)
            if venture is None or venture.archived_at is not None:
                return 0
            if workspace_id not in {None, venture.workspace_id}:
                raise PermissionError("mailbox_job_scope_mismatch")
            workspace_id = venture.workspace_id
        if not workspace_id:
            raise ValueError("mailbox_job_workspace_required")
        query = pipeline._mailboxes_query(session, workspace_id).where(
            GtmEmailAccount.status == "active", GtmEmailAccount.id > cursor).order_by(None).order_by(
            GtmEmailAccount.id).limit(batch_size())
        if mailboxes.enabled():
            accounts = list(session.scalars(query))
        else:
            account = pipeline._active_mailbox(session, workspace_id) if not cursor else None
            accounts = [account] if account is not None else []
        seen = set()
        for mailbox in accounts:
            account = next(account for account in _read_accounts(session, workspace_id, mailbox)
                if account.status == "active")
            owner = session.get(Workspace, account.workspace_id).owner_id
            account_key = gmail_account_key(owner, account.connected_account_id)
            if account_key in seen:
                continue
            seen.add(account_key)
            key = f"gtm.gmail.read:{account_key}:{window}"
            store.lock(session, owner)
            enqueue(session, "gtm.gmail.read", {"mailbox_id": account.id, "window": window, "_actor_subject": owner},
                workspace_id=account.workspace_id, dedupe_key=key,
                effect_key=key, run_after=utcnow(), priority=10)
            row = store.state(session, owner, account.workspace_id, account.id)
            data = json.loads(row.data_json or "{}")
            data["gmail_latest_window"] = max(window, data.get("gmail_latest_window", ""))
            store.save(row, data)
        if mailboxes.enabled() and len(accounts) == batch_size():
            key = f"gtm.gmail.read:{workspace_id}:{window}:page:{accounts[-1].id}"
            enqueue(session, "gtm.gmail.read", {"mailbox_cursor": accounts[-1].id, "window": window},
                workspace_id=workspace_id, dedupe_key=key, effect_key=key, priority=10)
        session.commit()
    return len(accounts)
