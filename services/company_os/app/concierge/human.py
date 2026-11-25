"""Durable founder to-dos (DB human_tasks), distinct from in-memory catalog drafts."""

from __future__ import annotations

import json
import logging

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.concierge.social_guide import SOCIAL_GUIDE
from app.db.models import HumanTask, PlannedTask
from app.state import MEM, db

log = logging.getLogger(__name__)

_OPEN = ("open", "pending", "in_progress")

GUIDES: dict[str, dict] = {
    "github": {
        "open_url": "https://github.com/login",
        "open_label": "Open GitHub",
        "skip_label": "Keep the repo local for now",
        "steps": [
            {
                "text": "Sign in to GitHub",
                "url": "https://github.com/login",
            },
            {
                "text": "Create a fine-grained token with access to repositories",
                "url": "https://github.com/settings/tokens?type=beta",
            },
            {
                "text": "Paste the token below. It stays in the local vault, never in chat.",
            },
        ],
        "fields": [
            {
                "id": "token",
                "label": "GitHub token",
                "secret": True,
                "placeholder": "github_pat_…",
            }
        ],
    },
    "social": SOCIAL_GUIDE,
    "stripe": {
        "open_url": None,
        "open_label": "Complete Stripe on Supymem",
        "skip_label": "I'll connect Stripe later",
        "steps": [
            {
                "text": "Click Complete Stripe on Supymem. Stripe opens their KYC screens; you stay on this site.",
                "url": None,
            },
            {
                "text": "Finish the Stripe account. We store the connected account id, never card numbers.",
                "url": None,
            },
            {
                "text": "Optional: if you already have a Stripe account you want to use yourself, paste a secret key below instead.",
                "url": "https://dashboard.stripe.com/apikeys",
            },
        ],
        "fields": [
            {
                "id": "token",
                "label": "Stripe secret key (optional BYO)",
                "secret": True,
                "optional": True,
                "placeholder": "sk_test_…",
            }
        ],
    },
    "decision": {
        "open_label": "Answer in chat",
        "skip_label": "Keep this blocked for now",
        "steps": [
            {
                "text": "Reply in chat with the call, spend, destroy, or change scope only happens after you say so.",
                "url": None,
            }
        ],
    },
}

ALTERNATIVES: dict[str, dict] = {
    "github": {
        "title": "Keep building locally without GitHub",
        "why": "Supymem can continue in the venture workspace and connect a repository later.",
        "steps": [
            {"text": "Continue building in the local venture workspace."},
            {"text": "Keep deployment and collaboration work parked until a repository is connected."},
        ],
    },
    "social": {
        "title": "Prepare outreach for manual sending",
        "why": "Supymem can research prospects and prepare reviewed copy; you can send it from your own account when convenient.",
        "steps": [
            {"text": "Supymem prepares a small, reviewed outreach batch."},
            {"text": "Copy and send the approved messages manually from your own account."},
        ],
    },
    "stripe": {
        "title": "Validate willingness to pay before connecting payments",
        "why": "Use a pricing commitment or manual invoice request first, then connect Stripe after demand is proven.",
        "steps": [
            {"text": "Add a clear paid offer and collect payment intent without charging anyone."},
            {"text": "Connect Stripe before accepting an actual payment."},
        ],
    },
}


def _serialize(row: HumanTask) -> dict:
    try:
        steps = json.loads(row.steps_json or "[]")
    except json.JSONDecodeError:
        steps = []
    provider = row.provider or ""
    guide = GUIDES.get(provider) or {}
    if not steps:
        steps = list(guide.get("steps") or [])
    if provider in GUIDES and GUIDES[provider].get("open_url"):
        href = f"/v/{row.venture_id}/connect/{provider}"
    elif provider:
        href = f"/v/{row.venture_id}#attention-{row.id}"
    else:
        href = f"/v/{row.venture_id}#attention-{row.id}"
    return {
        "id": row.id,
        "title": row.title,
        "why": row.why,
        "provider": provider,
        "category": row.category,
        "status": row.status,
        "priority": row.priority,
        "steps": steps,
        "guide": guide,
        "href": href,
    }


def ensure_human_task(
    venture_id: str,
    *,
    title: str,
    why: str,
    provider: str,
    category: str = "credential",
    steps: list | None = None,
    priority: int = 1,
) -> dict:
    """Create one open task per provider, or return the existing one."""
    factory = db()
    now = utcnow()
    guide = GUIDES.get(provider) or {}
    with factory() as session:
        existing = session.scalars(
            select(HumanTask).where(
                HumanTask.venture_id == venture_id,
                HumanTask.provider == provider,
                HumanTask.status.in_(_OPEN),
            )
        ).first()
        if existing:
            return _serialize(existing)
        row = HumanTask(
            id=new_id("ht"),
            venture_id=venture_id,
            title=title,
            why=why,
            provider=provider,
            category=category,
            status="open",
            priority=priority,
            blocks_json=json.dumps([provider]),
            lead_time_days=0,
            steps_json=json.dumps(steps or guide.get("steps") or [{"text": title, "url": None}]),
            expects_json=json.dumps({"armed": True}),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        result = _serialize(row)
    # Delivery is intentionally after the task commit. A notification is a
    # secondary channel; it must never create a task that does not exist or
    # make the durable founder queue depend on macOS delivery.
    try:
        from app.desktop_notices import enqueue as notify_desktop

        notify_desktop(
            title=f"Supymem needs you: {title}",
            body=why or "Open Supymem to continue this company.",
            venture_id=venture_id,
            kind="human_task",
            # A notification always returns to the durable task itself. Some
            # task cards link onward to a provider setup guide, but using that
            # card href here would skip the founder's recorded request and can
            # point outside the native shell's allowlisted routes.
            target_path=f"/v/{venture_id}#attention-{result['id']}",
        )
    except Exception:
        log.exception("desktop_notice_enqueue_failed", extra={"venture_id": venture_id})
    return result


def list_open_human_tasks(venture_id: str) -> list[dict]:
    from app.core.local_mode import is_local

    local = is_local()
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(HumanTask)
            .where(
                HumanTask.venture_id == venture_id,
                HumanTask.status.in_(_OPEN),
            )
            .order_by(HumanTask.priority.asc(), HumanTask.created_at.asc())
        ).all()
        return [_serialize(row) for row in rows if not (local and row.provider == "stripe")]


def dismiss_provider_attention_if_unused(venture_id: str, provider: str) -> int:
    """Dismiss provider attention after its last blocked planned task is gone.

    A provider ask can represent more than one planned task, so dismissing one
    task must not clear attention still required by another.  Conversely, the
    founder should not keep seeing an obsolete status-bar alert after they
    dismiss the final blocked task.
    """
    provider = provider.strip()
    if not provider:
        return 0
    factory = db()
    now = utcnow()
    needle = f"needs_you:{provider}"
    with factory() as session:
        remaining = session.scalars(
            select(PlannedTask.id).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status.in_(("open", "running")),
                PlannedTask.last_error == needle,
            )
        ).first()
        if remaining:
            return 0
        rows = session.scalars(
            select(HumanTask).where(
                HumanTask.venture_id == venture_id,
                HumanTask.provider == provider,
                HumanTask.status.in_(_OPEN),
            )
        ).all()
        for row in rows:
            try:
                submitted = json.loads(row.submitted_json or "{}")
            except (json.JSONDecodeError, TypeError):
                submitted = {}
            row.status = "dismissed"
            row.completed_at = now
            row.updated_at = now
            row.submitted_json = json.dumps(
                {
                    **submitted,
                    "dismissed": True,
                    "reason": "No remaining planned work requires this provider.",
                }
            )
        session.commit()
        return len(rows)


def request_alternative(venture_id: str, task_id: str, *, reason: str = "") -> dict:
    """Replace a blocked founder ask with an explicit lower-dependency path.

    The ask remains visible: requesting an alternative is not the same as
    claiming the underlying dependency is complete.
    """
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(HumanTask, task_id)
        if not row or row.venture_id != venture_id or row.status not in _OPEN:
            raise KeyError(task_id)
        alternative = ALTERNATIVES.get(row.provider or "") or {
            "title": "Replan around this dependency",
            "why": "Keep the blocked action visible while Supymem finds work that can continue without it.",
            "steps": [
                {"text": "Leave this dependency parked."},
                {"text": "Continue with research, planning, or preparation that does not require it."},
            ],
        }
        row.title = str(alternative["title"])
        row.why = str(alternative["why"])
        row.category = "alternative"
        row.steps_json = json.dumps(alternative["steps"])
        row.submitted_json = json.dumps({
            "alternative_requested": True,
            "reason": reason.strip()[:1000],
            "original_provider": row.provider,
        })
        row.updated_at = now
        session.commit()
        return _serialize(row)


def get_human_task(venture_id: str, task_id: str | None = None, *, provider: str | None = None) -> dict | None:
    factory = db()
    with factory() as session:
        row = None
        if task_id:
            row = session.get(HumanTask, task_id)
            if row and row.venture_id != venture_id:
                return None
        elif provider:
            row = session.scalars(
                select(HumanTask).where(
                    HumanTask.venture_id == venture_id,
                    HumanTask.provider == provider,
                    HumanTask.status.in_(_OPEN),
                )
            ).first()
        return _serialize(row) if row else None


def complete_human_task(
    venture_id: str,
    *,
    provider: str,
    fields: dict | None = None,
    skipped: bool = False,
) -> dict:
    """Mark the ask done, store non-secret fields, unpark waiting planned work."""
    fields = {str(k): str(v).strip() for k, v in (fields or {}).items() if str(v).strip()}
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.scalars(
            select(HumanTask).where(
                HumanTask.venture_id == venture_id,
                HumanTask.provider == provider,
                HumanTask.status.in_(_OPEN),
            )
        ).first()
        if row is None:
            raise KeyError(provider)
        public = {k: v for k, v in fields.items() if k != "token"}
        token = fields.get("token")
        row.status = "skipped" if skipped else "done"
        row.completed_at = now
        row.updated_at = now
        row.submitted_json = json.dumps({"skipped": skipped, "fields": public})
        if token:
            from app.concierge.verify import store_in_vault_handle

            stored = store_in_vault_handle(
                token,
                f"vault:{provider}",
                workspace_id=venture_id,
                provider=provider,
            )
            row.vault_refs_json = json.dumps([stored.get("handle")])
        session.commit()
        out = _serialize(row)

    if public or token:
        key = f"founder_channels:{venture_id}"
        blob = dict(MEM.get(key) or {})
        stored_fields = {**public}
        if token:
            stored_fields["connected"] = "1"
        blob[provider] = {**dict(blob.get(provider) or {}), **stored_fields}
        MEM[key] = blob
    keep_parked = skipped and provider in {"social", "stripe"}
    if not keep_parked:
        unpark_for_provider(venture_id, provider)
    if provider == "decision":
        from app.munder.ledger import answer_human, sync_planned_runnable

        card_id = MEM.get(f"munder_decision_card:{venture_id}")
        answer = public.get("answer") or public.get("text") or ("skipped" if skipped else "acknowledged")
        if card_id:
            answer_human(venture_id, str(card_id), answer)
        try:
            sync_planned_runnable(venture_id)
        except Exception:
            log.exception("planned_task_sync_failed", extra={"venture_id": venture_id})
    return out


def unpark_for_provider(venture_id: str, provider: str) -> int:
    factory = db()
    now = utcnow()
    n = 0
    needle = f"needs_you:{provider}"
    with factory() as session:
        for row in session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "open",
                PlannedTask.runnable == 0,
            )
        ).all():
            if (row.last_error or "") != needle:
                continue
            row.runnable = 1
            row.last_error = None
            row.updated_at = now
            n += 1
        session.commit()
    return n
