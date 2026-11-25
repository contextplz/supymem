"""Linear webhook verification — signature + freshness (39 §6, T10.47)."""

from __future__ import annotations

import json
import time

from app.connectors.protocol import WebhookEvent
from app.connectors.webhooks import WebhookRejected, header, require_fresh, verify_hex

SIGNATURE_HEADER = "Linear-Signature"
TIMESTAMP_HEADER = "Linear-Delivery-Timestamp"
DELIVERY_HEADER = "Linear-Delivery"
EVENT_HEADER = "Linear-Event"
EVENTS = ("Issue", "Comment", "IssueUpdate")


def verify(raw: bytes, headers: dict[str, str], secret: str, *, now_ms: float | None = None) -> WebhookEvent:
    signature = header(headers, SIGNATURE_HEADER)
    if not verify_hex(secret, raw, signature):
        raise WebhookRejected("signature mismatch")
    ts_raw = header(headers, TIMESTAMP_HEADER)
    try:
        sent_ms = float(ts_raw)
    except (TypeError, ValueError) as exc:
        raise WebhookRejected("missing timestamp") from exc
    require_fresh(sent_ms, now_ms=now_ms if now_ms is not None else time.time() * 1000)
    delivery = header(headers, DELIVERY_HEADER)
    if not delivery:
        raise WebhookRejected("missing delivery id")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookRejected("unparseable body") from exc
    issue = (payload.get("data") or payload).get("id")
    return WebhookEvent(
        provider="linear",
        delivery_id=delivery,
        event=header(headers, EVENT_HEADER) or "unknown",
        payload=payload,
        target=str(issue or ""),
    )
