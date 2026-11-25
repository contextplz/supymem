"""Outbound webhooks (41 §6).

Contract: HMAC-SHA256 over the raw body with a per-subscription secret, a `t=`
timestamp so a captured request cannot be replayed beyond five minutes,
at-least-once delivery with exponential retry for 24 h, an inspectable delivery
log, and a dead-letter list with manual redelivery.

Payloads carry ids and a summary line. Never secrets, credentials or file
contents — enforced by :func:`scrub` and swept by the tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets as secrets_mod
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select

from app.audit.writer import audit
from app.db.models import WebhookDelivery, WebhookSubscription
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id
from core.env_contract import parse_app_env
from core.research_web.guard import check_url, pin_request

EVENTS: frozenset[str] = frozenset(
    {
        "engagement.created",
        "engagement.blocked",
        "engagement.delivered",
        "engagement.failed",
        "workflow.run.succeeded",
        "workflow.run.failed",
        "question.pending",
        "approval.pending",
        "proposal.created",
        "cost.threshold.crossed",
        "governance.autonomy.changed",
        "killswitch.engaged",
    }
)

REPLAY_WINDOW = timedelta(minutes=5)
# Exponential, capped, spanning ~24 h in eight attempts.
BACKOFF_S = (30, 120, 600, 1800, 7200, 21600, 43200, 86400)
MAX_ATTEMPTS = len(BACKOFF_S)
SUMMARY_LIMIT = 200

# Keys that must never leave, whatever a caller passes in.
_FORBIDDEN_KEYS = re.compile(
    r"(secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|"
    r"authorization|cookie|session|bearer|content|body|diff|patch|source)",
    re.IGNORECASE,
)
_SECRET_SHAPES = (
    re.compile(r"\bcos_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
)

REDACTED = "[redacted]"


class WebhookError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def scrub(value: Any, *, _depth: int = 0) -> Any:
    """Drop anything that looks like a secret or a file body."""
    if _depth > 6:
        return REDACTED
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if _FORBIDDEN_KEYS.search(str(key)):
                clean[str(key)] = REDACTED
            else:
                clean[str(key)] = scrub(item, _depth=_depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [scrub(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        text = value
        for shape in _SECRET_SHAPES:
            text = shape.sub(REDACTED, text)
        return text[:SUMMARY_LIMIT]
    return value


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subscription:
    id: str
    workspace_id: str
    url: str
    events: list[str]
    status: str
    # Shown once, at creation, so they can configure verification.
    secret: str | None = None

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        body = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "url": self.url,
            "events": self.events,
            "status": self.status,
        }
        if include_secret and self.secret:
            body["secret"] = self.secret
        return body


def _local_url(url: str) -> bool:
    parts = urlsplit(url)
    return parse_app_env() == "local" and parts.scheme == "http" and parts.hostname == "127.0.0.1"


def subscribe(
    *, workspace_id: str, url: str, events: list[str], actor_id: str = "system"
) -> Subscription:
    if not url.startswith("https://") and not _local_url(url):
        raise WebhookError(
            "insecure_url", "Webhook URLs must be https (localhost may be http)."
        )
    unknown = sorted(set(events) - EVENTS)
    if unknown:
        raise WebhookError("unknown_event", f"Unknown events: {', '.join(unknown)}")
    if not events:
        raise WebhookError("no_events", "Subscribe to at least one event.")
    if not _local_url(url):
        try:
            reason = check_url(url)
        except ValueError as exc:
            raise WebhookError("unsafe_url", "Webhook URLs must resolve to a public address.") from exc
        if reason:
            raise WebhookError("unsafe_url", "Webhook URLs must resolve to a public address.")

    from app.connectors.secrets import store

    secret = secrets_mod.token_urlsafe(32)
    ref = store().put(
        workspace_id=workspace_id, provider="webhook", key="signing", value=secret
    )
    row = WebhookSubscription(
        id=new_id("whk"),
        workspace_id=workspace_id,
        url=url,
        secret_ref=ref,
        events_json=json.dumps(sorted(set(events))),
        status="active",
        created_at=utcnow(),
    )
    factory = db()
    with factory() as session:
        session.add(row)
        session.commit()
        sub = Subscription(
            id=row.id,
            workspace_id=workspace_id,
            url=url,
            events=sorted(set(events)),
            status="active",
            secret=secret,
        )
    audit(
        "webhook.subscribed",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        target_ref=sub.id,
        payload={"url": url, "events": sub.events},
    )
    return sub


def unsubscribe(subscription_id: str, *, actor_id: str = "system") -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.get(WebhookSubscription, subscription_id)
        if row is None:
            raise WebhookError("not_found", "No subscription with that id.")
        row.status = "disabled"
        workspace_id = row.workspace_id
        ref = row.secret_ref
        session.commit()

    from app.connectors.secrets import store

    store().forget(ref)
    audit(
        "webhook.unsubscribed",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        target_ref=subscription_id,
    )
    return {"id": subscription_id, "status": "disabled"}


def list_subscriptions(workspace_id: str) -> list[Subscription]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(WebhookSubscription).where(
                WebhookSubscription.workspace_id == workspace_id
            )
        ).all()
        return [
            Subscription(
                id=r.id,
                workspace_id=r.workspace_id,
                url=r.url,
                events=json.loads(r.events_json or "[]"),
                status=r.status,
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------


def sign(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(utcnow().timestamp())
    mac = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, *, now: int | None = None) -> bool:
    """Constant-time verification that also rejects stale timestamps."""
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    ts_raw, provided = parts.get("t"), parts.get("v1")
    if not ts_raw or not provided:
        return False
    try:
        ts = int(ts_raw)
    except ValueError:
        return False
    moment = now if now is not None else int(utcnow().timestamp())
    if abs(moment - ts) > REPLAY_WINDOW.total_seconds():
        return False
    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------


def emit(event: str, *, workspace_id: str, payload: dict[str, Any]) -> list[str]:
    """Queue one event for every active subscriber. Returns delivery ids."""
    if event not in EVENTS:
        raise WebhookError("unknown_event", f"Unknown event: {event}")
    clean = scrub(payload)
    body = {
        "event": event,
        "workspace_id": workspace_id,
        "created_at": utcnow().isoformat(),
        "data": clean,
    }
    now = utcnow()
    ids: list[str] = []
    factory = db()
    with factory() as session:
        subs = session.scalars(
            select(WebhookSubscription).where(
                WebhookSubscription.workspace_id == workspace_id,
                WebhookSubscription.status == "active",
            )
        ).all()
        for sub in subs:
            if event not in set(json.loads(sub.events_json or "[]")):
                continue
            row = WebhookDelivery(
                id=new_id("whd"),
                subscription_id=sub.id,
                event=event,
                payload_json=json.dumps(body, default=str),
                attempts=0,
                status="pending",
                next_attempt_at=now,
                created_at=now,
            )
            session.add(row)
            ids.append(row.id)
        session.commit()
    return ids


@dataclass(frozen=True)
class Attempt:
    delivery_id: str
    status: str
    attempts: int
    error: str | None = None


def deliver_pending(*, limit: int = 20, now: Any = None) -> list[Attempt]:
    """Send anything due. Called by the scheduler and directly by tests."""
    moment = now or utcnow()
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.status.in_(("pending", "retrying")))
            .order_by(WebhookDelivery.created_at)
            .limit(limit)
        ).all()
        due = [
            r for r in rows if (as_utc(r.next_attempt_at) or moment) <= moment
        ]
        pending = [(r.id, r.subscription_id, r.payload_json) for r in due]

    results: list[Attempt] = []
    for delivery_id, subscription_id, payload_json in pending:
        results.append(_attempt(delivery_id, subscription_id, payload_json, moment))
    return results


def _attempt(
    delivery_id: str, subscription_id: str, payload_json: str, moment: Any
) -> Attempt:
    import httpx

    from app.connectors.secrets import store

    factory = db()
    with factory() as session:
        sub = session.get(WebhookSubscription, subscription_id)
        url = sub.url if sub else None
        ref = sub.secret_ref if sub else None

    if url is None or ref is None:
        return _record(delivery_id, ok=False, error="subscription missing", moment=moment)

    try:
        secret = store().get(ref, requester="webhooks")
    except (KeyError, PermissionError) as exc:
        return _record(delivery_id, ok=False, error=f"secret unavailable: {exc}", moment=moment)

    body = payload_json.encode()
    signature = sign(secret, body, timestamp=int(moment.timestamp()))
    try:
        with httpx.Client(follow_redirects=False, trust_env=False, timeout=10.0) as client:
            request = client.build_request("POST", url, content=body, headers={
                "Content-Type": "application/json",
                "X-CompanyOS-Signature": signature,
                "X-CompanyOS-Delivery": delivery_id,
            })
            if not _local_url(url):
                pin_request(request)
            resp = client.send(request)
        ok = 200 <= resp.status_code < 300
        error = None if ok else f"HTTP {resp.status_code}"
    except Exception as exc:  # network shapes vary; all are retryable
        ok = False
        error = type(exc).__name__
    return _record(delivery_id, ok=ok, error=error, moment=moment)


def _record(delivery_id: str, *, ok: bool, error: str | None, moment: Any) -> Attempt:
    factory = db()
    with factory() as session:
        row = session.get(WebhookDelivery, delivery_id)
        if row is None:
            return Attempt(delivery_id, "missing", 0, "delivery missing")
        row.attempts += 1
        if ok:
            row.status = "delivered"
            row.delivered_at = moment
            row.last_error = None
            row.next_attempt_at = None
        elif row.attempts >= MAX_ATTEMPTS:
            row.status = "dead"
            row.last_error = error
            row.next_attempt_at = None
        else:
            row.status = "retrying"
            row.last_error = error
            row.next_attempt_at = moment + timedelta(
                seconds=BACKOFF_S[min(row.attempts, MAX_ATTEMPTS - 1)]
            )
        status, attempts = row.status, row.attempts
        session.commit()
    return Attempt(delivery_id, status, attempts, error)


def redeliver(delivery_id: str, *, actor_id: str = "system") -> dict[str, Any]:
    """Manual retry from the dead-letter list."""
    factory = db()
    with factory() as session:
        row = session.get(WebhookDelivery, delivery_id)
        if row is None:
            raise WebhookError("not_found", "No delivery with that id.")
        row.status = "pending"
        row.attempts = 0
        row.next_attempt_at = utcnow()
        row.last_error = None
        session.commit()
    audit(
        "webhook.redelivered",
        actor_id=actor_id,
        actor_kind="person",
        target_ref=delivery_id,
    )
    return {"id": delivery_id, "status": "pending"}


def deliveries(
    workspace_id: str, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        sub_ids = [
            r.id
            for r in session.scalars(
                select(WebhookSubscription).where(
                    WebhookSubscription.workspace_id == workspace_id
                )
            ).all()
        ]
        if not sub_ids:
            return []
        query = select(WebhookDelivery).where(
            WebhookDelivery.subscription_id.in_(sub_ids)
        )
        if status:
            query = query.where(WebhookDelivery.status == status)
        rows = session.scalars(
            query.order_by(WebhookDelivery.created_at.desc()).limit(min(limit, 100))
        ).all()
        return [
            {
                "id": r.id,
                "subscription_id": r.subscription_id,
                "event": r.event,
                "attempts": r.attempts,
                "status": r.status,
                "last_error": r.last_error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
            }
            for r in rows
        ]
